import os
import io
import base64
import time
import logging
from typing import List, Optional

import numpy as np
import requests
from PIL import Image
from fastapi import FastAPI, HTTPException
from transformers import CLIPProcessor

from schemas import (
    ModelInferenceRequest,
    ToolResponse,
    HealthResponse,
    ModelResult,
    LabelResult,
    Evidence,
    ErrorDetail,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

HF_ENDPOINT = os.environ.get("HF_ENDPOINT")
if HF_ENDPOINT:
    os.environ["HF_HUB_ENDPOINT"] = HF_ENDPOINT

TRITON_URL = os.environ.get("TRITON_URL", "http://localhost:8000")
MODEL_NAME = "kobe_detect_v2"
MODEL_VERSION = "v2.0"

app = FastAPI(title="KobeDetect Gateway", version="1.0.0")

processor: Optional[CLIPProcessor] = None


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def softmax(arr: np.ndarray) -> np.ndarray:
    exp_arr = np.exp(arr - np.max(arr))
    return exp_arr / np.sum(exp_arr)


@app.on_event("startup")
async def startup_event():
    global processor
    logger.info("Loading CLIPProcessor from openai/clip-vit-base-patch16...")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch16", local_files_only=True)
    logger.info("CLIPProcessor loaded.")


@app.get("/internal/v1/models/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    loaded = processor is not None
    return HealthResponse(
        status="healthy" if loaded else "loading",
        model_loaded=loaded,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
    )


def load_image(content_url: str) -> Image.Image:
    if content_url.startswith("http://") or content_url.startswith("https://"):
        resp = requests.get(content_url, timeout=30)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    elif content_url.startswith("data:image"):
        header, encoded = content_url.split(",", 1)
        data = base64.b64decode(encoded)
        img = Image.open(io.BytesIO(data)).convert("RGB")
    else:
        img = Image.open(content_url).convert("RGB")
    return img


@app.post("/internal/v1/models/infer", response_model=ToolResponse)
async def infer(request: ModelInferenceRequest) -> ToolResponse:
    start_time = time.perf_counter()
    trace_id = request.trace_id
    route = request.route_decision
    content = request.content

    def elapsed_ms() -> int:
        return int((time.perf_counter() - start_time) * 1000)

    if route.modality != "image":
        error = ErrorDetail(
            code="UNSUPPORTED_MODALITY",
            message=f"Modality '{route.modality}' not supported. Only 'image' is supported.",
            retryable=False,
        )
        return ToolResponse(
            status="error",
            data=None,
            errors=[error],
            latency_ms=elapsed_ms(),
            trace_id=trace_id,
        )

    try:
        img = load_image(content.url)
    except Exception as exc:
        logger.exception("Failed to load image")
        error = ErrorDetail(
            code="IMAGE_LOAD_FAILED",
            message=str(exc),
            retryable=True,
        )
        return ToolResponse(
            status="error",
            data=None,
            errors=[error],
            latency_ms=elapsed_ms(),
            trace_id=trace_id,
        )

    try:
        inputs = processor(images=img, return_tensors="np")
        pixel_values = inputs["pixel_values"]
    except Exception as exc:
        logger.exception("CLIP preprocessing failed")
        error = ErrorDetail(
            code="PREPROCESS_FAILED",
            message=str(exc),
            retryable=False,
        )
        return ToolResponse(
            status="error",
            data=None,
            errors=[error],
            latency_ms=elapsed_ms(),
            trace_id=trace_id,
        )

    triton_payload = {
        "inputs": [
            {
                "name": "pixel_values",
                "shape": list(pixel_values.shape),
                "datatype": "FP32",
                "data": pixel_values.flatten().tolist(),
            }
        ],
        "outputs": [
            {"name": "is_target_logits"},
            {"name": "safety_logits"},
        ],
    }

    infer_url = f"{TRITON_URL}/v2/models/{MODEL_NAME}/infer"
    triton_latency = 0
    try:
        triton_start = time.perf_counter()
        triton_resp = requests.post(infer_url, json=triton_payload, timeout=request.timeout_ms / 1000.0)
        triton_resp.raise_for_status()
        triton_data = triton_resp.json()
        triton_latency = int((time.perf_counter() - triton_start) * 1000)
    except Exception as exc:
        logger.exception("Triton inference request failed")
        error = ErrorDetail(
            code="TRITON_ERROR",
            message=str(exc),
            retryable=True,
        )
        return ToolResponse(
            status="error",
            data=None,
            errors=[error],
            latency_ms=elapsed_ms(),
            trace_id=trace_id,
        )

    try:
        outputs = {out["name"]: out for out in triton_data["outputs"]}
        is_target_logits = np.array(outputs["is_target_logits"]["data"], dtype=np.float32)
        safety_logits = np.array(outputs["safety_logits"]["data"], dtype=np.float32)

        is_target_prob = float(sigmoid(is_target_logits[0]))

        if is_target_prob < 0.5:
            safety_probs = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            safety_probs = softmax(safety_logits)

        labels = [
            LabelResult(
                label="SAFE",
                sub_label="",
                score=float(safety_probs[0]),
                normalized_score=float(safety_probs[0]),
            ),
            LabelResult(
                label="NEUTRAL",
                sub_label="",
                score=float(safety_probs[1]),
                normalized_score=float(safety_probs[1]),
            ),
            LabelResult(
                label="HARMFUL",
                sub_label="",
                score=float(safety_probs[2]),
                normalized_score=float(safety_probs[2]),
            ),
        ]

        identity_str = "目标人物" if is_target_prob >= 0.5 else "非目标人物"
        evidence = [
            Evidence(
                evidence_id="ev_kd_identity",
                type="model_score",
                content=f"人物识别: {identity_str} (is_target={is_target_prob:.4f})",
            )
        ]

        model_result = ModelResult(
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            modality="image",
            labels=labels,
            evidence=evidence,
            latency_ms=triton_latency,
            status="success",
            error=None,
        )
    except Exception as exc:
        logger.exception("Post-processing failed")
        error = ErrorDetail(
            code="POSTPROCESS_FAILED",
            message=str(exc),
            retryable=False,
        )
        return ToolResponse(
            status="error",
            data=None,
            errors=[error],
            latency_ms=elapsed_ms(),
            trace_id=trace_id,
        )

    return ToolResponse(
        status="success",
        data=model_result,
        errors=[],
        latency_ms=elapsed_ms(),
        trace_id=trace_id,
    )
