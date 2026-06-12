"""
损失函数 - CLIPDetector v2
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, Optional


class FocalLoss(nn.Module):
    """Focal Loss for classification with class imbalance
    支持硬标签 [B] 和 soft target [B, C]
    """

    def __init__(self, alpha: Optional[torch.Tensor] = None,
                 gamma: float = 2.0, reduction: str = "mean",
                 label_smoothing: float = 0.0, num_classes: int = 3):
        super().__init__()
        # 使用register_buffer使alpha随模型自动移到目标设备
        self.register_buffer("alpha", alpha)
        self.gamma = gamma
        self.reduction = reduction
        self.label_smoothing = label_smoothing
        self.num_classes = num_classes

    def _smooth_targets(self, targets: torch.Tensor) -> torch.Tensor:
        """将硬标签 [B] 转换为带 label smoothing 的 soft target [B, C]"""
        if self.label_smoothing <= 0:
            return targets
        smooth_value = self.label_smoothing / self.num_classes
        soft_targets = torch.full(
            (targets.size(0), self.num_classes),
            smooth_value,
            device=targets.device,
            dtype=torch.float32,
        )
        confidence = 1.0 - self.label_smoothing + smooth_value
        soft_targets.scatter_(1, targets.unsqueeze(1), confidence)
        return soft_targets

    def forward(self, logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        if targets.dim() == 1:
            # 硬标签路径：若启用 label smoothing，先转换为 soft target
            if self.label_smoothing > 0:
                targets = self._smooth_targets(targets)
                return self.forward(logits, targets)
            ce_loss = F.cross_entropy(logits, targets, weight=self.alpha,
                                       reduction="none")
            pt = torch.exp(-ce_loss)
        elif targets.dim() == 2:
            # soft target 路径
            targets = targets / targets.sum(dim=-1, keepdim=True)
            log_probs = F.log_softmax(logits, dim=-1)
            ce_loss = -(targets * log_probs).sum(dim=-1)
            # pt: 预测概率在目标分布上的期望
            probs = torch.exp(log_probs)
            pt = (targets * probs).sum(dim=-1)
            # alpha 加权
            if self.alpha is not None:
                alpha_weight = (targets * self.alpha).sum(dim=-1)
                ce_loss = alpha_weight * ce_loss
        else:
            raise ValueError(f"targets 维度必须是 1 或 2，得到 {targets.dim()}")

        pt = pt.clamp(max=1.0)
        focal_loss = ((1 - pt) ** self.gamma) * ce_loss

        if self.reduction == "mean":
            return focal_loss.mean()
        elif self.reduction == "sum":
            return focal_loss.sum()
        return focal_loss


class MultiTaskLoss(nn.Module):
    """
    多任务联合损失
    L = λ1 * L_identity + λ2 * L_safety

    注意: 不再手动实现L2正则化，依赖AdamW的weight_decay
    """

    def __init__(self,
                 lambda_identity: float = 0.5,
                 lambda_safety: float = 1.0,
                 safety_loss_type: str = "focal",
                 safety_alpha: Optional[list] = None,
                 safety_gamma: float = 2.0,
                 safety_label_smoothing: float = 0.0,
                 temperature: float = 0.07):
        super().__init__()

        self.lambda_identity = lambda_identity
        self.lambda_safety = lambda_safety
        self.temperature = temperature

        # 人物识别损失: 对比学习风格的BCE
        # 实际上我们直接优化logit差值，用MSE或BCE都可以
        self.identity_criterion = nn.BCEWithLogitsLoss()

        # 内容安全分类损失
        if safety_loss_type == "focal":
            alpha = torch.tensor(safety_alpha) if safety_alpha else None
            self.safety_criterion = FocalLoss(
                alpha=alpha, gamma=safety_gamma,
                label_smoothing=safety_label_smoothing,
            )
        elif safety_loss_type == "ce":
            weight = torch.tensor(safety_alpha) if safety_alpha else None
            self.safety_criterion = nn.CrossEntropyLoss(weight=weight)
        else:
            raise ValueError(f"Unknown safety loss type: {safety_loss_type}")

    def forward(self,
                is_target_logits: torch.Tensor,
                safety_logits: torch.Tensor,
                is_target_gt: torch.Tensor,
                safety_gt: torch.Tensor,
                safety_mask: torch.Tensor = None,
                return_raw: bool = False) -> Dict[str, torch.Tensor]:
        """
        Args:
            is_target_logits: (B,) 人物识别logits (pos_sim - neg_sim)
            safety_logits: (B, 3) 内容安全分类logits
            is_target_gt: (B,) 0/1
            safety_gt: (B,) 类别索引 0/1/2
            safety_mask: (B,) 0/1，只有 mask=1 的样本参与 safety loss 计算
            return_raw: 为 True 时返回未加权、未detach的原始损失（供 GradNorm 使用）
        """
        # 人物识别损失（所有样本）
        loss_identity = self.identity_criterion(
            is_target_logits, is_target_gt.float()
        )

        # 内容安全分类损失（只对 is_target=1 的样本计算）
        if safety_mask is not None and safety_mask.sum() > 0:
            mask_bool = safety_mask.bool()
            loss_safety = self.safety_criterion(
                safety_logits[mask_bool], safety_gt[mask_bool]
            )
        else:
            loss_safety = torch.tensor(0.0, device=safety_logits.device)

        if return_raw:
            return {
                "identity": loss_identity,
                "safety": loss_safety,
            }

        # 总损失
        total_loss = (
            self.lambda_identity * loss_identity +
            self.lambda_safety * loss_safety
        )

        return {
            "total": total_loss,
            "identity": loss_identity.detach(),
            "safety": loss_safety.detach(),
        }
