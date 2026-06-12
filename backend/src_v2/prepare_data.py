#!/usr/bin/env python3
"""
数据准备脚本
将 data/{safe,neutral,harmful}/*.jpg 转换为训练格式
"""

import argparse
import json
import random
import shutil
from pathlib import Path


def prepare_data(data_root: str, output_dir: str, seed: int = 42):
    """
    将目录结构数据转换为训练格式

    Input:
        data_root/safe/*.jpg      -> is_target=1, safety=0
        data_root/neutral/*.jpg   -> is_target=0, safety=1
        data_root/harmful/*.jpg   -> is_target=1, safety=2

    Output:
        output_dir/images/*.jpg
        output_dir/annotations.json
        output_dir/splits/{train,val,test}.txt
    """
    data_root = Path(data_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    image_dir = output_dir / "images"
    image_dir.mkdir(exist_ok=True)

    splits_dir = output_dir / "splits"
    splits_dir.mkdir(exist_ok=True)

    random.seed(seed)

    # 目录到标签映射
    label_map = {
        "safe": {"is_target": 1, "content_safety": 0},
        "neutral": {"is_target": 0, "content_safety": 1},
        "harmful": {"is_target": 1, "content_safety": 2},
    }

    annotations = {}
    all_ids = []
    category_counts = {"safe": 0, "neutral": 0, "harmful": 0}

    for category, labels in label_map.items():
        src_dir = data_root / category
        if not src_dir.exists():
            print(f"[WARN] Directory not found: {src_dir}")
            continue

        for src_path in sorted(src_dir.glob("*.jpg")):
            # 统一命名: category_序号.jpg
            category_counts[category] += 1
            new_id = f"{category}_{category_counts[category]:05d}.jpg"

            # 复制图片
            dst_path = image_dir / new_id
            shutil.copy2(src_path, dst_path)

            # 记录标注
            annotations[new_id] = labels
            all_ids.append(new_id)

        print(f"[Copy] {category}: {category_counts[category]} images")

    # 划分数据集: 80% train, 10% val, 10% test
    random.shuffle(all_ids)
    n_total = len(all_ids)
    n_test = max(1, int(n_total * 0.1))
    n_val = max(1, int(n_total * 0.1))
    n_train = n_total - n_val - n_test

    train_ids = all_ids[:n_train]
    val_ids = all_ids[n_train:n_train + n_val]
    test_ids = all_ids[n_train + n_val:]

    # 保存split文件
    for split_name, ids in [("train", train_ids), ("val", val_ids), ("test", test_ids)]:
        split_path = splits_dir / f"{split_name}.txt"
        with open(split_path, "w", encoding="utf-8") as f:
            for img_id in ids:
                f.write(img_id + "\n")
        print(f"[Split] {split_name}: {len(ids)} samples")

    # 保存标注文件
    with open(output_dir / "annotations.json", "w", encoding="utf-8") as f:
        json.dump(annotations, f, indent=2, ensure_ascii=False)

    # 保存数据统计
    stats = {
        "total": n_total,
        "train": n_train,
        "val": n_val,
        "test": n_test,
        "categories": {
            "safe": category_counts["safe"],
            "neutral": category_counts["neutral"],
            "harmful": category_counts["harmful"],
        },
    }
    with open(output_dir / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print(f"\n[Done] Data prepared at {output_dir}")
    print(f"  Total images: {n_total}")
    print(f"  Train/Val/Test: {n_train}/{n_val}/{n_test}")

    return output_dir


def main():
    parser = argparse.ArgumentParser(description="Prepare training data")
    parser.add_argument("--data_root", type=str, default="../data",
                        help="原始数据目录 (包含 safe/, neutral/, harmful/)")
    parser.add_argument("--output", type=str, default="../data_prepared",
                        help="输出目录")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    prepare_data(args.data_root, args.output, args.seed)


if __name__ == "__main__":
    main()
