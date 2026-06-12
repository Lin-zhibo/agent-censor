#!/usr/bin/env python3
"""
使用 CLIP zero-shot 重新为所有图片标注 is_target 标签。

不再依赖目录假设（safe=人物、neutral=非人物、harmful=人物），
而是直接用 CLIP 文本 prompt 与图像的相似度来判断是否包含目标人物。
"""

import argparse
import json
import shutil
from pathlib import Path

import torch
from tqdm import tqdm

from models.clip_detector import CLIPDetector


def relabel_identity(data_dir: str, batch_size: int = 64, device: str = "cuda"):
    data_dir = Path(data_dir)
    annot_path = data_dir / "annotations.json"

    with open(annot_path, "r", encoding="utf-8") as f:
        annotations = json.load(f)

    image_ids = list(annotations.keys())
    print(f"Relabeling {len(image_ids)} images with zero-shot CLIP identity prompts...")

    model = CLIPDetector(device=device)
    model.eval()

    identity_changed = 0
    stats = {"target": 0, "non_target": 0}

    with torch.no_grad():
        for i in tqdm(range(0, len(image_ids), batch_size), desc="Relabeling"):
            batch_ids = image_ids[i : i + batch_size]
            img_paths = [str(data_dir / "images" / img_id) for img_id in batch_ids]

            pixel_values = model.preprocess_images(img_paths)
            outputs = model.predict(pixel_values)
            probs = outputs["is_target"].cpu().numpy()

            for img_id, prob in zip(batch_ids, probs):
                old_label = annotations[img_id].get("is_target", None)
                new_label = 1 if float(prob) > model.identity_threshold else 0
                annotations[img_id]["is_target"] = new_label
                annotations[img_id]["is_target_score"] = round(float(prob), 6)

                if old_label is not None and old_label != new_label:
                    identity_changed += 1

                stats["target" if new_label == 1 else "non_target"] += 1

    # 备份原标注
    backup_path = annot_path.with_suffix(".json.bak")
    shutil.copy2(annot_path, backup_path)
    print(f"[Backup] Original annotations saved to {backup_path}")

    with open(annot_path, "w", encoding="utf-8") as f:
        json.dump(annotations, f, indent=2, ensure_ascii=False)

    print(f"[Done] Updated {annot_path}")
    print(f"  Target:     {stats['target']}")
    print(f"  Non-target: {stats['non_target']}")
    print(f"  Changed:    {identity_changed} ({identity_changed / len(image_ids) * 100:.2f}%)")


def main():
    parser = argparse.ArgumentParser(description="Relabel is_target with CLIP zero-shot")
    parser.add_argument("--data_dir", type=str, default="../data_prepared",
                        help="包含 images/ 和 annotations.json 的目录")
    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--device", type=str, default="cuda")
    args = parser.parse_args()

    relabel_identity(args.data_dir, args.batch_size, args.device)


if __name__ == "__main__":
    main()
