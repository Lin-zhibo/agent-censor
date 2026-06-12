"""
KobeDetect v2 - CLIP + LoRA 微调架构
极简设计：共享CLIP编码器 → 双任务输出
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Optional, Tuple
from transformers import CLIPModel, CLIPProcessor


class IdentityAwareSafetyHead(nn.Module):
    """
    Phase 1+2: Identity-aware Safety Head
    - CLIP-Adapter 残差融合保护预训练知识
    - Identity 门控: 非目标人物时抑制 harmful/neutral 输出
    - 加深到 3 层，增强表达能力
    """

    def __init__(self, embed_dim: int = 512, num_classes: int = 3,
                 adapter_dim: int = 128, dropout: float = 0.3):
        super().__init__()

        # 1. CLIP-Adapter: bottleneck MLP + 可学习残差权重
        self.adapter = nn.Sequential(
            nn.Linear(embed_dim, adapter_dim),
            nn.GELU(),
            nn.Linear(adapter_dim, embed_dim),
        )
        self.alpha = nn.Parameter(torch.tensor(0.2))

        # 2. Identity 门控: identity_prob -> 门控向量
        self.gate_proj = nn.Sequential(
            nn.Linear(1, embed_dim),
            nn.Sigmoid(),
        )

        # 3. 加深分类层 (embed_dim*2 -> embed_dim -> embed_dim//2 -> num_classes)
        self.classifier = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.LayerNorm(embed_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(embed_dim, embed_dim // 2),
            nn.LayerNorm(embed_dim // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(embed_dim // 2, num_classes),
        )

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                # GELU 的增益近似于 ReLU，使用 relu 作为 kaiming 的 nonlinearity 参数
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, image_embeds: torch.Tensor, identity_logits: torch.Tensor) -> torch.Tensor:
        """
        Args:
            image_embeds: (B, embed_dim) CLIP 图像embedding
            identity_logits: (B,) 人物识别logits (pos_sim - neg_sim)
        Returns:
            safety_logits: (B, num_classes)
        """
        # CLIP-Adapter 残差融合
        adapted = self.adapter(image_embeds)
        fused = self.alpha * adapted + (1.0 - self.alpha) * image_embeds

        # Identity 门控
        identity_prob = torch.sigmoid(identity_logits).unsqueeze(-1)  # (B, 1)
        gate = self.gate_proj(identity_prob)  # (B, embed_dim)
        gated = fused * gate

        # 拼接原始融合特征和门控特征，增强表达
        combined = torch.cat([fused, gated], dim=-1)  # (B, embed_dim*2)
        return self.classifier(combined)


class CLIPDetector(nn.Module):
    """
    CLIP-based 公众人物图像舆情检测模型

    架构:
        图像 → CLIP ViT (LoRA微调) → 图像embedding (512D)
            ↓
        ├─→ 人物识别: 与预计算文本prompts余弦相似度
        └─→ 内容安全: 轻量MLP (512→256→3)
    """

    SAFETY_CLASSES = ["safe", "neutral", "harmful"]

    # 预定义的文本prompts
    PERSON_PROMPTS = {
        "positive": [  # 目标人物
            "a photo of Kobe Bryant",
            "Kobe Bryant, basketball player",
            "Kobe Bryant wearing Lakers jersey",
            "Kobe Bryant smiling",
            "Kobe Bryant in a basketball game",
        ],
        "negative": [  # 非目标人物
            "a photo of someone else",
            "another person",
            "not Kobe Bryant",
            "a different basketball player",
            "a random person",
        ],
    }

    def __init__(
        self,
        model_name: str = "openai/clip-vit-base-patch16",
        safety_hidden_dim: int = 256,
        safety_dropout: float = 0.3,
        identity_threshold: float = 0.5,
        device: str = "cuda",
    ):
        super().__init__()
        self.device = device
        self.identity_threshold = identity_threshold

        # 加载CLIP模型并移到设备
        self.clip = CLIPModel.from_pretrained(model_name).to(self.device)
        self.processor = CLIPProcessor.from_pretrained(model_name)

        # 冻结原始CLIP参数 (LoRA会覆盖部分)
        for param in self.clip.parameters():
            param.requires_grad = False

        # 内容安全分类头: Identity-aware CLIP-Adapter + 门控 + 加深层
        self.safety_head = IdentityAwareSafetyHead(
            embed_dim=512,
            num_classes=3,
            adapter_dim=128,
            dropout=safety_dropout,
        )

        # 预计算文本embedding (用于人物识别，推理时缓存)
        self.register_buffer("text_embeds_pos", None)
        self.register_buffer("text_embeds_neg", None)
        self._precompute_text_embeddings()

        self._init_weights()

    def _init_weights(self):
        for m in self.safety_head.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    @torch.no_grad()
    def _precompute_text_embeddings(self):
        """预计算文本prompts的embedding，避免每次推理重复编码"""
        # 正例 (目标人物)
        pos_inputs = self.processor(
            text=self.PERSON_PROMPTS["positive"],
            return_tensors="pt",
            padding=True,
        ).to(self.device)
        pos_embeds = self.clip.get_text_features(**pos_inputs)
        if not isinstance(pos_embeds, torch.Tensor):
            pos_embeds = pos_embeds.pooler_output
        pos_embeds = F.normalize(pos_embeds, dim=-1)
        self.text_embeds_pos = pos_embeds  # (N_pos, 512)

        # 负例 (非目标人物)
        neg_inputs = self.processor(
            text=self.PERSON_PROMPTS["negative"],
            return_tensors="pt",
            padding=True,
        ).to(self.device)
        neg_embeds = self.clip.get_text_features(**neg_inputs)
        if not isinstance(neg_embeds, torch.Tensor):
            neg_embeds = neg_embeds.pooler_output
        neg_embeds = F.normalize(neg_embeds, dim=-1)
        self.text_embeds_neg = neg_embeds  # (N_neg, 512)

    def encode_image(self, images: torch.Tensor) -> torch.Tensor:
        """
        编码图像为CLIP embedding
        Args:
            images: (B, 3, H, W), 已预处理
        Returns:
            image_embeds: (B, 512), L2归一化
        """
        image_embeds = self.clip.get_image_features(pixel_values=images)
        if not isinstance(image_embeds, torch.Tensor):
            image_embeds = image_embeds.pooler_output
        image_embeds = F.normalize(image_embeds, dim=-1)
        return image_embeds

    def forward(self, images: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        前向传播
        Args:
            images: (B, 3, H, W), 已预处理
        Returns:
            Dict:
                - is_target_logits: (B,) 人物识别logits (未sigmoid)
                - safety_logits: (B, 3) 内容安全分类logits (未softmax)
                - image_embeds: (B, 512) 用于可视化/debug
        """
        # 1. CLIP图像编码
        image_embeds = self.encode_image(images)  # (B, 512)

        # 2. 人物识别: 与预计算文本prompts做余弦相似度
        # 正例最大相似度
        sim_pos = torch.mm(image_embeds, self.text_embeds_pos.t())  # (B, N_pos)
        sim_pos_max = sim_pos.max(dim=1)[0]  # (B,)

        # 负例最大相似度
        sim_neg = torch.mm(image_embeds, self.text_embeds_neg.t())  # (B, N_neg)
        sim_neg_max = sim_neg.max(dim=1)[0]  # (B,)

        # 正例logit - 负例logit (对比学习风格)
        is_target_logits = sim_pos_max - sim_neg_max  # (B,)

        # 3. 内容安全分类 (identity-aware)
        safety_logits = self.safety_head(image_embeds, is_target_logits)  # (B, 3)

        return {
            "is_target_logits": is_target_logits,
            "safety_logits": safety_logits,
            "image_embeds": image_embeds,
        }

    @torch.no_grad()
    def predict(self, images: torch.Tensor) -> Dict[str, torch.Tensor]:
        """推理模式"""
        self.eval()
        outputs = self.forward(images)

        # 人物识别: sigmoid
        is_target_prob = torch.sigmoid(outputs["is_target_logits"])  # (B,)

        # 内容安全: softmax
        safety_probs = F.softmax(outputs["safety_logits"], dim=-1)  # (B, 3)

        # 风险评分
        risk_scores = self.compute_risk_score(is_target_prob, safety_probs)

        return {
            "is_target": is_target_prob,
            "safety_probs": safety_probs,
            "risk_score": risk_scores,
            "image_embeds": outputs["image_embeds"],
        }

    @staticmethod
    def compute_risk_score(is_target: torch.Tensor, safety_probs: torch.Tensor) -> torch.Tensor:
        """
        计算综合风险评分
        risk = is_target * (0.8 * P(harmful) + 0.1 * P(neutral))
        """
        risk = is_target * (
            0.8 * safety_probs[:, 2] +   # harmful
            0.1 * safety_probs[:, 1]     # neutral
        )
        return risk

    def preprocess_images(self, images: List) -> torch.Tensor:
        """
        预处理图像列表
        Args:
            images: List[PIL.Image] or List[str]
        Returns:
            pixel_values: (B, 3, 224, 224)
        """
        from PIL import Image

        pil_images = []
        for img in images:
            if isinstance(img, str):
                pil_images.append(Image.open(img).convert("RGB"))
            else:
                pil_images.append(img.convert("RGB"))

        inputs = self.processor(images=pil_images, return_tensors="pt")
        return inputs["pixel_values"].to(self.device)
