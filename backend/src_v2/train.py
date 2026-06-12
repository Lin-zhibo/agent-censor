"""
训练脚本 - CLIP + LoRA 微调
"""

import os
import argparse
import json

import torch
from torch.utils.data import DataLoader, WeightedRandomSampler
from torch.optim import AdamW
# scheduler imported dynamically in main (ReduceLROnPlateau)
from tqdm import tqdm

from models.clip_detector import CLIPDetector
from models.lora_config import get_clip_lora_config, apply_lora_to_clip
from models.losses import MultiTaskLoss
from models.rgcl import SimpleQueueContrastiveLoss
from utils.dataset import KobeDataset, collate_fn
from utils.metrics import compute_metrics
from utils.mixup import ConservativeMixUp
from utils.ema import ModelEMA


def get_optimizer(model, lr: float = 1e-4, weight_decay: float = 1e-4):
    """只优化LoRA参数和分类头"""
    params = []
    for name, param in model.named_parameters():
        if param.requires_grad:
            params.append({"params": param, "lr": lr})
    return AdamW(params, weight_decay=weight_decay)


def train_epoch(model, dataloader, criterion, optimizer, rgcl_loss, device, epoch,
                task_weights=None, mixup=None, warmup_epochs=3, ema=None):
    model.train()
    total_loss = 0.0
    loss_identity_sum = 0.0
    loss_safety_sum = 0.0
    loss_rgcl_sum = 0.0

    all_is_pred, all_is_gt = [], []
    all_safety_pred, all_safety_gt = [], []

    pbar = tqdm(dataloader, desc=f"Epoch {epoch} [Train]")
    for batch in pbar:
        pixel_values = batch["pixel_values"].to(device)
        is_target = batch["is_target"].to(device)
        safety = batch["content_safety"].to(device)
        safety_mask = batch["safety_mask"].to(device)

        # 保留原始硬标签，用于 RGCL 和训练指标
        is_target_hard = is_target.clone()
        safety_hard = safety.clone()

        if mixup is not None and epoch >= warmup_epochs:
            pixel_values, safety, is_target, safety_mask = mixup(
                pixel_values, safety, is_target, batch["is_target"], safety_mask
            )

        optimizer.zero_grad()
        outputs = model(pixel_values)

        # 固定权重多任务损失（identity : safety = 0.5 : 1.0）
        # safety_mask 确保只有 is_target=1 的图参与 safety loss
        loss_dict = criterion(
            outputs["is_target_logits"],
            outputs["safety_logits"],
            is_target,
            safety,
            safety_mask=safety_mask,
            return_raw=True,
        )

        losses = [loss_dict["identity"], loss_dict["safety"]]
        if task_weights is None:
            current_weights = [0.5, 1.0]
        else:
            current_weights = task_weights
        total_loss_batch = sum(w * l for w, l in zip(current_weights, losses))

        # Phase 3: RGCL 对比学习损失
        # 只有 is_target=1 的样本参与对比学习
        rgcl_l = torch.tensor(0.0, device=device)
        if rgcl_loss is not None and safety_mask.sum() > 0:
            with torch.no_grad():
                rgcl_loss.update_queue(outputs["image_embeds"].detach(), safety_hard)
            rgcl_l = rgcl_loss(outputs["image_embeds"], safety_hard)
            if rgcl_l.item() > 0:
                total_loss_batch = total_loss_batch + 0.3 * rgcl_l
                loss_rgcl_sum += rgcl_l.item()

        total_loss_batch.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()

        if ema is not None:
            ema.update(model)

        total_loss += total_loss_batch.item()
        loss_identity_sum += loss_dict["identity"].item()
        loss_safety_sum += loss_dict["safety"].item()

        with torch.no_grad():
            is_pred = (torch.sigmoid(outputs["is_target_logits"]) > 0.5).cpu().numpy()
            safety_pred = outputs["safety_logits"].argmax(dim=-1).cpu().numpy()

        all_is_pred.extend(is_pred)
        all_is_gt.extend(is_target_hard.cpu().numpy())
        # 只收集 is_target=1 的样本用于 safety 指标
        mask_np = safety_mask.cpu().numpy().astype(bool)
        all_safety_pred.extend(safety_pred[mask_np])
        all_safety_gt.extend(safety_hard.cpu().numpy()[mask_np])

        pbar.set_postfix({
            "loss": f"{total_loss_batch.item():.4f}",
            "id": f"{loss_dict['identity'].item():.4f}",
            "safety": f"{loss_dict['safety'].item():.4f}",
            "rgcl": f"{rgcl_l.item():.4f}",
        })

    metrics = compute_metrics(all_is_pred, all_is_gt, all_safety_pred, all_safety_gt)
    n = len(dataloader)
    return {
        "loss": total_loss / n,
        "loss_identity": loss_identity_sum / n,
        "loss_safety": loss_safety_sum / n,
        "loss_rgcl": loss_rgcl_sum / n,
        **metrics,
    }


