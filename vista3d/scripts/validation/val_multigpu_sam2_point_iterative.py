# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# export CUDA_VISIBLE_DEVICES=0;torchrun --nnodes=1 --nproc_per_node=1 -m scripts.validation.val_multigpu_sam2_point_iterative run --config_file "['configs/supported_eval/infer_sam2_point.yaml']"

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import timedelta
from typing import Optional, Sequence, Union

import monai
from PIL import Image
import torch
import torch.distributed as dist
from monai import transforms
from monai.apps.auto3dseg.auto_runner import logger
from monai.auto3dseg.utils import datafold_read
from monai.bundle import ConfigParser
from monai.bundle.scripts import _pop_args, _update_args
from monai.data import DataLoader, partition_dataset
from monai.metrics import compute_dice
from monai.utils import set_determinism
import numpy as np
from sam2.build_sam import build_sam2_video_predictor
from scipy.ndimage import binary_erosion

from ..train import CONFIG
from ..utils.workflow_utils import generate_prompt_pairs_val, get_next_points_val

def save_nifti_frames_to_jpg(data, output_folder=None):
    data = torch.squeeze(data)
    # Ensure output folder exists
    if not os.path.exists(output_folder):
        os.makedirs(output_folder, exist_ok=True)
    # Loop through each frame in the 3D image
    for i in range(data.shape[2]):
        save_name = os.path.join(output_folder, f'{i + 1:04d}.jpg')
        if os.path.exists(save_name):
            continue
        frame = data[:, :, i]
        # Normalize the frame to the range 0-255
        frame = frame.astype(np.uint8)
        # Save the frame as a JPEG image
        img = Image.fromarray(frame)
        img.save(save_name)

    return output_folder


def get_points_from_label(labels, index=1):
    """ Sample the starting point 
        label [1, H, W, ...]
    """
    plabels = labels == index
    plabels = monai.transforms.utils.get_largest_connected_component_mask(
        plabels
    )
    plabelpoints = torch.nonzero(plabels)
    pmean = plabelpoints.float().mean(0)
    pdis = ((plabelpoints - pmean) ** 2).sum(-1)
    _, sorted_indices = torch.sort(pdis)
    point = plabelpoints[sorted_indices[0]]
    return point

def get_points_from_false_pred(pred, gt, _point, _label):
    # handle false postive 
    fp_mask = torch.logical_and(torch.logical_not(gt), pred)
    # Define the structuring element (kernel) of size 5x5
    structuring_element = np.ones((20, 20), dtype=np.uint8)
    # Perform erosion
    eroded_image = binary_erosion(fp_mask.cpu().numpy(), structure=structuring_element).astype(np.uint8)
    plabelpoints = torch.nonzero(torch.from_numpy(eroded_image))
    if len(plabelpoints) > 0:
        pdis = ((plabelpoints - torch.tensor([_point[0][1], _point[0][0]] ,device=plabelpoints.device)) ** 2).sum(-1)
        _, sorted_indices = torch.sort(pdis)
        npoint = plabelpoints[sorted_indices[0]]
        _point.append([npoint[1], npoint[0]])
        _label.append(0)
    
    # handle false negative 
    fp_mask = torch.logical_and(torch.logical_not(pred), gt)
    # Define the structuring element (kernel) of size 5x5
    structuring_element = np.ones((20, 20), dtype=np.uint8)
    # Perform erosion
    eroded_image = binary_erosion(fp_mask.cpu().numpy(), structure=structuring_element).astype(np.uint8)
    plabelpoints = torch.nonzero(torch.from_numpy(eroded_image))
    if len(plabelpoints) > 0:
        pdis = ((plabelpoints - torch.tensor([_point[0][1], _point[0][0]] ,device=plabelpoints.device)) ** 2).sum(-1)
        _, sorted_indices = torch.sort(pdis)
        npoint = plabelpoints[sorted_indices[0]]
        _point.append([npoint[1], npoint[0]])
        _label.append(1)

    return _point, _label

