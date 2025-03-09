# Copyright (c) OpenMMLab. All rights reserved.
import copy
import json
import logging
import os

import torch
from datasets import Dataset as HFDataset
from datasets import DatasetDict, load_from_disk
from mmengine import print_log
from mmengine.config import Config, ConfigDict
from mmengine.utils.misc import get_object_from_string
from PIL import Image
from torch.utils.data import Dataset

from xtuner.registry import BUILDER, MAP_FUNC

from .huggingface import process_hf_dataset
from .utils import encode_fn, expand2square


def load_jsonl(json_file):
    with open(json_file) as f:
        lines = f.readlines()
    data = []
    for line in lines:
        data.append(json.loads(line))
    return data


class LLaVADataset(Dataset):
    def __init__(
        self,
        image_folder,
        image_processor,
        data_path=None,
        tokenizer=None,
        offline_processed_text_folder=None,
        max_dataset_length=None,
        dataset_map_fn=None,
        template_map_fn=None,
        max_length=2048,
        pad_image_to_square=False,
        preprocess_text_data=True,
    ):
        super().__init__()

        assert offline_processed_text_folder or (data_path and tokenizer)
        if offline_processed_text_folder and data_path:
            print_log(
                "Both `offline_processed_text_folder` and "
                "`data_path` are set, and we load dataset from"
                "`offline_processed_text_folder` "
                f"({offline_processed_text_folder})",
                logger="current",
                level=logging.WARNING,
            )

        if offline_processed_text_folder is not None:
            self.data = load_from_disk(offline_processed_text_folder)
        else:
            if data_path.endswith(".json"):
                json_data = json.load(open(data_path))
            elif data_path.endswith(".jsonl"):
                json_data = load_jsonl(data_path)
            else:
                raise NotImplementedError

            if preprocess_text_data:
                for idx in range(len(json_data)):
                    if isinstance(json_data[idx]["id"], int):
                        json_data[idx]["id"] = str(json_data[idx]["id"])
                json_data = DatasetDict({"train": HFDataset.from_list(json_data)})
                text_data = process_hf_dataset(
                    dataset=json_data,
                    tokenizer=tokenizer,
                    max_length=max_length,
                    dataset_map_fn=dataset_map_fn,
                    template_map_fn=template_map_fn,
                    split="train",
                    max_dataset_length=max_dataset_length,
                    remove_unused_columns=False,
                    pack_to_max_length=False,
                    with_image_token=True,
                )
                self.data = text_data
            else:
                if isinstance(tokenizer, dict) or isinstance(tokenizer, Config) or isinstance(tokenizer, ConfigDict):
                    tokenizer = BUILDER.build(tokenizer)

                if isinstance(dataset_map_fn, str):
                    map_fn_obj = MAP_FUNC.get(dataset_map_fn) or get_object_from_string(dataset_map_fn)
                    if map_fn_obj is not None:
                        dataset_map_fn = map_fn_obj
                    else:
                        raise TypeError(
                            "dataset_map_fn must be a function or a "
                            "registered function's string in MAP_FUNC, "
                            f"but got a string of '{dataset_map_fn}'"
                        )

                if (
                    isinstance(template_map_fn, dict)
                    or isinstance(template_map_fn, Config)
                    or isinstance(template_map_fn, ConfigDict)
                ):
                    template_map_fn = BUILDER.build(template_map_fn)

                self.dataset_map_fn = dataset_map_fn
                self.template_map_fn = template_map_fn
                self.tokenizer = tokenizer
                self.data = json_data

        self.max_length = max_length
        self.preprocess_text_data = preprocess_text_data

        self.image_folder = image_folder
        if (
            isinstance(image_processor, dict)
            or isinstance(image_processor, Config)
            or isinstance(image_processor, ConfigDict)
        ):
            self.image_processor = BUILDER.build(image_processor)
        else:
            self.image_processor = image_processor
        self.pad_image_to_square = pad_image_to_square

    @property
    def modality_length(self):
        length_list = []
        for data_dict in self.data:
            if self.preprocess_text_data:
                cur_len = len(data_dict["input_ids"])
            else:
                cur_len = sum(len(conv["value"].split()) for conv in data_dict["conversations"])
            if data_dict.get("image", None) is None:
                cur_len = -cur_len
            length_list.append(cur_len)
        return length_list

    def __len__(self):
        return len(self.data)

    def process_text_data(self, data_dict, with_image_token=True):
        if self.preprocess_text_data:
            return data_dict

        if self.dataset_map_fn is not None:
            data_dict = self.dataset_map_fn(data_dict)
        if self.template_map_fn is not None:
            data_dict = self.template_map_fn(data_dict)
        data_dict = encode_fn(data_dict, self.tokenizer, self.max_length, True, with_image_token)
        return data_dict

    def __getitem__(self, index):
        data_dict = copy.deepcopy(self.data[index])
        if data_dict.get("image", None) is not None:
            image_file = data_dict["image"]
            image = Image.open(os.path.join(self.image_folder, image_file)).convert("RGB")
            if self.pad_image_to_square:
                image = expand2square(image, tuple(int(x * 255) for x in self.image_processor.image_mean))
            image = self.image_processor.preprocess(image, return_tensors="pt")["pixel_values"][0]
            data_dict["pixel_values"] = image
            data_dict.update(self.process_text_data(data_dict, with_image_token=True))
        else:
            if hasattr(self.image_processor, "crop_size"):
                crop_size = self.image_processor.crop_size
            else:
                crop_size = self.image_processor.size
            data_dict["pixel_values"] = torch.zeros(3, crop_size["height"], crop_size["width"])
            data_dict.update(self.process_text_data(data_dict, with_image_token=False))
        return data_dict