@torch.no_grad()
def validate(model, dataloader, criterion, device, epoch):
    model.eval()
    total_loss = 0.0
    all_is_pred, all_is_gt = [], []
    all_safety_pred, all_safety_gt = [], []

    pbar = tqdm(dataloader, desc=f"Epoch {epoch} [Val]")
    for batch in pbar:
        pixel_values = batch["pixel_values"].to(device)
        is_target = batch["is_target"].to(device)
        safety = batch["content_safety"].to(device)
        safety_mask = batch["safety_mask"].to(device)

        outputs = model(pixel_values)
        loss_dict = criterion(
            outputs["is_target_logits"],
            outputs["safety_logits"],
            is_target,
            safety,
            safety_mask=safety_mask,
        )
        total_loss += loss_dict["total"].item()

        is_pred = (torch.sigmoid(outputs["is_target_logits"]) > 0.5).cpu().numpy()
        safety_pred = outputs["safety_logits"].argmax(dim=-1).cpu().numpy()

        all_is_pred.extend(is_pred)
        all_is_gt.extend(is_target.cpu().numpy())
        # 只收集 is_target=1 的样本用于 safety 指标
        mask_np = safety_mask.cpu().numpy().astype(bool)
        all_safety_pred.extend(safety_pred[mask_np])
        all_safety_gt.extend(safety.cpu().numpy()[mask_np])

    metrics = compute_metrics(all_is_pred, all_is_gt, all_safety_pred, all_safety_gt)
    n = len(dataloader)
    return {
        "loss": total_loss / n,
        **metrics,
    }


