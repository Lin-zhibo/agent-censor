#!/usr/bin/env python3
"""
快速验证训练流程 - 小批量数据 + 少轮数
用法: conda run -n regqav python quick_verify.py
"""

import json
import random
import shutil
from pathlib import Path

import torch
from torch.utils.data import DataLoader

from models.clip_detector import CLIPDetector
from models.lora_config import apply_lora_to_clip, get_clip_lora_config
from utils.dataset import KobeDataset, collate_fn
from utils.metrics import compute_metrics
from models.losses import MultiTaskLoss


def create_mini_dataset(data_dir: str, output_dir: str, samples_per_class: int = 50):
    """从完整数据集采样小批量数据用于快速验证"""
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    image_dir = output_dir / "images"
    image_dir.mkdir(exist_ok=True)
    splits_dir = output_dir / "splits"
    splits_dir.mkdir(exist_ok=True)

    with open(data_dir / "annotations.json", "r", encoding="utf-8") as f:
        annotations = json.load(f)

    # 按类别分组
    class_images = {0: [], 1: [], 2: []}  # safe, neutral, harmful
    for img_id, anno in annotations.items():
        cls = anno["content_safety"]
        class_images[cls].append(img_id)

    # 每类采样
    selected = []
    new_annotations = {}
    class_names = {0: "safe", 1: "neutral", 2: "harmful"}

    for cls, names in class_names.items():
        imgs = class_images[cls]
        sampled = random.sample(imgs, min(samples_per_class, len(imgs)))
        for img_id in sampled:
            src = data_dir / "images" / img_id
            dst = image_dir / img_id
            shutil.copy2(src, dst)
            new_annotations[img_id] = annotations[img_id]
            selected.append(img_id)
        print(f"[Sample] {names}: {len(sampled)} images")

    # 全部作为训练集（快速验证不做划分）
    with open(splits_dir / "train.txt", "w", encoding="utf-8") as f:
        for img_id in selected:
            f.write(img_id + "\n")

    with open(output_dir / "annotations.json", "w", encoding="utf-8") as f:
        json.dump(new_annotations, f, indent=2, ensure_ascii=False)

    print(f"[Done] Mini dataset: {len(selected)} images at {output_dir}")
    return output_dir


def quick_train(data_dir: str, epochs: int = 3, batch_size: int = 8):
    """快速训练验证"""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'='*50}")
    print(f"Quick Verify Training")
    print(f"Device: {device}")
    print(f"Epochs: {epochs}")
    print(f"Batch size: {batch_size}")
    print(f"{'='*50}\n")

    # 加载模型
    print("[1/4] Loading CLIP model...")
    model = CLIPDetector(device=str(device))
    lora_config = get_clip_lora_config(r=8, lora_alpha=16)
    model.clip = apply_lora_to_clip(model.clip, lora_config)
    model = model.to(device)

    # 损失函数
    criterion = MultiTaskLoss(
        lambda_identity=0.5,
        lambda_safety=1.0,
        safety_loss_type="focal",
        safety_alpha=[0.5, 1.0, 5.0],
        safety_gamma=2.0,
    ).to(device)

    # 优化器
    trainable = [p for p in model.parameters() if p.requires_grad]
    print(f"[2/4] Trainable params: {sum(p.numel() for p in trainable):,}")
    optimizer = torch.optim.AdamW(trainable, lr=1e-4, weight_decay=0.01)

    # 数据
    print("[3/4] Loading dataset...")
    dataset = KobeDataset(data_dir=data_dir, split="train", processor=model.processor)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, collate_fn=collate_fn)
    print(f"  Batches: {len(loader)}")

    # 训练
    print(f"[4/4] Training...\n")
    model.train()

    for epoch in range(1, epochs + 1):
        total_loss = 0.0
        all_is_pred, all_is_gt = [], []
        all_safety_pred, all_safety_gt = [], []

        for batch_idx, batch in enumerate(loader):
            pixel_values = batch["pixel_values"].to(device)
            is_target = batch["is_target"].to(device)
            safety = batch["content_safety"].to(device)

            optimizer.zero_grad()
            outputs = model(pixel_values)

            loss_dict = criterion(
                outputs["is_target_logits"],
                outputs["safety_logits"],
                is_target,
                safety,
            )

            loss_dict["total"].backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()

            total_loss += loss_dict["total"].item()

            with torch.no_grad():
                is_pred = (torch.sigmoid(outputs["is_target_logits"]) > 0.5).cpu().numpy()
                safety_pred = outputs["safety_logits"].argmax(dim=-1).cpu().numpy()
                all_is_pred.extend(is_pred)
                all_is_gt.extend(is_target.cpu().numpy())
                all_safety_pred.extend(safety_pred)
                all_safety_gt.extend(safety.cpu().numpy())

            if (batch_idx + 1) % 5 == 0:
                print(f"  Epoch {epoch} | Batch {batch_idx+1}/{len(loader)} | Loss: {loss_dict['total'].item():.4f}")

        metrics = compute_metrics(all_is_pred, all_is_gt, all_safety_pred, all_safety_gt)
        print(f"\n[Epoch {epoch} Summary]")
        print(f"  Loss: {total_loss / len(loader):.4f}")
        print(f"  Identity F1: {metrics['identity_f1']:.4f}")
        print(f"  Safety F1: {metrics['safety_macro_f1']:.4f}")
        print(f"  Harmful F1: {metrics['safety_harmful_f1']:.4f}")
        print(f"  Harmful Recall: {metrics['safety_harmful_recall']:.4f}")
        print("-" * 40)

    print("\n[Done] Quick verify completed successfully!")
    print("Training pipeline is ready.")


def main():
    random.seed(42)
    torch.manual_seed(42)

    # 创建小批量数据集
    mini_dir = Path("../data_mini")
    if not (mini_dir / "annotations.json").exists():
        create_mini_dataset("../data_prepared", mini_dir, samples_per_class=50)
    else:
        print(f"[Skip] Mini dataset already exists at {mini_dir}")

    # 快速训练
    quick_train(str(mini_dir), epochs=3, batch_size=8)


if __name__ == "__main__":
    main()
