#!/usr/bin/env python3
"""
FastAPI REST API - ONNX Runtime 推理服务

用法:
    python api_onnx.py --model kobe_detect_v2.onnx --port 8000
    python api_onnx.py --model kobe_detect_v2.onnx --port 8000 --host 0.0.0.0
"""

import argparse
import io
from pathlib import Path

import numpy as np
from PIL import Image

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from infer_onnx import KobeDetectONNX


app = FastAPI(title="KobeDetect ONNX API", version="2.0")
model = None


@app.on_event("startup")
def startup_event():
    global model
    import sys
    args = sys.argv
    model_path = "kobe_detect_v2.onnx"
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            model_path = args[i + 1]
    print(f"Loading ONNX model from {model_path}...")
    model = KobeDetectONNX(model_path)
    print("Model loaded successfully.")


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "model_loaded": model is not None}


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    """
    单图预测
    上传图片，返回人物识别和内容安全分类结果
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        image = Image.open(io.BytesIO(await file.read())).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

    result = model.predict_single(image)

    return JSONResponse({
        "is_target": result["is_target"],
        "safety": result["safety"],
        "risk_score": result["risk_score"],
    })


@app.post("/batch_predict")
async def batch_predict(files: list[UploadFile] = File(...)):
    """
    批量预测
    上传多张图片，返回批量结果
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    images = []
    filenames = []
    for file in files:
        try:
            image = Image.open(io.BytesIO(await file.read())).convert("RGB")
            images.append(image)
            filenames.append(file.filename)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid image {file.filename}: {e}")

    results = model.predict(images)

    response = []
    for i, filename in enumerate(filenames):
        response.append({
            "filename": filename,
            "is_target": float(results["is_target"][i]),
            "safety": {
                "safe": float(results["safety_probs"][i][0]),
                "neutral": float(results["safety_probs"][i][1]),
                "harmful": float(results["safety_probs"][i][2]),
            },
            "risk_score": float(results["risk_score"][i]),
        })

    return JSONResponse({"results": response})


def main():
    parser = argparse.ArgumentParser(description="KobeDetect ONNX API Server")
    parser.add_argument("--model", type=str, default="kobe_detect_v2.onnx", help="ONNX model path")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    args = parser.parse_args()

    # 启动前预加载模型验证
    print(f"Pre-loading model from {args.model}...")
    _ = KobeDetectONNX(args.model)

    print(f"Starting API server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
