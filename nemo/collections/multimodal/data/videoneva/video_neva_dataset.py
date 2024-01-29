# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import json
import logging
import os
import re
import tarfile
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Union

import cv2
import numpy as np
import torch
import torch.nn.functional as F
import transformers
from einops import rearrange
from omegaconf import DictConfig
from PIL import Image
from torch.utils.data import Dataset, default_collate
from transformers import CLIPImageProcessor

import nemo.collections.multimodal.data.neva.conversation as conversation_lib
from nemo.collections.multimodal.data.clip.augmentations.augmentations import image_transform
from nemo.collections.multimodal.data.neva.conversation import (
    DEFAULT_BOS_TOKEN,
    DEFAULT_EOS_TOKEN,
    DEFAULT_IM_END_TOKEN,
    DEFAULT_IM_START_TOKEN,
    DEFAULT_IMAGE_PATCH_TOKEN,
    DEFAULT_IMAGE_TOKEN,
    DEFAULT_LABELS_TOKEN,
    DEFAULT_PAD_TOKEN,
    DEFAULT_SEPARATOR_TOKEN,
    DEFAULT_SYSTEM_TOKEN,
    DEFAULT_UNK_TOKEN,
)
from nemo.collections.multimodal.data.neva.neva_dataset import TarOrFolderImageLoader
from nemo.collections.nlp.modules.common.megatron.utils import get_ltor_masks_and_position_ids

DEFAULT_VIDEO_TOKEN = "<video>"
DEFAULT_VIDEO_PATCH_TOKEN = "<vid_patch>"
DEFAULT_VID_START_TOKEN = "<vid_start>"
DEFAULT_VID_END_TOKEN = "<vid_end>"

MAX_NUM_IMAGES = 1
MAX_NUM_VIDEOS = 1
IGNORE_INDEX = -1


class TarOrFolderVideoLoader:
    """
        A class for loading videos from a tar archive or a regular folder.

        This class provides functionality to open and read videos from either a tar archive
        (.tar file) or a standard directory with video files. It builds an index of
        videos if the source is a tar archive for efficient access.

        Attributes:
            video_folder (str): The path to the tar archive or video folder.
            tar_index (dict): A dictionary that maps file names to their tarfile member
                              objects if the video source is a tar archive.

        Methods:
            __init__(self, image_folder): Initializes the loader with the specified video folder.
            build_index(self): Builds an index of video file names and their corresponding
                               tarfile member objects for a tar archive.
            open_video(self, file_name): Opens and returns a video by its file name. The video
                                         is returned as an OpenCV VideoCapture object.
        """

    def __init__(self, video_folder):
        self.video_folder = video_folder
        self.tar_index = {}
        if self.video_folder.endswith('.tar'):
            self.build_index()

    def build_index(self):
        with tarfile.open(self.video_folder, 'r') as tar:
            for member in tar.getmembers():
                self.tar_index[member.name] = member

    def open_video(self, file_name):
        if self.video_folder.endswith('.tar'):
            with tarfile.open(self.video_folder, 'r') as tar:
                member = self.tar_index.get(file_name)
                if member:
                    f = tar.extractfile(member)
                    video_data = np.frombuffer(f.read(), dtype=np.uint8)
                    return cv2.imdecode(video_data, cv2.IMREAD_UNCHANGED)
        else:
            return cv2.VideoCapture(os.path.join(self.video_folder, file_name))
        return None



def preprocess_multimodal(sources: dict, multimodal_cfg: dict, cur_token_len: int, use_plain: bool = False) -> Dict:
    """
    Preprocesses multimodal sources based on the provided configuration.

    This function modifies the sources for multimodal data processing. It checks if the data is multimodal and
    adjusts the token lengths accordingly. It also handles the start and end tokens for images and replaces
    image tokens in conversations.

    Parameters:
    - sources (dict): A dictionary containing the multimodal sources to be processed.
    - multimodal_cfg (dict): A configuration dictionary specifying various options for multimodal processing.
      It includes keys like 'is_multimodal', 'use_im_start_end', and 'sep_image_conv_front'.
    - cur_token_len (int): The current length of tokens to be considered for image processing.
    - use_plain (bool, optional): A boolean flag to use plain image token replacement without additional processing.
      Defaults to False.

    Returns:
    - dict: The processed sources dictionary after applying multimodal preprocessing steps.
    """
    is_multimodal = multimodal_cfg['is_multimodal']
    image_token_len = cur_token_len
    if not is_multimodal:
        return sources

    # <video> := `num_frames` * <image>
    if multimodal_cfg['use_im_start_end']:
        replace_token = DEFAULT_IMAGE_PATCH_TOKEN * image_token_len
        vid_replace_token = replace_token * multimodal_cfg['num_frames']
    else:
        replace_token = DEFAULT_IMAGE_PATCH_TOKEN * (image_token_len - 2)
        vid_replace_token = replace_token * multimodal_cfg['num_frames']
    replace_token = DEFAULT_IM_START_TOKEN + replace_token + DEFAULT_IM_END_TOKEN
    vid_replace_token = DEFAULT_VID_START_TOKEN + vid_replace_token + DEFAULT_IM_END_TOKEN

    for source in sources:
        conversation = source['conversations']
        if multimodal_cfg['sep_image_conv_front']:
            assert DEFAULT_IMAGE_TOKEN in conversation[0]['value']
            IMAGE_TOKEN_NUM = conversation[0]['value'].count(DEFAULT_IMAGE_TOKEN)
            VIDEO_TOKEN_NUM = conversation[0]['value'].count(DEFAULT_VIDEO_TOKEN)
            if IMAGE_TOKEN_NUM > VIDEO_TOKEN_NUM:
                DEFAULT_TOKEN = DEFAULT_IMAGE_TOKEN
            else:
                DEFAULT_TOKEN = DEFAULT_VIDEO_TOKEN

            conversation[0]['value'] = conversation[0]['value'].replace(DEFAULT_TOKEN, '').strip()
            conversation[0]['value'] = (
                DEFAULT_TOKEN
                + conversation_lib.default_conversation.sep
                + conversation_lib.default_conversation.roles[0]
                + ": "
                + conversation[0]['value']
            )
        if use_plain:
            assert DEFAULT_IMAGE_TOKEN in conversation[0]['value']
            conversation[0]['value'] = DEFAULT_IMAGE_TOKEN
        for turn in conversation:
            turn["value"] = turn["value"].replace(DEFAULT_IMAGE_TOKEN, replace_token)
            turn["value"] = turn["value"].replace(DEFAULT_VIDEO_TOKEN, vid_replace_token)
    return sources
