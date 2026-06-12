#!/usr/bin/env python3
"""
ONNX Runtime 推理脚本 - CLIPDetector v2

用法:
    python infer_onnx.py --model kobe_detect_v2.onnx --image path/to/image.jpg
    python infer_onnx.py --model kobe_detect_v2.onnx --image path/to/image.jpg --threshold 0.5
"""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


class KobeDetectONNX:
    """ONNX Runtime 推理封装"""

    def __init__(self, model_path: str, identity_threshold: float = 0.5):
        import onnxruntime as ort

        # 自动选择可用的 provider
        available_providers = ort.get_available_providers()
        if "CUDAExecutionProvider" in available_providers:
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            print(f"Using providers: {providers}")
        else:
            providers = ["CPUExecutionProvider"]
            print(f"CUDA not available, using: {providers}")

        self.session = ort.InferenceSession(model_path, providers=providers)
        self.identity_threshold = identity_threshold

        # 加载 CLIPProcessor 用于预处理（仍需 transformers）
        from transformers import CLIPProcessor
        self.processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch16")

        # 获取输入输出信息
        self.input_name = self.session.get_inputs()[0].name

    def preprocess(self, images) -> np.ndarray:
        """预处理图像列表"""
        pil_images = []
        for img in images:
            if isinstance(img, (str, Path)):
                pil_images.append(Image.open(img).convert("RGB"))
            else:
                pil_images.append(img.convert("RGB"))

        inputs = self.processor(images=pil_images, return_tensors="np")
        return inputs["pixel_values"]

    def predict(self, images) -> dict:
        """
        推理并返回结果
        Args:
            images: List[PIL.Image] or List[str]
        Returns:
            dict: is_target, safety_probs, risk_score
        """
        pixel_values = self.preprocess(images)

        # ONNX 推理
        is_target_logits, safety_logits = self.session.run(
            None, {self.input_name: pixel_values}
        )

        # sigmoid
        is_target_prob = 1.0 / (1.0 + np.exp(-is_target_logits))

        # 硬编码规则：非目标人物时强制 safe（与原始模型逻辑一致）
        safety_probs = np.zeros((len(is_target_prob), 3))
        for i, is_target in enumerate(is_target_prob):
            if is_target < self.identity_threshold:
                safety_probs[i] = [1.0, 0.0, 0.0]
            else:
                exp_safety = np.exp(safety_logits[i] - safety_logits[i].max())
                safety_probs[i] = exp_safety / exp_safety.sum()

        # risk_score (与原始模型一致)
        risk_scores = is_target_prob * (
            0.8 * safety_probs[:, 2] + 0.1 * safety_probs[:, 1]
        )

        return {
            "is_target": is_target_prob,
            "safety_probs": safety_probs,
            "risk_score": risk_scores,
        }

    def predict_single(self, image) -> dict:
        """单图推理，返回标量结果"""
        results = self.predict([image])
        return {
            "is_target": float(results["is_target"][0]),
            "safety": {
                "safe": float(results["safety_probs"][0][0]),
                "neutral": float(results["safety_probs"][0][1]),
                "harmful": float(results["safety_probs"][0][2]),
            },
            "risk_score": float(results["risk_score"][0]),
        }


def main():
    parser = argparse.ArgumentParser(description="ONNX Runtime inference for CLIPDetector v2")
    parser.add_argument("--model", type=str, required=True, help="Path to ONNX model")
    parser.add_argument("--image", type=str, required=True, help="Path to image file")
    parser.add_argument("--threshold", type=float, default=0.5, help="Identity threshold")
    args = parser.parse_args()

    model = KobeDetectONNX(args.model, identity_threshold=args.threshold)
    result = model.predict_single(args.image)

    print(f"\n{'='*50}")
    print(f"Inference Result")
    print(f"{'='*50}")
    print(f"is_target:     {result['is_target']:.4f}")
    print(f"safety:")
    print(f"  safe:        {result['safety']['safe']:.4f}")
    print(f"  neutral:     {result['safety']['neutral']:.4f}")
    print(f"  harmful:     {result['safety']['harmful']:.4f}")
    print(f"risk_score:    {result['risk_score']:.4f}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
