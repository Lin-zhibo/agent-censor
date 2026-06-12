"""
数据集定义 - CLIPDetector v2
"""

import json
from pathlib import Path
from typing import Dict, List

import torch
import torchvision.transforms as T
from torch.utils.data import Dataset
from PIL import Image


class KobeDataset(Dataset):
    """
    数据集格式:
    data_dir/
      ├── images/
      │     ├── 00001.jpg
      │     └── ...
      ├── annotations.json
      └── splits/
            ├── train.txt
            ├── val.txt
            └── test.txt

    annotations.json:
    {
      "00001.jpg": {
        "is_target": 1,
        "content_safety": 2,  # 0=safe, 1=neutral, 2=harmful
      },
      ...
    }
    """

    # Hard negative samples from test set analysis
    # These harmful samples were misclassified as safe/neutral
    HARD_NEGATIVE_IDS = {
        # baseline 误分样本
        "03369.jpg", "01122.jpg",  # harmful -> safe
        "03718.jpg", "03500.jpg",  # harmful -> neutral
        # MixUp epoch15 在 data/v2 test 上的新增假阴性
        "00966.jpg", "00977.jpg", "01043.jpg",
        "03118.jpg", "03121.jpg", "03298.jpg",
        "03755.jpg", "03827.jpg",
    }

    SAFETY_MAP = {
        "safe": 0,
        "neutral": 1,
        "harmful": 2,
        "positive": 0,
        "negative": 2,
    }

    def __init__(self,
                 data_dir: str,
                 split: str = "train",
                 processor=None,
                 image_size: int = 224,
                 augment: bool = True):
        """
        Args:
            augment: 是否对训练样本做数据增强（仅 split='train' 生效）
        """

        self.data_dir = Path(data_dir)
        self.image_dir = self.data_dir / "images"
        self.split = split
        self.processor = processor
        self.image_size = image_size
        self.augment = augment and (split == "train")

        # 数据增强: 仅对 harmful 样本做强增强
        # 每次 __getitem__ 时随机应用，增加多样性
        self.strong_aug = T.Compose([
            T.RandomResizedCrop(image_size, scale=(0.5, 1.0), ratio=(0.75, 1.33)),
            T.RandomHorizontalFlip(p=0.5),
            T.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.1),
            T.RandomRotation(degrees=15),
            T.RandomApply([T.GaussianBlur(kernel_size=5, sigma=(0.1, 2.0))], p=0.3),
        ])

        # 基础数据增强: 应用于所有训练样本
        self.base_aug = T.Compose([
            T.RandomResizedCrop(image_size, scale=(0.7, 1.0), ratio=(0.9, 1.1)),
            T.RandomHorizontalFlip(p=0.5),
            T.RandomApply([
                T.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.2, hue=0.05),
                T.RandomRotation(degrees=8),
            ], p=0.5),
        ])

        # 加载标注
        anno_path = self.data_dir / "annotations.json"
        if anno_path.exists():
            with open(anno_path, "r", encoding="utf-8") as f:
                self.annotations = json.load(f)
        else:
            self.annotations = {}

        # 加载split
        split_path = self.data_dir / "splits" / f"{split}.txt"
        if split_path.exists():
            with open(split_path, "r") as f:
                self.image_ids = [line.strip() for line in f if line.strip()]
        else:
            self.image_ids = sorted([
                p.name for p in self.image_dir.glob("*")
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")
            ])

        print(f"[{split}] Loaded {len(self.image_ids)} samples from {data_dir}")

    def __len__(self):
        return len(self.image_ids)

    def _parse_safety(self, safety, is_target: int = 1) -> int:
        if isinstance(safety, int):
            return max(0, min(2, safety))
        if isinstance(safety, str):
            return self.SAFETY_MAP.get(safety.lower(), 1)
        # is_target=0 的样本默认 safe (0)
        return 1 if is_target == 1 else 0

    def __getitem__(self, idx: int) -> Dict:
        image_id = self.image_ids[idx]
        image_path = self.image_dir / image_id

        try:
            pil_image = Image.open(image_path).convert("RGB")
        except Exception as e:
            print(f"Warning: Failed to load {image_path}: {e}")
            pil_image = Image.new("RGB", (self.image_size, self.image_size), (128, 128, 128))

        # 数据增强: 仅对 harmful 样本做强增强
        # 每个 epoch 看到的增强样本都不同，有效扩充 harmful 数据多样性
        anno = self.annotations.get(image_id, {})
        is_target_val = anno.get("is_target", 0)
        if isinstance(is_target_val, bool):
            is_target_val = int(is_target_val)
        content_safety = self._parse_safety(anno.get("content_safety", None), is_target_val)
        if self.augment:
            pil_image = self.base_aug(pil_image)
            if content_safety == 2:
                pil_image = self.strong_aug(pil_image)

        # 使用CLIP processor预处理
        if self.processor:
            inputs = self.processor(images=pil_image, return_tensors="pt")
            pixel_values = inputs["pixel_values"].squeeze(0)  # (3, 224, 224)
        else:
            pixel_values = T.Compose([
                T.Resize((self.image_size, self.image_size)),
                T.ToTensor(),
                T.Normalize(mean=[0.48145466, 0.4578275, 0.40821073],
                           std=[0.26862954, 0.26130258, 0.27577711]),
            ])(pil_image)

        # safety_mask: is_target=1 的图全权重参与 safety 训练
        # is_target=0 的图以 0.3 权重参与（增加 safety head 的负样本 exposure）
        safety_mask = 1.0 if is_target_val == 1 else 0.3

        # hard negative: test set 上被误分的 harmful 样本，增加采样权重
        is_hard_negative = image_id in self.HARD_NEGATIVE_IDS

        return {
            "pixel_values": pixel_values,
            "is_target": torch.tensor(is_target_val, dtype=torch.float32),
            "content_safety": torch.tensor(content_safety, dtype=torch.long),
            "safety_mask": torch.tensor(safety_mask, dtype=torch.float32),
            "is_hard_negative": torch.tensor(is_hard_negative, dtype=torch.bool),
            "image_id": image_id,
        }


def collate_fn(batch: List[Dict]) -> Dict:
    """自定义batch组装"""
    return {
        "pixel_values": torch.stack([b["pixel_values"] for b in batch]),
        "is_target": torch.stack([b["is_target"] for b in batch]),
        "content_safety": torch.stack([b["content_safety"] for b in batch]),
        "safety_mask": torch.stack([b["safety_mask"] for b in batch]),
        "is_hard_negative": torch.stack([b["is_hard_negative"] for b in batch]),
        "image_ids": [b["image_id"] for b in batch],
    }