def run(config_file: Optional[Union[str, Sequence[str]]] = None, **override):
    # Initialize distributed and scale parameters based on GPU memory
    if torch.cuda.device_count() > 1:
        dist.init_process_group(
            backend="nccl", init_method="env://", timeout=timedelta(seconds=10000)
        )
        world_size = dist.get_world_size()
        dist.barrier()
    else:
        world_size = 1
    
    # use bfloat16
    torch.autocast(device_type="cuda", dtype=torch.bfloat16).__enter__()

    if torch.cuda.get_device_properties(0).major >= 8:
        # turn on tfloat32 for Ampere GPUs (https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices)
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    if isinstance(config_file, str) and "," in config_file:
        config_file = config_file.split(",")

    _args = _update_args(config_file=config_file, **override)
    config_file_ = _pop_args(_args, "config_file")[0]

    parser = ConfigParser()
    parser.read_config(config_file_)
    parser.update(pairs=_args)

    sam2_checkpoint = parser.get_parsed_content("ckpt")
    model_cfg = parser.get_parsed_content("model_cfg")
    data_file_base_dir = parser.get_parsed_content("data_file_base_dir")
    data_list_file_path = parser.get_parsed_content("data_list_file_path")
    fold = parser.get_parsed_content("fold")
    label_set = parser.get_parsed_content("label_set", default=None)
    transforms_infer = parser.get_parsed_content("transforms_infer")
    list_key = parser.get_parsed_content("list_key", default="testing")
    five_fold = parser.get_parsed_content("five_fold", default=True)
    remove_out = parser.get_parsed_content("remove_out", default=True)
    use_center = parser.get_parsed_content("use_center", default=True)
    output_path = parser.get_parsed_content("output_path")
    dataset_name = parser.get_parsed_content("dataset_name", default=None)
    MAX_ITER = parser.get_parsed_content("max_iter", default=1)

    if label_set is None:
        label_mapping = parser.get_parsed_content(
            "label_mapping", default="./data/jsons/label_mappings.json"
        )
        with open(label_mapping, "r") as f:
            label_mapping = json.load(f)
        label_set = [0] + [_xx[0] for _xx in label_mapping[dataset_name]]

    random_seed = parser.get_parsed_content("random_seed", default=0)
    if random_seed is not None and (
        isinstance(random_seed, int) or isinstance(random_seed, float)
    ):
        set_determinism(seed=random_seed)

    CONFIG["handlers"]["file"]["filename"] = parser.get_parsed_content(
        "log_output_file"
    )
    logging.config.dictConfig(CONFIG)
    logging.getLogger("torch.distributed.distributed_c10d").setLevel(logging.WARNING)
    logger.debug(f"Number of GPUs: {torch.cuda.device_count()}")
    logger.debug(f"World_size: {world_size}")
    if five_fold:
        train_files, val_files = datafold_read(
            datalist=data_list_file_path,
            basedir=data_file_base_dir,
            fold=fold,
            key="training",
        )
        test_files, _ = datafold_read(
            datalist=data_list_file_path,
            basedir=data_file_base_dir,
            fold=-1,
            key="testing",
        )
    else:
        train_files, _ = datafold_read(
            datalist=data_list_file_path,
            basedir=data_file_base_dir,
            fold=-1,
            key="training",
        )
        val_files, _ = datafold_read(
            datalist=data_list_file_path,
            basedir=data_file_base_dir,
            fold=-1,
            key="validation",
        )
        test_files, _ = datafold_read(
            datalist=data_list_file_path,
            basedir=data_file_base_dir,
            fold=-1,
            key="testing",
        )
    process_dict = {
        "training": train_files,
        "validation": val_files,
        "testing": test_files,
        "all": train_files + val_files + test_files,
    }
    process_files = process_dict[list_key]
    for i in range(len(process_files)):
        if (
            isinstance(process_files[i]["image"], list)
            and len(process_files[i]["image"]) > 1
        ):
            process_files[i]["image"] = process_files[i]["image"][0]
    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
        print(f"Total files {len(process_files)}")
        print(process_files)
    if torch.cuda.device_count() > 1:
        process_files = partition_dataset(
            data=process_files,
            shuffle=False,
            num_partitions=world_size,
            even_divisible=False,
        )[dist.get_rank()]
    logger.debug(f"Val_files: {len(process_files)}")
    val_ds = monai.data.Dataset(data=process_files, transform=transforms_infer)
    val_loader = DataLoader(
        val_ds,
        num_workers=parser.get_parsed_content("num_workers_validation", default=2),
        batch_size=1,
        shuffle=False,
    )

    device = (
        torch.device(f"cuda:{os.environ['LOCAL_RANK']}")
        if world_size > 1
        else torch.device("cuda:0")
    )

    predictor = build_sam2_video_predictor(model_cfg, sam2_checkpoint)

    predictor = predictor.to(device)

    post_pred = transforms.AsDiscrete(threshold=0.0, dtype=torch.uint8)

    max_iters = MAX_ITER
    metric_dim = len(label_set) - 1
    log_string = []
    with torch.no_grad():
        obj_num = len(val_loader)
        if torch.cuda.device_count() > 1:
            size_tensor = torch.tensor(obj_num, device=device)
            output_tensor = [torch.zeros_like(size_tensor) for _ in range(world_size)]
            dist.barrier()
            dist.all_gather(output_tensor, size_tensor)
            obj_num = max(output_tensor)
        metric = (
            torch.zeros(
                obj_num, metric_dim, max_iters, dtype=torch.float, device=device
            )
            + torch.nan
        )

        _index = 0
        for val_data in val_loader:
            val_filename = val_data["image"].meta["filename_or_obj"][0]
            _index += 1
            name_parts = val_filename.split("/")
            video_dir=os.path.join(output_path, dataset_name, 
                                       name_parts[-2]+ "_" + name_parts[-1].split(".")[0])
            save_nifti_frames_to_jpg(val_data["image"], video_dir)
            # scan all the JPEG frame names in this directory
            frame_names = [
                p for p in os.listdir(video_dir)
                if os.path.splitext(p)[-1] in [".jpg", ".jpeg", ".JPG", ".JPEG"]
            ]
            frame_names.sort(key=lambda p: int(os.path.splitext(p)[0]))
            inference_state = predictor.init_state(video_path=video_dir)
            # loop through the label_set
            for i in range(1, len(label_set)):
                predictor.reset_state(inference_state)
                label_index = label_set[i]
                label = torch.squeeze((val_data["label"] == label_index).to(torch.uint8))
                for idx in range(max_iters):
                    if idx == 0:
                        predictor.reset_state(inference_state)
                        # select initial points from the center of ROI
                        point = get_points_from_label(label)
                        _point = [[point[1], point[0]]]
                        _label = [1]
                        ann_frame_idx = point[-1]
                        ann_obj_id = 1 
                        points = np.array(_point, dtype=np.float32)
                        labels = np.array(_label, np.int32)
                        _, out_obj_ids, out_mask_logits = predictor.add_new_points(
                            inference_state=inference_state,
                            frame_idx=ann_frame_idx,
                            obj_id=ann_obj_id,
                            points=points,
                            labels=labels,
                        )
                        pred = (out_mask_logits[0] > 0.0).cpu()[0]
                        gt = label[:,:,point[-1]] == 1
                        _point, _label = get_points_from_false_pred(pred, gt, _point, _label)
                       
                    else:
                        predictor.reset_state(inference_state)
                        # select points from the slice with smallest dice
                        point = get_points_from_label(label[..., lowerest_dice_index])

                        # select initial points from the center of ROI
                        point = get_points_from_label(label)
                        _point_additional = [[point[1], point[0]]]
                        _label_additional = [1]
                        ann_frame_idx = point[-1]
                        ann_obj_id = 1 
                        points = np.array(_point, dtype=np.float32)
                        labels = np.array(_label, np.int32)
                        _, out_obj_ids, out_mask_logits = predictor.add_new_points(
                            inference_state=inference_state,
                            frame_idx=ann_frame_idx,
                            obj_id=ann_obj_id,
                            points=points,
                            labels=labels,
                        )
                        pred = (out_mask_logits[0] > 0.0).cpu()[0]
                        gt = label[:,:,point[-1]] == 1
                        _point += _point_additional
                        _label += _label_additional
                        _point, _label = get_points_from_false_pred(pred, gt, _point, _label)

                    points = np.array(_point, dtype=np.float32)
                    labels = np.array(_label, np.int32)
                    # run propagation throughout the video and collect the results in a dict
                    predictor.reset_state(inference_state)
                    # The add_new_points must rerun to reset the state
                    _, out_obj_ids, out_mask_logits = predictor.add_new_points(
                        inference_state=inference_state,
                        frame_idx=ann_frame_idx,
                        obj_id=ann_obj_id,
                        points=points,
                        labels=labels,
                    )
                    video_segments = {}  # video_segments contains the per-frame segmentation results
                    for out_frame_idx, out_obj_ids, out_mask_logits in predictor.propagate_in_video(inference_state, reverse=False):
                        video_segments[out_frame_idx] = {
                            out_obj_id: (out_mask_logits[i] > 0.0).cpu().numpy()
                            for i, out_obj_id in enumerate(out_obj_ids)
                        }
                    for out_frame_idx, out_obj_ids, out_mask_logits in predictor.propagate_in_video(inference_state, reverse=True):
                        video_segments[out_frame_idx] = {
                            out_obj_id: (out_mask_logits[i] > 0.0).cpu().numpy()
                            for i, out_obj_id in enumerate(out_obj_ids)
                        }
                    ####
                    pred = [video_segments[i][ann_obj_id][0] for i in sorted(list(video_segments.keys()))]
                    pred = torch.from_numpy(np.stack(pred).transpose(1,2,0))

                    # compute per-frame dice
                    lowerest_dice = 1000
                    for d in range(pred.shape[-1]):
                        if torch.sum(label[..., d]) > 0:
                            pt_frame_dice = compute_dice(
                                y_pred=pred[..., d].unsqueeze(0).unsqueeze(0), 
                                y=label[..., d].unsqueeze(0).unsqueeze(0),
                                include_background=False
                            )
                            if pt_frame_dice < lowerest_dice:
                                lowerest_dice_index = d
                                lowerest_dice = pt_frame_dice

                    # compue volume dice
                    pt_volume_dice = compute_dice(
                            y_pred=pred.unsqueeze(0).unsqueeze(0), 
                            y=label.unsqueeze(0).unsqueeze(0),
                            include_background=False
                        )
                    
                    print(f"iter {idx}, pt_volume_dice", pt_volume_dice)

                    metric[_index - 1, i - 1, idx] = pt_volume_dice

                    string = f"Validation Dice score : {idx} / {_index} / {len(val_loader)}/ {val_filename}: {metric[_index-1,:,idx]}"
                    print(string)
                    log_string.append(string)
                    # move all to cpu to avoid potential out memory in invert transform
                    torch.cuda.empty_cache()

        log_string = sorted(log_string)
        for _ in log_string:
            logger.debug(_)

        if torch.cuda.device_count() > 1:
            dist.barrier()
            global_combined_tensor = [
                torch.zeros_like(metric) for _ in range(world_size)
            ]
            dist.all_gather(tensor_list=global_combined_tensor, tensor=metric)
            metric = torch.vstack(global_combined_tensor)

        if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
            # remove metric that's all NaN
            keep_index = ~torch.isnan(metric).all(1).all(1)
            metric = metric[keep_index]
            point_num = point_num[keep_index]
            if max_iters > 1:
                metric_best = torch.nan_to_num(metric, 0).max(2)[0]
            else:
                metric_best = metric[:, :, 0]
            for i in range(metric.shape[0]):
                logger.debug(f"object {i}: {metric[i].tolist()}")
                logger.debug(f"object {i}: {metric_best[i].tolist()}")
            print("point_number", point_num, point_num.nanmean(0))
            torch.save(
                {"metric": metric.cpu(), "point": point_num.cpu()},
                parser.get_parsed_content("log_output_file").replace("log", "pt"),
            )
            logger.debug(
                f"Best metric {metric_best.nanmean(0).tolist()}, best avg {metric_best.nanmean(0).nanmean().tolist()}"
            )
            logger.debug(
                f"point needed, {point_num.tolist()}, mean is {point_num.nanmean(0).tolist()}"
            )

    torch.cuda.empty_cache()
    if torch.cuda.device_count() > 1:
        dist.barrier()
        dist.destroy_process_group()

    return


if __name__ == "__main__":
    from monai.utils import optional_import

    fire, _ = optional_import("fire")
    fire.Fire()