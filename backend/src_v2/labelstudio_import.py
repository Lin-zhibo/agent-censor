#!/usr/bin/env python3
"""
生成Label Studio导入文件
支持预填充模型预测结果，标注者可以参考模型输出进行修正

用法:
  # 基础导入（不含模型预测）
  python labelstudio_import.py --image_dir ../data_prepared/images --output ../labelstudio_import.json

  # 带模型预测预填充（推荐）
  python labelstudio_import.py --image_dir ../data_prepared/images \
      --output ../labelstudio_import.json \
      --checkpoint ../checkpoints/best_model.pth \
      --device cuda
"""

import argparse
import json
import sys
from pathlib import Path

import torch
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from inference import CLIPDetectorPredictor


def generate_import_file(
    image_dir: str,
    output_file: str,
    checkpoint_path: str = None,
    device: str = "cuda",
    limit: int = None,
):
    """
    生成Label Studio导入JSON文件

    格式: [{"image": "...", "model_identity": "...", "model_safety": "...", ...}, ...]
    """
    image_dir = Path(image_dir)
    images = sorted(image_dir.glob("*.jpg"))
    if limit:
        images = images[:limit]

    # 可选: 加载模型进行预填充预测
    predictor = None
    if checkpoint_path and Path(checkpoint_path).exists():
        print(f"Loading model from {checkpoint_path} for pre-fill predictions...")
        predictor = CLIPDetectorPredictor(checkpoint_path, device=device)

    tasks = []
    for img_path in tqdm(images, desc="Generating import file"):
        task = {
            "image": str(img_path.resolve()),
        }

        if predictor is not None:
            try:
                pred = predictor.predict(str(img_path))
                task.update({
                    "model_identity": "是Kobe" if pred["is_target_binary"] else "非Kobe",
                    "model_safety": pred["content_type"],
                    "model_risk": f"{pred['risk_score']:.3f} ({pred['risk_level']})",
                    "model_confidence": f"is_target={pred['is_target']:.3f}, "
                                        f"safe={pred['content_probs']['safe']:.3f}, "
                                        f"neutral={pred['content_probs']['neutral']:.3f}, "
                                        f"harmful={pred['content_probs']['harmful']:.3f}",
                })
            except Exception as e:
                print(f"Warning: Failed to predict {img_path}: {e}")
                task.update({
                    "model_identity": "预测失败",
                    "model_safety": "预测失败",
                    "model_risk": "N/A",
                    "model_confidence": "N/A",
                })

        tasks.append(task)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

    print(f"\n[Done] Generated {len(tasks)} tasks")
    print(f"  Output: {output_file}")
    print(f"\nLabel Studio导入步骤:")
    print(f"  1. 启动 Label Studio: label-studio start")
    print(f"  2. 创建新项目")
    print(f"  3. Settings -> Labeling Interface -> 粘贴 docs/label-studio-config.xml 内容")
    print(f"  4. Settings -> Cloud Storage -> 添加 Local Storage")
    print(f"     路径: {image_dir.parent.resolve()}")
    print(f"  5. 导入数据: 上传 {output_file}")


def export_annotations(labelstudio_export: str, output_annotations: str):
    """
    将Label Studio导出的JSON转为训练用的annotations.json格式

    用法:
      python labelstudio_import.py --export ../labelstudio-export.json --output ../annotations_new.json
    """
    with open(labelstudio_export, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    annotations = {}
    for task in tasks:
        image_path = task.get("image", "")
        image_id = Path(image_path).name

        # 解析标注结果
        result = task.get("annotations", [{}])[0].get("result", [])

        is_target = None
        content_safety = None
        harmful_subtype = []
        has_text = None
        confidence = None
        note = ""

        for r in result:
            if r.get("from_name") == "is_target":
                is_target = int(r.get("value", {}).get("choices", ["0"])[0])
            elif r.get("from_name") == "content_safety":
                content_safety = r.get("value", {}).get("choices", ["neutral"])[0]
            elif r.get("from_name") == "harmful_subtype":
                harmful_subtype = r.get("value", {}).get("choices", [])
            elif r.get("from_name") == "has_text":
                has_text = r.get("value", {}).get("choices", ["false"])[0] == "true"
            elif r.get("from_name") == "confidence":
                confidence = r.get("value", 0)
            elif r.get("from_name") == "note":
                note = r.get("value", {}).get("text", "")

        # 只导出完整的标注
        if is_target is not None and content_safety is not None:
            safety_map = {"safe": 0, "neutral": 1, "harmful": 2}
            annotations[image_id] = {
                "is_target": is_target,
                "content_safety": safety_map.get(content_safety, 1),
                "harmful_subtype": harmful_subtype,
                "has_text": has_text,
                "confidence": confidence,
                "note": note,
            }

    with open(output_annotations, "w", encoding="utf-8") as f:
        json.dump(annotations, f, indent=2, ensure_ascii=False)

    print(f"[Done] Exported {len(annotations)} annotations to {output_annotations}")


def main():
    parser = argparse.ArgumentParser(description="Label Studio import/export utilities")
    parser.add_argument("--image_dir", type=str, default="../data_prepared/images",
                        help="图片目录")
    parser.add_argument("--output", type=str, default="../labelstudio_import.json",
                        help="输出JSON文件路径")
    parser.add_argument("--checkpoint", type=str, default=None,
                        help="模型checkpoint路径，用于预填充预测结果")
    parser.add_argument("--device", type=str, default="cuda",
                        help="推理设备")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制导入数量（用于测试）")
    parser.add_argument("--export", type=str, default=None,
                        help="Label Studio导出文件路径（用于反向导出annotations.json）")
    args = parser.parse_args()

    if args.export:
        # 反向导出模式
        export_annotations(args.export, args.output)
    else:
        # 生成导入文件
        generate_import_file(
            args.image_dir,
            args.output,
            checkpoint_path=args.checkpoint,
            device=args.device,
            limit=args.limit,
        )


if __name__ == "__main__":
    main()
