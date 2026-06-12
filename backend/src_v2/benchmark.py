#!/usr/bin/env python3
"""
推理速度对比测试 - PyTorch vs ONNX Runtime

用法:
    python benchmark.py --pytorch_checkpoint outputs_v2_aug/best_model.pth --onnx_model kobe_detect_v2.onnx --batch_sizes 1 4 8 16
"""

import argparse
import time

import numpy as np
import torch
import torch.nn.functional as F

from models.clip_detector import CLIPDetector
from models.lora_config import get_clip_lora_config, apply_lora_to_clip
from infer_onnx import KobeDetectONNX


def benchmark_pytorch(model, pixel_values, warmup=10, runs=100):
    """PyTorch 推理基准测试"""
    model.eval()
    device = pixel_values.device

    # Warmup
    with torch.no_grad():
        for _ in range(warmup):
            _ = model(pixel_values)

    # Synchronize before timing
    if device.type == "cuda":
        torch.cuda.synchronize()

    # Benchmark
    start = time.perf_counter()
    with torch.no_grad():
        for _ in range(runs):
            _ = model(pixel_values)
    if device.type == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    avg_latency_ms = (elapsed / runs) * 1000
    batch_size = pixel_values.shape[0]
    throughput = (runs * batch_size) / elapsed
    return avg_latency_ms, throughput


def benchmark_onnx(session, pixel_values, warmup=10, runs=100):
    """ONNX Runtime 推理基准测试"""
    input_name = session.get_inputs()[0].name

    # Warmup
    for _ in range(warmup):
        _ = session.run(None, {input_name: pixel_values})

    # Benchmark
    start = time.perf_counter()
    for _ in range(runs):
        _ = session.run(None, {input_name: pixel_values})
    elapsed = time.perf_counter() - start

    avg_latency_ms = (elapsed / runs) * 1000
    batch_size = pixel_values.shape[0]
    throughput = (runs * batch_size) / elapsed
    return avg_latency_ms, throughput


def load_pytorch_model(checkpoint_path, device):
    """加载 PyTorch 模型"""
    model = CLIPDetector(device=device)
    lora_config = get_clip_lora_config(r=8, lora_alpha=16)
    model.clip = apply_lora_to_clip(model.clip, lora_config)
    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["model"], strict=False)
    model.clip = model.clip.merge_and_unload()
    model = model.to(device).eval()
    return model


def run_benchmarks(pytorch_model, onnx_session, batch_sizes, device):
    """运行所有基准测试"""
    results = []

    for bs in batch_sizes:
        print(f"\n{'='*60}")
        print(f"Batch Size: {bs}")
        print(f"{'='*60}")

        dummy_input = torch.randn(bs, 3, 224, 224).to(device)
        dummy_np = dummy_input.cpu().numpy()

        # PyTorch FP32 CUDA
        pt_latency, pt_throughput = benchmark_pytorch(pytorch_model, dummy_input)
        print(f"PyTorch FP32 CUDA: {pt_latency:.2f} ms | {pt_throughput:.1f} img/s")
        results.append({
            "backend": "PyTorch FP32",
            "device": device.type.upper(),
            "batch": bs,
            "latency_ms": pt_latency,
            "throughput": pt_throughput,
        })

        # ONNX FP32
        onnx_latency, onnx_throughput = benchmark_onnx(onnx_session, dummy_np)
        print(f"ONNX FP32:         {onnx_latency:.2f} ms | {onnx_throughput:.1f} img/s")
        results.append({
            "backend": "ONNX FP32",
            "device": device.type.upper(),
            "batch": bs,
            "latency_ms": onnx_latency,
            "throughput": onnx_throughput,
        })

        # Speedup
        speedup = pt_latency / onnx_latency
        print(f"Speedup:           {speedup:.2f}x")

    return results


def print_summary(results):
    """打印结果表格"""
    print(f"\n{'='*80}")
    print(f"Benchmark Summary")
    print(f"{'='*80}")
    print(f"{'Backend':<18} {'Device':<8} {'Batch':<8} {'Latency (ms)':<15} {'Throughput (img/s)':<20}")
    print("-" * 80)
    for r in results:
        print(f"{r['backend']:<18} {r['device']:<8} {r['batch']:<8} {r['latency_ms']:<15.2f} {r['throughput']:<20.1f}")
    print(f"{'='*80}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark PyTorch vs ONNX Runtime")
    parser.add_argument("--pytorch_checkpoint", type=str, required=True, help="Path to PyTorch checkpoint")
    parser.add_argument("--onnx_model", type=str, required=True, help="Path to ONNX model")
    parser.add_argument("--batch_sizes", type=int, nargs="+", default=[1, 4, 8, 16], help="Batch sizes to test")
    parser.add_argument("--device", type=str, default="cuda", help="Device: cuda or cpu")
    parser.add_argument("--warmup", type=int, default=10, help="Warmup iterations")
    parser.add_argument("--runs", type=int, default=100, help="Benchmark iterations")
    args = parser.parse_args()

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # 加载 PyTorch 模型
    print("Loading PyTorch model...")
    pytorch_model = load_pytorch_model(args.pytorch_checkpoint, device)

    # 加载 ONNX 模型
    print("Loading ONNX model...")
    onnx_model = KobeDetectONNX(args.onnx_model)
    onnx_session = onnx_model.session

    # 运行基准测试
    results = run_benchmarks(pytorch_model, onnx_session, args.batch_sizes, device)

    # 打印汇总
    print_summary(results)


if __name__ == "__main__":
    main()
