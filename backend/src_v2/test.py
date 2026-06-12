"""
测试脚本 - 在测试集上评估模型并生成混淆矩阵
"""

import os
import argparse
import json
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from sklearn.metrics import classification_report, confusion_matrix, average_precision_score
import numpy as np

from models.clip_detector import CLIPDetector
from models.lora_config import get_clip_lora_config, apply_lora_to_clip
from utils.dataset import KobeDataset, collate_fn


def evaluate_test(model, dataloader, device):
    model.eval()
    all_is_pred, all_is_gt = [], []
    all_safety_pred, all_safety_gt = [], []
    all_safety_probs = []
    all_image_ids = []

    with torch.no_grad():
        for batch in dataloader:
            pixel_values = batch["pixel_values"].to(device)
            is_target = batch["is_target"].to(device)
            safety = batch["content_safety"].to(device)
            safety_mask = batch["safety_mask"].to(device)

            outputs = model(pixel_values)

            is_pred = (torch.sigmoid(outputs["is_target_logits"]) > 0.5).cpu().numpy()
            safety_logits = outputs["safety_logits"]
            safety_probs = F.softmax(safety_logits, dim=-1).cpu().numpy()
            safety_pred = safety_logits.argmax(dim=-1).cpu().numpy()

            all_is_pred.extend(is_pred)
            all_is_gt.extend(is_target.cpu().numpy())

            mask_np = safety_mask.cpu().numpy().astype(bool)
            all_safety_pred.extend(safety_pred[mask_np])
            all_safety_gt.extend(safety.cpu().numpy()[mask_np])
            all_safety_probs.extend(safety_probs[mask_np])
            all_image_ids.extend([bid for bid, m in zip(batch["image_ids"], mask_np) if m])

    return {
        "is_pred": np.array(all_is_pred),
        "is_gt": np.array(all_is_gt),
        "safety_pred": np.array(all_safety_pred),
        "safety_gt": np.array(all_safety_gt),
        "safety_probs": np.array(all_safety_probs),
        "image_ids": all_image_ids,
    }


def print_confusion_matrix(y_true, y_pred, labels, title):
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")
    print(f"{'':12}", end="")
    for l in labels:
        print(f"{l:>10}", end="")
    print()
    for i, row_label in enumerate(labels):
        print(f"{row_label:12}", end="")
        for j, val in enumerate(cm[i]):
            print(f"{val:>10}", end="")
        print(f"  (total: {cm[i].sum()})")
    print()
    return cm


