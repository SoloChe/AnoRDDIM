"""
Synthetic domain translation from a source 2D domain to a target.
"""

import argparse
import os
import pathlib

import numpy as np
import torch.distributed as dist
import torch

from common import read_model_and_diffusion
from guided_diffusion import dist_util, logger
from guided_diffusion.script_util import (
    model_and_diffusion_defaults,
    add_dict_to_argparser,
)

from data import get_brats_data_iter, check_data
from obtain_hyperpara import obtain_hyperpara, get_mask_batch_FPDM
from evaluate import get_stats, median_pool, evaluate
from sample import sample

import matplotlib.pyplot as plt
from torchvision.utils import save_image, make_grid
import torch.nn.functional as F
from torch.nn.parallel.distributed import DistributedDataParallel as DDP


def main():
    args = create_argparser().parse_args()

    dist_util.setup_dist()
    logger.configure()
    logger.log(f"args: {args}")

    image_subfolder = args.image_dir
    pathlib.Path(image_subfolder).mkdir(parents=True, exist_ok=True)

    logger.log(f"reading models ...")
    args.num_classes = int(args.num_classes) if args.num_classes else None
    if args.num_classes:
        args.class_cond = True
    args.multi_class = True if args.num_classes > 2 else False

    model, diffusion = read_model_and_diffusion(
        args, args.model_dir, args.model_num, args.ema
    )

    data_val = get_brats_data_iter(
        args.data_dir,
        args.batch_size_val,
        split="val",
        mixed=True,
        training=False,
        seed=args.seed,
        logger=logger,
    )

    data_test = get_brats_data_iter(
        args.data_dir,
        args.batch_size,
        split="test",
        mixed=True,
        training=False,
        seed=args.seed,
        logger=logger,
    )

    model = DDP(
        model,
        device_ids=[dist_util.dev()],
        output_device=dist_util.dev(),
        broadcast_buffers=False,
        bucket_cap_mb=128,
        find_unused_parameters=False,
    )

    logger.log(f"Validation: starting to get threshold and abe range ...")
    if not args.tuned:
        thr_01, abe_min, abe_max = obtain_hyperpara(data_val, diffusion, model, args, dist_util.dev())
        logger.log(f"abe_min: {abe_min}, abe_max: {abe_max}, thr_01: {thr_01}")

    logger.log(f"starting to inference ...")

    DICE = []
    DICE_ANO = []
    IOU = []
    IOU_ANO = []
    RECALL = []
    RECALL_ANO = []
    PRECISION = []
    PRECISION_ANO = []
    AUC = []
    AUC_ANO = []
    PR_AUC = []
    PR_AUC_ANO = []
    Y = []
    PRED_Y = []

    k = 0
    while k < args.num_batches:
        all_sources = []
        all_masks = []
        all_pred_masks = []
        all_terms = {"xstart_null": [], "xstart": []}

        k += 1

        source, mask, lab = next(data_test)
        Y.append(lab)

        logger.log(
            f"translating at batch {k} on rank {dist.get_rank()}, shape {source.shape}..."
        )
        logger.log(f"device: {torch.cuda.current_device()}")

        source = source.to(dist_util.dev())
        mask = mask.to(dist_util.dev())

        logger.log(
            f"source with mean {source.mean()} and std {source.std()} on rank {dist.get_rank()}"
        )

        y0 = torch.ones(source.shape[0], dtype=torch.long) * torch.arange(
            start=0, end=1
        ).reshape(
            -1, 1
        )  # 0 for healthy
        y0 = y0.reshape(-1, 1).squeeze().to(dist_util.dev())

        model_kwargs_reverse = {"threshold": -1, "clf_free": True, "null": args.null}
        model_kwargs0 = {"y": y0, "threshold": -1, "clf_free": True}

        # inference
        minibatch_metrics = diffusion.calc_pred_xstart_loop(
            model,
            source,
            args.w,
            modality=args.modality,
            d_reverse=args.d_reverse,
            sample_steps=args.rev_steps,
            model_kwargs=model_kwargs0,
            model_kwargs_reverse=model_kwargs_reverse,
            dynamic_clip=args.dynamic_clip,
        )

        # collect metrics
        if not args.tuned:
            # w = 2
            thr_01 = 0.9968
            abe_min = torch.tensor([0.0021, 0.0018], device=dist_util.dev())
            abe_max = torch.tensor([0.1648, 0.1335], device=dist_util.dev())
            
            pred_mask, pred_lab, pred_map = get_mask_batch_FPDM(
                minibatch_metrics,
                source,
                args.modality,
                thr_01,
                abe_min,
                abe_max,
                args.image_size,
                median_filter=args.median_filter,
            )
        else:
            # already obtained tuned parameters
            thr = 0.11
            thr_01 = 0.9968
            t_e = torch.tensor([580, 580], device=dist_util.dev())
            pred_mask, pred_lab, pred_map = get_mask_batch_FPDM(
                minibatch_metrics,
                source,
                args.modality,
                thr_01,
                args.image_size,
                thr=thr,
                t_e=t_e,
                median_filter=args.median_filter,
            )
        PRED_Y.append(pred_lab)

        eval_metrics = evaluate(mask, pred_mask, source, pred_map)
        eval_metrics_ano = evaluate(mask, pred_mask, source, pred_map, lab)
        cls_metrics = get_stats(Y, PRED_Y)

        DICE.append(eval_metrics["dice"])
        DICE_ANO.append(eval_metrics_ano["dice"])

        IOU.append(eval_metrics["iou"])
        IOU_ANO.append(eval_metrics_ano["iou"])

        RECALL.append(eval_metrics["recall"])
        RECALL_ANO.append(eval_metrics_ano["recall"])

        PRECISION.append(eval_metrics["precision"])
        PRECISION_ANO.append(eval_metrics_ano["precision"])

        AUC.append(eval_metrics["AUC"])
        AUC_ANO.append(eval_metrics_ano["AUC"])

        PR_AUC.append(eval_metrics["PR_AUC"])
        PR_AUC_ANO.append(eval_metrics_ano["PR_AUC"])

        logger.log(
            f"-------------------------------------at batch {k}-----------------------------------------"
        )
        logger.log(f"mean dice: {eval_metrics['dice']:0.3f}")
        logger.log(f"mean iou: {eval_metrics['iou']:0.3f}")
        logger.log(f"mean precision: {eval_metrics['precision']:0.3f}")
        logger.log(f"mean recall: {eval_metrics['recall']:0.3f}")
        logger.log(f"mean auc: {eval_metrics['AUC']:0.3f}")
        logger.log(f"mean pr auc: {eval_metrics['PR_AUC']:0.3f}")

        logger.log(
            "-------------------------------------------------------------------------------------------"
        )
        logger.log(f"running dice: {np.mean(DICE):0.3f}")  # keep 3 decimals
        logger.log(f"running iou: {np.mean(IOU):0.3f}")
        logger.log(f"running precision: {np.mean(PRECISION):0.3f}")
        logger.log(f"running recall: {np.mean(RECALL):0.3f}")
        logger.log(f"running auc: {np.mean(AUC):0.3f}")
        logger.log(f"running pr auc: {np.mean(PR_AUC):0.3f}")
        logger.log(
            "-------------------------------------------------------------------------------------------"
        )
        logger.log(f"running dice ano: {np.mean(DICE_ANO):0.3f}")
        logger.log(f"running iou ano: {np.mean(IOU_ANO):0.3f}")
        logger.log(f"running precision ano: {np.mean(PRECISION_ANO):0.3f}")
        logger.log(f"running recall ano: {np.mean(RECALL_ANO):0.3f}")
        logger.log(f"running auc ano: {np.mean(AUC_ANO):0.3f}")
        logger.log(f"running pr auc ano: {np.mean(PR_AUC_ANO):0.3f}")
        logger.log(
            "-------------------------------------------------------------------------------------------"
        )
        logger.log(f"running cls acc: {cls_metrics['acc']:0.3f}")
        logger.log(f"running cls recall: {cls_metrics['recall']:0.3f}")
        logger.log(f"running cls precision: {cls_metrics['precision']:0.3f}")
        logger.log(f"running cls num_ano: {cls_metrics['num_ano']}")
        logger.log(
            "-------------------------------------------------------------------------------------------"
        )

        if args.save_data:
            logger.log("collecting metrics...")
            for key in all_terms.keys():
                terms = minibatch_metrics[key]
                gathered_terms = [
                    torch.zeros_like(terms) for _ in range(dist.get_world_size())
                ]
                dist.all_gather(gathered_terms, terms)
                all_terms[key].extend([term.cpu().numpy() for term in gathered_terms])

            gathered_source = [
                torch.zeros_like(source) for _ in range(dist.get_world_size())
            ]
            gathered_mask = [
                torch.zeros_like(mask) for _ in range(dist.get_world_size())
            ]
            gathered_pred_mask = [
                torch.zeros_like(pred_mask) for _ in range(dist.get_world_size())
            ]
            
            dist.all_gather(gathered_source, source)
            dist.all_gather(gathered_mask, mask)
            dist.all_gather(gathered_pred_mask, pred_mask)
            
            all_sources.extend([source.cpu().numpy() for source in gathered_source])
            all_masks.extend([mask.cpu().numpy() for mask in gathered_mask])
            all_pred_masks.extend([pred_mask.cpu().numpy()])
            
            all_sources = np.concatenate(all_sources, axis=0)
            all_sources_path = os.path.join(image_subfolder, f"source_{k}.npy")
            np.save(all_sources_path, all_sources)

            all_masks = np.concatenate(all_masks, axis=0)
            all_masks_path = os.path.join(image_subfolder, f"mask_{k}.npy")
            np.save(all_masks_path, all_masks)
            
            all_pred_masks = np.concatenate(all_pred_masks, axis=0)
            all_pred_masks_path = os.path.join(image_subfolder, f"pred_mask_{k}.npy")
            np.save(all_pred_masks_path, all_pred_masks)

            for key in all_terms.keys():
                all_terms_path = os.path.join(logger.get_dir(), f"{key}_terms_{k}.npy")
                np.save(all_terms_path, all_terms[key])

    dist.barrier()

    logger.log(f"evaluation complete")


def create_argparser():
    defaults = dict(
        data_dir="",
        image_dir="",
        model_dir="",
        batch_size=32,
        rev_steps=600,
        model_num=None,
        ema=False,
        null=False,
        save_data=False,
        num_batches_val=2,
        batch_size_val=100,
        d_reverse=True,
        median_filter=True,
        dynamic_clip=False,
        tuned=False,
        seed=0,  # reproduce
    )
    defaults.update(model_and_diffusion_defaults())
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--modality",
        type=int,
        nargs="+",
        help="0:flair, 1:t1, 2:t1ce, 3:t2",
        default=0.0,  # flair as default
    )

    parser.add_argument(
        "--w",
        type=float,
        help="weight for clf-free samples",
        default=-1,  # disabled in default
    )

    parser.add_argument(
        "--num_batches",
        type=int,
        help="weight for clf-free samples",
        default=1,  # disabled in default
    )

    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":
    main()
