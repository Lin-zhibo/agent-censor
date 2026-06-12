"""
MixUp 数据增强工具 - CLIPDetector v2
"""

import numpy as np
import torch
from typing import Optional, Tuple


class ConservativeMixUp:
    """
    保守型 MixUp：在混合时偏向保留有害/目标信息

    - safety_harmful_floor: 若配对中任一 harmful，混合后 harmful 分量至少为此值
    - identity_target_floor: 若配对中任一 target=1，混合后 identity 至少为此值
    """

    def __init__(self, alpha: float = 0.4, safety_harmful_floor: float = 0.6,
                 identity_target_floor: float = 0.6):
        self.alpha = alpha
        self.safety_harmful_floor = safety_harmful_floor
        self.identity_target_floor = identity_target_floor

    def __call__(self, images: torch.Tensor, labels_safety: torch.Tensor,
                 labels_identity: torch.Tensor, labels_identity_raw: torch.Tensor,
                 safety_mask: Optional[torch.Tensor] = None) -> Tuple:
        """
        Args:
            images: [B, 3, H, W] tensor
            labels_safety: [B] long tensor, 0=safe, 1=neutral, 2=harmful
            labels_identity: [B] float tensor (0 or 1)
            labels_identity_raw: [B] long tensor (0 or 1)
            safety_mask: optional [B] float tensor

        Returns:
            (mixed_images, mixed_safety, mixed_identity, mixed_safety_mask)
            mixed_safety_mask 为 None 当 safety_mask 未提供时
        """
        batch_size = images.size(0)

        # 保证所有标签与索引在同一设备上
        labels_identity_raw = labels_identity_raw.to(images.device)

        # batch_size=1 时直接短路返回原始输入（MixUp 需要配对）
        if batch_size == 1:
            num_safety_classes = 3
            safety_onehot = torch.zeros(batch_size, num_safety_classes,
                                         device=images.device, dtype=torch.float32)
            safety_onehot.scatter_(1, labels_safety.unsqueeze(1), 1.0)
            if safety_mask is not None:
                return images, safety_onehot, labels_identity.float(), safety_mask
            return images, safety_onehot, labels_identity.float(), None

        lam = float(np.random.beta(self.alpha, self.alpha))
        lam = max(min(lam, 1.0 - 1e-3), 1e-3)
        index = torch.randperm(batch_size, device=images.device)

        # 图像混合
        mixed_images = lam * images + (1 - lam) * images[index]

        # Safety 标签 one-hot 化并混合
        num_safety_classes = 3
        safety_onehot = torch.zeros(batch_size, num_safety_classes,
                                     device=images.device, dtype=torch.float32)
        safety_onehot.scatter_(1, labels_safety.unsqueeze(1), 1.0)
        safety_onehot_b = safety_onehot[index]

        mixed_safety = lam * safety_onehot + (1 - lam) * safety_onehot_b

        # 若配对中任一 harmful (==2)，则 harmful 分量至少为 safety_harmful_floor
        has_harmful = (labels_safety == 2) | (labels_safety[index] == 2)
        # 向量化：需要提升 harmful 分量到 floor 的样本
        harmful_weight = mixed_safety[:, 2]
        need_boost = has_harmful & (harmful_weight < self.safety_harmful_floor)

        # 将 SAFE/NEUTRAL 按比例压缩，使 HARMFUL = floor
        other_sum = mixed_safety[:, :2].sum(dim=-1)
        scale = (1.0 - self.safety_harmful_floor) / (other_sum + 1e-8)
        mixed_safety[:, 0] = torch.where(need_boost, mixed_safety[:, 0] * scale, mixed_safety[:, 0])
        mixed_safety[:, 1] = torch.where(need_boost, mixed_safety[:, 1] * scale, mixed_safety[:, 1])
        mixed_safety[:, 2] = torch.where(need_boost, self.safety_harmful_floor, harmful_weight)

        # 重新归一化（含 eps）
        mixed_safety = mixed_safety / (mixed_safety.sum(dim=1, keepdim=True) + 1e-8)

        # Identity 标签混合
        identity_a = labels_identity.float()
        identity_b = labels_identity[index].float()
        mixed_identity = lam * identity_a + (1 - lam) * identity_b

        # 若配对中任一 target=1，则混合后 identity 至少为 identity_target_floor
        has_target = (labels_identity_raw == 1) | (labels_identity_raw[index] == 1)
        mixed_identity[has_target] = torch.maximum(
            mixed_identity[has_target],
            torch.tensor(self.identity_target_floor, device=images.device)
        )

        # 根据 mixed_identity 更新 mask
        mixed_safety_mask = None
        if safety_mask is not None:
            mixed_safety_mask = torch.where(
                mixed_identity >= 0.5,
                torch.tensor(1.0, device=images.device),
                torch.tensor(0.3, device=images.device)
            )

        return mixed_images, mixed_safety, mixed_identity, mixed_safety_mask
