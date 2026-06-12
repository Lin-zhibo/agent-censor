"""
Simple RGCL: Retrieval-Guided Contrastive Learning
轻量版：用固定大小队列维护历史样本 embedding，计算对比损失
"""

import torch
import torch.nn.functional as F


class SimpleQueueContrastiveLoss(torch.nn.Module):
    """
    简单的队列式对比学习损失
    - 维护一个固定大小的特征队列
    - 对每个 query，从队列中检索同类别的 positive 和异类别的 negative
    - 使用 InfoNCE 损失拉近同类、推开异类
    """

    def __init__(self, embed_dim: int = 512, queue_size: int = 4096, temperature: float = 0.15):
        super().__init__()
        self.embed_dim = embed_dim
        self.queue_size = queue_size
        self.temperature = temperature

        # 注册队列 buffer（不参与梯度）
        self.register_buffer("queue_embeds", torch.randn(embed_dim, queue_size))
        self.register_buffer("queue_labels", torch.zeros(queue_size, dtype=torch.long))
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

        # 初始化队列
        self.queue_embeds = F.normalize(self.queue_embeds, dim=0)

    @torch.no_grad()
    def update_queue(self, embeds: torch.Tensor, labels: torch.Tensor):
        """
        将新的 embedding 加入队列
        Args:
            embeds: (B, embed_dim), L2 归一化后的 embedding
            labels: (B,), safety 标签 (0=safe, 1=neutral, 2=harmful)
        """
        embeds = F.normalize(embeds, dim=-1)
        batch_size = embeds.shape[0]
        ptr = int(self.queue_ptr)

        # 循环写入队列
        if ptr + batch_size <= self.queue_size:
            self.queue_embeds[:, ptr:ptr + batch_size] = embeds.T
            self.queue_labels[ptr:ptr + batch_size] = labels
        else:
            # 分两段写入
            remain = self.queue_size - ptr
            self.queue_embeds[:, ptr:] = embeds[:remain].T
            self.queue_labels[ptr:] = labels[:remain]
            self.queue_embeds[:, :batch_size - remain] = embeds[remain:].T
            self.queue_labels[:batch_size - remain] = labels[remain:]

        ptr = (ptr + batch_size) % self.queue_size
        self.queue_ptr[0] = ptr

    def forward(self, query_embeds: torch.Tensor, query_labels: torch.Tensor) -> torch.Tensor:
        """
        计算对比损失
        Args:
            query_embeds: (B, embed_dim), 当前 batch 的图像 embedding
            query_labels: (B,), safety 标签
        Returns:
            loss: scalar
        """
        # 只在队列有一定填充后才计算损失
        ptr = int(self.queue_ptr)
        if ptr < 64 and ptr + query_embeds.shape[0] < 64:
            return torch.tensor(0.0, device=query_embeds.device)

        query_embeds = F.normalize(query_embeds, dim=-1)

        # 计算 query 与队列中所有样本的相似度
        # (B, queue_size)
        sim = torch.mm(query_embeds, self.queue_embeds.to(query_embeds.device))
        sim = sim / self.temperature

        # 构建 mask：同类别为 positive，异类别为 negative
        # (B, queue_size)
        labels_q = query_labels.unsqueeze(1)  # (B, 1)
        labels_k = self.queue_labels.to(query_embeds.device).unsqueeze(0)  # (1, queue_size)
        positive_mask = (labels_q == labels_k).float()  # 同类=1
        negative_mask = (labels_q != labels_k).float()  # 异类=1

        # 排除自身（虽然队列中的样本来自之前 batch，不会完全重合，但为了安全）
        # 这里不做排除，因为队列中的样本来自不同 batch

        # InfoNCE: 对每个 query，在 positive 中拉近距离，在 negative 中推开
        # 公式: -log(exp(sim_pos) / (exp(sim_pos) + sum(exp(sim_neg))))
        exp_sim = torch.exp(sim)

        # positive 的 exp sum
        pos_exp = (exp_sim * positive_mask).sum(dim=1)  # (B,)

        # negative 的 exp sum
        neg_exp = (exp_sim * negative_mask).sum(dim=1)  # (B,)

        # 至少需要一个 positive
        valid = positive_mask.sum(dim=1) > 0
        if valid.sum() == 0:
            return torch.tensor(0.0, device=query_embeds.device)

        pos_exp = pos_exp[valid]
        neg_exp = neg_exp[valid]

        # InfoNCE 损失
        loss = -torch.log(pos_exp / (pos_exp + neg_exp + 1e-8))
        return loss.mean()
