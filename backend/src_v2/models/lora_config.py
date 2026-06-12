"""
LoRA 配置 - 用于微调 CLIP 图像编码器
"""

from peft import LoraConfig, get_peft_model, TaskType


def get_clip_lora_config(r: int = 8, lora_alpha: int = 16, dropout: float = 0.1):
    """
    获取CLIP ViT的LoRA配置

    Args:
        r: LoRA秩，越小越轻量。r=8平衡效果和效率
        lora_alpha: 缩放系数，通常设为2*r
        dropout: LoRA层的dropout率

    Returns:
        LoraConfig
    """
    # CLIP ViT-B/16 在HuggingFace中的模块名
    # 每个encoder layer包含: self_attn (q_proj, k_proj, v_proj, out_proj)
    #                        mlp (fc1, fc2)
    target_modules = [
        "q_proj",
        "k_proj",
        "v_proj",
        "out_proj",
        "fc1",
        "fc2",
    ]

    config = LoraConfig(
        r=r,
        lora_alpha=lora_alpha,
        target_modules=target_modules,
        lora_dropout=dropout,
        bias="none",
        # 不指定modules_to_save，因为分类头是独立模块
    )

    return config


def apply_lora_to_clip(clip_model, lora_config):
    """
    对CLIP模型应用LoRA

    Args:
        clip_model: CLIPModel实例
        lora_config: LoraConfig

    Returns:
        应用了LoRA的模型
    """
    model = get_peft_model(clip_model, lora_config)
    model.print_trainable_parameters()
    return model
