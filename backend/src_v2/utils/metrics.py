"""
评估指标
"""

import numpy as np
from typing import Dict, List
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score


def compute_metrics(is_pred: List, is_gt: List,
                    safety_pred: List, safety_gt: List) -> Dict[str, float]:
    """
    计算多任务评估指标
    """
    is_pred = np.array(is_pred)
    is_gt = np.array(is_gt)
    safety_pred = np.array(safety_pred)
    safety_gt = np.array(safety_gt)

    metrics = {}

    # 人物识别指标
    metrics["identity_accuracy"] = accuracy_score(is_gt, is_pred)
    metrics["identity_precision"] = precision_score(is_gt, is_pred, zero_division=0)
    metrics["identity_recall"] = recall_score(is_gt, is_pred, zero_division=0)
    metrics["identity_f1"] = f1_score(is_gt, is_pred, zero_division=0)

    # 内容安全分类指标
    metrics["safety_accuracy"] = accuracy_score(safety_gt, safety_pred)
    metrics["safety_macro_precision"] = precision_score(safety_gt, safety_pred, average="macro", zero_division=0)
    metrics["safety_macro_recall"] = recall_score(safety_gt, safety_pred, average="macro", zero_division=0)
    metrics["safety_macro_f1"] = f1_score(safety_gt, safety_pred, average="macro", zero_division=0)

    # 各类别F1
    safety_f1_per_class = f1_score(safety_gt, safety_pred, average=None, zero_division=0)
    metrics["safety_f1_safe"] = safety_f1_per_class[0]
    metrics["safety_f1_neutral"] = safety_f1_per_class[1]
    metrics["safety_f1_harmful"] = safety_f1_per_class[2]

    # harmful类重点关注
    metrics["safety_harmful_precision"] = precision_score(safety_gt == 2, safety_pred == 2, zero_division=0)
    metrics["safety_harmful_recall"] = recall_score(safety_gt == 2, safety_pred == 2, zero_division=0)
    metrics["safety_harmful_f1"] = f1_score(safety_gt == 2, safety_pred == 2, zero_division=0)

    return metrics