def main():
    parser = argparse.ArgumentParser(description="Train CLIPDetector v2")
    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="./outputs_v2")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--lora_r", type=int, default=8)
    parser.add_argument("--lora_alpha", type=int, default=16)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--resume", type=str, default=None)
    parser.add_argument("--use_mixup", action="store_true", default=False)
    parser.add_argument("--mixup_alpha", type=float, default=0.4)
    parser.add_argument("--mixup_warmup_epochs", type=int, default=3)
    parser.add_argument("--best_metric", type=str, default="identity_f1",
                        choices=["identity_f1", "safety_harmful_recall", "safety_macro_f1"],
                        help="Metric used to select best model and early stopping")
    parser.add_argument("--save_every", type=int, default=1,
                        help="Save a checkpoint every N epochs (1=every epoch)")
    parser.add_argument("--use_ema", action="store_true", default=False,
                        help="Use Exponential Moving Average for model weights")
    parser.add_argument("--ema_decay", type=float, default=0.999,
                        help="EMA decay rate")
    parser.add_argument("--safety_alpha", type=float, nargs=3, default=[1.0, 1.0, 2.0],
                        help="FocalLoss class weights for [safe, neutral, harmful]")
    parser.add_argument("--safety_gamma", type=float, default=2.0,
                        help="FocalLoss gamma")
    parser.add_argument("--safety_label_smoothing", type=float, default=0.0,
                        help="Label smoothing for safety FocalLoss")
    parser.add_argument("--lambda_identity", type=float, default=0.5,
                        help="Weight for identity loss")
    parser.add_argument("--lambda_safety", type=float, default=1.0,
                        help="Weight for safety loss")
    parser.add_argument("--keep_last_n", type=int, default=2,
                        help="Keep only last N periodic checkpoints to save disk")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")

    # 加载模型
    print("Loading CLIP model...")
    model = CLIPDetector(device=str(device))

    # 应用LoRA
    print("Applying LoRA...")
    lora_config = get_clip_lora_config(r=args.lora_r, lora_alpha=args.lora_alpha)
    model.clip = apply_lora_to_clip(model.clip, lora_config)
    model = model.to(device)

    # 损失函数
    criterion = MultiTaskLoss(
        lambda_identity=args.lambda_identity,
        lambda_safety=args.lambda_safety,
        safety_loss_type="focal",
        safety_alpha=list(args.safety_alpha),
        safety_gamma=args.safety_gamma,
        safety_label_smoothing=args.safety_label_smoothing,
    ).to(device)

    # 固定权重多任务训练（identity : safety = 0.5 : 1.0）
    optimizer = get_optimizer(model, lr=args.lr)
    # Warmup + ReduceLROnPlateau 调度器
    warmup_epochs = 3
    scheduler = None  # warmup 手动处理，主 scheduler 在 warmup 后创建

    # Phase 3: RGCL 对比学习
    rgcl_loss = SimpleQueueContrastiveLoss(embed_dim=512, queue_size=4096).to(device)

    # MixUp 实例化
    mixup = None
    if args.use_mixup:
        mixup = ConservativeMixUp(alpha=args.mixup_alpha)

    # EMA
    ema = None
    if args.use_ema:
        ema = ModelEMA(model, decay=args.ema_decay)
        print(f"EMA enabled (decay={args.ema_decay})")

    # 数据（训练集启用 harmful 强增强）
    train_dataset = KobeDataset(
        data_dir=args.data_dir,
        split="train",
        processor=model.processor,
        augment=True,
    )
    val_dataset = KobeDataset(
        data_dir=args.data_dir,
        split="val",
        processor=model.processor,
    )

    # 过采样：对harmful类和hard negative适度增加采样权重
    weights = []
    for img_id in train_dataset.image_ids:
        anno = train_dataset.annotations.get(img_id, {})
        label = anno.get("content_safety", 1)
        weight = 2.0 if label == 2 else 1.0
        # hard negative: test 上被误分的 harmful 样本，3倍权重
        if img_id in KobeDataset.HARD_NEGATIVE_IDS:
            weight *= 3.0
        weights.append(weight)
    sampler = WeightedRandomSampler(weights, num_samples=len(weights)*2, replacement=True)

    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        sampler=sampler,
        num_workers=args.num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )

    # 恢复训练
    start_epoch = 0
    best_f1 = 0.0
    if args.resume:
        checkpoint = torch.load(args.resume, map_location=device)
        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        start_epoch = checkpoint["epoch"] + 1
        best_f1 = checkpoint.get("best_f1", 0.0)
        if ema is not None and "ema" in checkpoint:
            ema.load_state_dict(checkpoint["ema"])
        print(f"Resumed from epoch {start_epoch}")

    # 训练循环
    history = []
    patience = 10
    patience_counter = 0

    for epoch in range(start_epoch, args.epochs):
        print(f"\n{'='*60}")
        print(f"Epoch {epoch+1}/{args.epochs}")
        print(f"{'='*60}")

        # Warmup: 前 N 个 epoch 线性增加学习率
        if epoch < warmup_epochs:
            for param_group in optimizer.param_groups:
                param_group['lr'] = args.lr * (epoch + 1) / warmup_epochs

        train_metrics = train_epoch(
            model, train_loader, criterion, optimizer, rgcl_loss, str(device), epoch+1,
            mixup=mixup, warmup_epochs=args.mixup_warmup_epochs, ema=ema
        )

        # 若启用 EMA，验证时使用 EMA 权重
        if ema is not None:
            ema.apply_shadow(model)
        val_metrics = validate(model, val_loader, criterion, str(device), epoch+1)
        if ema is not None:
            ema.restore(model)

        # Warmup 结束后使用 ReduceLROnPlateau
        if epoch >= warmup_epochs:
            if scheduler is None:
                from torch.optim.lr_scheduler import ReduceLROnPlateau
                scheduler = ReduceLROnPlateau(optimizer, mode='max', factor=0.5, patience=3)
            scheduler.step(val_metrics.get(args.best_metric, 0))

        record = {
            "epoch": epoch + 1,
            "train": train_metrics,
            "val": val_metrics,
            "lr": optimizer.param_groups[0]["lr"],
        }
        history.append(record)

        print(f"\nTrain - Loss: {train_metrics['loss']:.4f} | "
              f"Identity F1: {train_metrics.get('identity_f1', 0):.4f} | "
              f"Safety F1: {train_metrics.get('safety_macro_f1', 0):.4f} | "
              f"RGCL: {train_metrics.get('loss_rgcl', 0):.4f}")
        print(f"Val   - Loss: {val_metrics['loss']:.4f} | "
              f"Identity F1: {val_metrics.get('identity_f1', 0):.4f} | "
              f"Safety F1: {val_metrics.get('safety_macro_f1', 0):.4f} | "
              f"Harmful Recall: {val_metrics.get('safety_harmful_recall', 0):.4f}")

        # 保存最佳模型（按指定指标）
        val_score = val_metrics.get(args.best_metric, 0)
        is_best = val_score > best_f1
        if is_best:
            best_f1 = val_score
            patience_counter = 0
            save_dict = {
                "epoch": epoch,
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "best_f1": best_f1,
                "best_metric": args.best_metric,
            }
            if ema is not None:
                save_dict["ema"] = ema.state_dict()
            torch.save(save_dict, os.path.join(args.output_dir, "best_model.pth"))
            print(f"Saved best model ({args.best_metric}: {best_f1:.4f})")
        else:
            patience_counter += 1

        # 按 save_every 保存周期 checkpoint
        if (epoch + 1) % args.save_every == 0:
            save_dict = {
                "epoch": epoch,
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "best_metric": args.best_metric,
            }
            if ema is not None:
                save_dict["ema"] = ema.state_dict()
            torch.save(save_dict, os.path.join(args.output_dir, f"checkpoint_epoch{epoch+1}.pth"))

        # 清理旧 checkpoint，只保留最近 N 个
        if args.keep_last_n > 0:
            checkpoint_files = sorted([
                f for f in os.listdir(args.output_dir)
                if f.startswith("checkpoint_epoch") and f.endswith(".pth")
            ], key=lambda x: int(x.replace("checkpoint_epoch", "").replace(".pth", "")))
            for old_ckpt in checkpoint_files[:-args.keep_last_n]:
                os.remove(os.path.join(args.output_dir, old_ckpt))

        if patience_counter >= patience:
            print(f"Early stopping at epoch {epoch+1}")
            break

    with open(os.path.join(args.output_dir, "history.json"), "w") as f:
        json.dump(history, f, indent=2)

    print(f"\nTraining completed! Best {args.best_metric}: {best_f1:.4f}")


if __name__ == "__main__":
    main()