def compute_pr_metrics(y_true, y_probs, labels=[0, 1, 2]):
    """计算 per-class AP 和 macro/micro AP"""
    y_true_binarized = np.zeros((len(y_true), len(labels)), dtype=int)
    for i, label in enumerate(y_true):
        y_true_binarized[i, label] = 1

    ap_per_class = {}
    for idx, label in enumerate(labels):
        if y_true_binarized[:, idx].sum() == 0:
            ap_per_class[int(label)] = 0.0
        else:
            ap_per_class[int(label)] = float(average_precision_score(
                y_true_binarized[:, idx], y_probs[:, idx]
            ))

    macro_ap = float(np.mean(list(ap_per_class.values())))
    micro_ap = float(average_precision_score(y_true_binarized.ravel(), y_probs.ravel()))

    return {
        "ap_per_class": ap_per_class,
        "macro_ap": macro_ap,
        "micro_ap": micro_ap,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="./test_results")
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--device", type=str, default="cuda")
    args = parser.parse_args()

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    os.makedirs(args.output_dir, exist_ok=True)

    print("Loading model...")
    model = CLIPDetector(device=str(device))
    lora_config = get_clip_lora_config(r=8, lora_alpha=16)
    model.clip = apply_lora_to_clip(model.clip, lora_config)

    checkpoint = torch.load(args.checkpoint, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["model"], strict=False)
    model = model.to(device)
    model.eval()

    print("Loading test data...")
    test_dataset = KobeDataset(data_dir=args.data_dir, split="test", processor=model.processor)
    test_loader = DataLoader(
        test_dataset, batch_size=args.batch_size, shuffle=False,
        num_workers=4, collate_fn=collate_fn, pin_memory=True,
    )

    print(f"Evaluating {len(test_dataset)} test samples...")
    results = evaluate_test(model, test_loader, device)

    # ===== Identity 评估 =====
    print("\n" + "="*60)
    print("IDENTITY (人物识别)")
    print("="*60)
    print(classification_report(
        results["is_gt"], results["is_pred"],
        target_names=["Non-Kobe", "Kobe"], digits=4,
    ))

    # ===== Safety 评估 =====
    print("\n" + "="*60)
    print("SAFETY (内容安全分类)")
    print("="*60)
    print(classification_report(
        results["safety_gt"], results["safety_pred"],
        target_names=["safe", "neutral", "harmful"], digits=4,
    ))

    # 混淆矩阵
    print_confusion_matrix(results["is_gt"], results["is_pred"], [0, 1], "Identity Confusion Matrix")
    cm_safety = print_confusion_matrix(
        results["safety_gt"], results["safety_pred"],
        [0, 1, 2], "Safety Confusion Matrix (0=safe, 1=neutral, 2=harmful)",
    )

    # 按类别分析 harmful 的误分类
    print(f"\n{'='*60}")
    print("Harmful 样本详细分析")
    print(f"{'='*60}")
    harmful_mask = results["safety_gt"] == 2
    harmful_preds = results["safety_pred"][harmful_mask]
    harmful_ids = [results["image_ids"][i] for i in range(len(results["safety_gt"])) if results["safety_gt"][i] == 2]

    misclassified_as_safe = [hid for hid, pred in zip(harmful_ids, harmful_preds) if pred == 0]
    misclassified_as_neutral = [hid for hid, pred in zip(harmful_ids, harmful_preds) if pred == 1]

    print(f"Total harmful samples: {harmful_mask.sum()}")
    print(f"Correctly classified as harmful: {(harmful_preds == 2).sum()}")
    print(f"Misclassified as safe: {len(misclassified_as_safe)}")
    print(f"Misclassified as neutral: {len(misclassified_as_neutral)}")

    if misclassified_as_safe[:5]:
        print(f"\nMisclassified as safe (first 5): {misclassified_as_safe[:5]}")
    if misclassified_as_neutral[:5]:
        print(f"Misclassified as neutral (first 5): {misclassified_as_neutral[:5]}")

    # PR-AUC / per-class AP
    print(f"\n{'='*60}")
    print("Safety PR Metrics (per-class AP)")
    print(f"{'='*60}")
    pr_metrics = compute_pr_metrics(results["safety_gt"], results["safety_probs"], labels=[0, 1, 2])
    print(f"Macro AP: {pr_metrics['macro_ap']:.4f}")
    print(f"Micro AP: {pr_metrics['micro_ap']:.4f}")
    for label, ap in pr_metrics["ap_per_class"].items():
        print(f"  AP class {label}: {ap:.4f}")

    # 保存结果
    output = {
        "checkpoint": args.checkpoint,
        "num_test_samples": len(test_dataset),
        "identity": {
            "accuracy": float((results["is_pred"] == results["is_gt"]).mean()),
            "pred_distribution": {
                "non_kobe": int((results["is_pred"] == 0).sum()),
                "kobe": int((results["is_pred"] == 1).sum()),
            },
        },
        "safety": {
            "confusion_matrix": cm_safety.tolist(),
            "harmful_recall": float((harmful_preds == 2).sum() / max(harmful_mask.sum(), 1)),
            "misclassified_as_safe": misclassified_as_safe,
            "misclassified_as_neutral": misclassified_as_neutral,
            "pr_metrics": pr_metrics,
        },
    }

    out_path = os.path.join(args.output_dir, "test_results.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
