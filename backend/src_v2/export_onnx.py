#!/usr/bin/env python3
"""
ONNX 导出脚本 - CLIPDetector v2
将训练好的 PyTorch 模型导出为 ONNX 格式

用法:
    python export_onnx.py --checkpoint outputs_v2_aug/best_model.pth --output kobe_detect_v2.onnx
    python export_onnx.py --checkpoint outputs_v2_aug/best_model.pth --output kobe_detect_v2.onnx --verify
"""

import argparse
import sys

import numpy as np
import torch
import torch.nn as nn

from models.clip_detector import CLIPDetector
from models.lora_config import get_clip_lora_config, apply_lora_to_clip


class ONNXExportWrapper(nn.Module):
    """
    ONNX 友好的包装器
    将 CLIPDetector 的 forward 转换为纯 tensor 流，确保 ONNX trace 友好
    """

    def __init__(self, detector):
        super().__init__()
        # 直接访问 vision_model，绕过 get_image_features() 的返回值不确定性
        self.clip_vision = detector.clip.vision_model
        self.visual_projection = detector.clip.visual_projection
        self.safety_head = detector.safety_head

        # 注册 text_embeds 为 buffer，使其成为 ONNX 常量
        self.register_buffer("text_embeds_pos", detector.text_embeds_pos)
        self.register_buffer("text_embeds_neg", detector.text_embeds_neg)

    def forward(self, pixel_values):
        """
        Args:
            pixel_values: (B, 3, 224, 224)
        Returns:
            is_target_logits: (B,)
            safety_logits: (B, 3)
        """
        # 图像编码: vision_model -> projection -> L2归一化
        vision_out = self.clip_vision(pixel_values, return_dict=True)
        image_embeds = self.visual_projection(vision_out.pooler_output)
        image_embeds = image_embeds / image_embeds.norm(dim=-1, keepdim=True)

        # 人物识别: 与预计算 text_embeds 做余弦相似度
        sim_pos = torch.mm(image_embeds, self.text_embeds_pos.t())  # (B, N_pos)
        sim_pos_max = sim_pos.max(dim=1)[0]  # (B,)

        sim_neg = torch.mm(image_embeds, self.text_embeds_neg.t())  # (B, N_neg)
        sim_neg_max = sim_neg.max(dim=1)[0]  # (B,)

        is_target_logits = sim_pos_max - sim_neg_max  # (B,)

        # 内容安全分类
        safety_logits = self.safety_head(image_embeds, is_target_logits)  # (B, 3)

        return is_target_logits, safety_logits


def load_model(checkpoint_path: str, device: str = "cuda"):
    """加载训练好的模型"""
    print("Loading CLIP model...")
    model = CLIPDetector(device=device)

    print("Applying LoRA...")
    lora_config = get_clip_lora_config(r=8, lora_alpha=16)
    model.clip = apply_lora_to_clip(model.clip, lora_config)

    print(f"Loading checkpoint from {checkpoint_path}...")
    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["model"], strict=False)
    model.eval()

    return model


def merge_lora(model):
    """合并 LoRA 权重到基础模型"""
    print("Merging LoRA weights...")
    model.clip = model.clip.merge_and_unload()

    # 验证合并后的模型完整性
    assert hasattr(model.clip, "get_image_features"), "Merged model missing get_image_features"
    assert hasattr(model.clip, "get_text_features"), "Merged model missing get_text_features"
    print("LoRA merged successfully.")
    return model


def export_onnx(model, output_path: str, device: str = "cuda"):
    """导出 ONNX 模型"""
    wrapper = ONNXExportWrapper(model).to(device).eval()

    # 构建 dummy input
    dummy_input = torch.randn(1, 3, 224, 224).to(device)

    print(f"Exporting ONNX to {output_path}...")
    torch.onnx.export(
        wrapper,
        dummy_input,
        output_path,
        input_names=["pixel_values"],
        output_names=["is_target_logits", "safety_logits"],
        dynamic_axes={
            "pixel_values": {0: "batch_size"},
            "is_target_logits": {0: "batch_size"},
            "safety_logits": {0: "batch_size"},
        },
        opset_version=14,
        do_constant_folding=True,
    )
    print(f"ONNX model exported to {output_path}")


def verify_onnx(model, onnx_path: str, device: str = "cuda", atol: float = 1e-5):
    """验证 ONNX 输出与 PyTorch 输出一致"""
    try:
        import onnxruntime as ort
    except ImportError:
        print("ERROR: onnxruntime not installed. Run: pip install onnxruntime-gpu")
        sys.exit(1)

    wrapper = ONNXExportWrapper(model).to(device).eval()

    # 使用多个随机输入验证
    print("Verifying ONNX export consistency...")
    batch_sizes = [1, 2, 4]
    all_passed = True

    for bs in batch_sizes:
        dummy_input = torch.randn(bs, 3, 224, 224).to(device)

        # PyTorch 推理
        with torch.no_grad():
            pt_is_target, pt_safety = wrapper(dummy_input)

        # ONNX 推理
        session = ort.InferenceSession(onnx_path)
        onnx_input = {"pixel_values": dummy_input.cpu().numpy()}
        onnx_is_target, onnx_safety = session.run(None, onnx_input)

        # 对比
        is_close_1 = np.allclose(onnx_is_target, pt_is_target.cpu().numpy(), atol=atol)
        is_close_2 = np.allclose(onnx_safety, pt_safety.cpu().numpy(), atol=atol)

        if is_close_1 and is_close_2:
            print(f"  Batch={bs}: PASS (atol={atol})")
        else:
            print(f"  Batch={bs}: FAIL")
            diff_1 = np.abs(onnx_is_target - pt_is_target.cpu().numpy()).max()
            diff_2 = np.abs(onnx_safety - pt_safety.cpu().numpy()).max()
            print(f"    is_target max diff: {diff_1:.8f}")
            print(f"    safety max diff: {diff_2:.8f}")
            all_passed = False

    if all_passed:
        print("All verification checks PASSED.")
        return True
    else:
        print("Verification FAILED. ONNX output differs from PyTorch.")
        return False


def main():
    parser = argparse.ArgumentParser(description="Export CLIPDetector v2 to ONNX")
    parser.add_argument("--checkpoint", type=str, required=True, help="Path to best_model.pth")
    parser.add_argument("--output", type=str, required=True, help="Output ONNX file path")
    parser.add_argument("--device", type=str, default="cuda", help="Device: cuda or cpu")
    parser.add_argument("--verify", action="store_true", help="Verify ONNX output consistency")
    parser.add_argument("--atol", type=float, default=1e-5, help="Tolerance for verification")
    args = parser.parse_args()

    device = args.device if torch.cuda.is_available() else "cpu"

    # 加载模型
    model = load_model(args.checkpoint, device)

    # 合并 LoRA
    model = merge_lora(model)

    if args.verify:
        # 仅验证，不导出
        success = verify_onnx(model, args.output, device, atol=args.atol)
        sys.exit(0 if success else 1)
    else:
        # 导出
        export_onnx(model, args.output, device)
        # 导出后立即验证
        success = verify_onnx(model, args.output, device, atol=args.atol)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
