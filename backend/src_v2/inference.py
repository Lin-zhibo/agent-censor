"""
推理脚本 - CLIPDetector v2
单图/批量图像检测
"""

import os
import argparse
import json
from pathlib import Path
from typing import Dict, List, Union

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from models.clip_detector import CLIPDetector
from models.lora_config import apply_lora_to_clip, get_clip_lora_config


class CLIPDetectorPredictor:
    """推理封装类"""

    def __init__(self,
                 model_path: str,
                 device: str = "cuda",
                 identity_threshold: float = 0.5,
                 risk_threshold_high: float = 0.7):

        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.identity_threshold = identity_threshold
        self.risk_threshold_high = risk_threshold_high

        # 加载模型并应用LoRA（与训练时一致）
        self.model = CLIPDetector(device=str(self.device))
        lora_config = get_clip_lora_config(r=8, lora_alpha=16)
        self.model.clip = apply_lora_to_clip(self.model.clip, lora_config)
        checkpoint = torch.load(model_path, map_location=self.device, weights_only=False)
        # 兼容旧模型: 新架构的 safety_head 参数可能缺失，用 strict=False 忽略
        missing, unexpected = self.model.load_state_dict(checkpoint["model"], strict=False)
        if missing:
            print(f"[WARN] Missing keys in checkpoint (will use random init): {len(missing)} keys")
            for k in missing[:5]:
                print(f"  - {k}")
            if len(missing) > 5:
                print(f"  ... and {len(missing) - 5} more")
        if unexpected:
            print(f"[WARN] Unexpected keys in checkpoint (will be ignored): {len(unexpected)} keys")
            for k in unexpected[:5]:
                print(f"  - {k}")
            if len(unexpected) > 5:
                print(f"  ... and {len(unexpected) - 5} more")
        self.model.to(self.device)
        self.model.eval()

    def predict(self, image: Union[str, Image.Image]) -> Dict:
        """单图预测"""
        if isinstance(image, str):
            pil_image = Image.open(image).convert("RGB")
        else:
            pil_image = image.convert("RGB")

        # 预处理
        pixel_values = self.model.preprocess_images([pil_image])

        # 推理
        with torch.no_grad():
            outputs = self.model.predict(pixel_values)

        is_target = outputs["is_target"][0].item()
        safety_probs = outputs["safety_probs"][0].cpu().numpy()
        risk_score = outputs["risk_score"][0].item()

        # Phase 1: 硬编码规则 — 不是目标人物时强制 safe
        if is_target < self.identity_threshold:
            safety_idx = 0  # safe
            content_type = "safe"
            # 重置 safety_probs：safe=1.0, neutral=0, harmful=0
            safety_probs = np.array([1.0, 0.0, 0.0])
            # risk_score 重新计算: is_target * (0.8*0 + 0.1*0) = 0
            risk_score = 0.0
        else:
            safety_idx = int(safety_probs.argmax())
            content_type = self.model.SAFETY_CLASSES[safety_idx]

        # 风险等级
        if risk_score >= 0.9:
            risk_level = "extreme"
        elif risk_score >= self.risk_threshold_high:
            risk_level = "high"
        elif risk_score >= 0.5:
            risk_level = "medium"
        elif risk_score >= 0.3:
            risk_level = "low"
        else:
            risk_level = "none"

        # 人物识别详细分数
        with torch.no_grad():
            image_embeds = self.model.encode_image(pixel_values)
            sim_pos = torch.mm(image_embeds, self.model.text_embeds_pos.t())
            sim_neg = torch.mm(image_embeds, self.model.text_embeds_neg.t())

        return {
            "is_target": is_target,
            "is_target_binary": is_target >= self.identity_threshold,
            "content_type": content_type,
            "content_probs": {
                "safe": float(safety_probs[0]),
                "neutral": float(safety_probs[1]),
                "harmful": float(safety_probs[2]),
            },
            "risk_score": risk_score,
            "risk_level": risk_level,
            "need_review": risk_score >= self.risk_threshold_high,
            "person_match_scores": {
                "max_positive_sim": float(sim_pos.max().item()),
                "max_negative_sim": float(sim_neg.max().item()),
            },
        }

    def predict_batch(self, images: List[Union[str, Image.Image]], batch_size: int = 8) -> List[Dict]:
        """批量预测"""
        results = []
        for i in range(0, len(images), batch_size):
            batch = images[i:i + batch_size]
            for img in batch:
                results.append(self.predict(img))
        return results


def main():
    parser = argparse.ArgumentParser(description="CLIPDetector v2 Inference")
    parser.add_argument("--model", type=str, required=True, help="模型检查点路径")
    parser.add_argument("--input", type=str, required=True, help="输入图像路径或目录")
    parser.add_argument("--output", type=str, default="./results_v2.json", help="输出JSON路径")
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--threshold", type=float, default=0.5, help="人物识别阈值")
    args = parser.parse_args()

    predictor = CLIPDetectorPredictor(
        model_path=args.model,
        device=args.device,
        identity_threshold=args.threshold,
    )

    input_path = Path(args.input)
    if input_path.is_dir():
        image_paths = [
            str(p) for p in input_path.glob("*")
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        ]
    else:
        image_paths = [str(input_path)]

    print(f"Processing {len(image_paths)} images...")
    results = predictor.predict_batch(image_paths)

    output = {
        "model": args.model,
        "threshold": args.threshold,
        "num_images": len(image_paths),
        "results": [
            {"image": str(path), **result}
            for path, result in zip(image_paths, results)
        ],
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Results saved to {args.output}")

    target_count = sum(1 for r in results if r["is_target_binary"])
    harmful_count = sum(1 for r in results if r["content_type"] == "harmful")
    high_risk_count = sum(1 for r in results if r["risk_level"] in ("high", "extreme"))

    print(f"\nSummary: Total={len(results)}, Target={target_count}, Harmful={harmful_count}, HighRisk={high_risk_count}")


if __name__ == "__main__":
    main()
