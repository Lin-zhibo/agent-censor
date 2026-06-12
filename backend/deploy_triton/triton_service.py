"""
Triton Inference Service using nvidia-pytriton Python backend.
Loads ONNX model via onnxruntime-gpu and serves via Triton HTTP API.
"""
import os
import logging

import numpy as np
import onnxruntime as ort
from pytriton.triton import Triton, TritonConfig
from pytriton.model_config.tensor import Tensor
from pytriton.decorators import batch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("triton_service")

MODEL_PATH = os.environ.get("MODEL_PATH", "/root/deploy_triton/triton/kobe_detect_v2/1/model.onnx")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))


def create_ort_session():
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    logger.info(f"Loading ONNX model from {MODEL_PATH}")
    logger.info(f"Available providers: {ort.get_available_providers()}")
    session = ort.InferenceSession(MODEL_PATH, sess_options, providers=providers)
    logger.info(f"Model loaded. Using provider: {session.get_providers()[0]}")
    return session


# Pre-load session at module level so it's ready before Triton starts
logger.info("Pre-loading ONNX session...")
_ORT_SESSION = create_ort_session()
logger.info("ONNX session ready.")


@batch
def infer_fn(pixel_values: np.ndarray) -> dict:
    """
    Inference function for nvidia-pytriton.
    pixel_values shape: [batch, 3, 224, 224]
    Returns dict with is_target_logits [batch, 1] and safety_logits [batch, 3].
    """
    outputs = _ORT_SESSION.run(
        None,
        {"pixel_values": pixel_values.astype(np.float32)},
    )
    # outputs[0]: is_target_logits [batch] -> reshape to [batch, 1]
    # outputs[1]: safety_logits [batch, 3]
    is_target = outputs[0]
    if is_target.ndim == 1:
        is_target = is_target.reshape(-1, 1)
    return {
        "is_target_logits": is_target,
        "safety_logits": outputs[1],
    }


def main():
    config = TritonConfig(
        http_port=HTTP_PORT,
        allow_http=True,
        allow_grpc=False,
        allow_metrics=True,
        metrics_port=8002,
        log_verbose=0,
    )

    triton = Triton(config=config)

    triton.bind(
        model_name="kobe_detect_v2",
        infer_func=infer_fn,
        inputs=[
            Tensor(shape=(3, 224, 224), dtype=np.float32, name="pixel_values"),
        ],
        outputs=[
            Tensor(shape=(1,), dtype=np.float32, name="is_target_logits"),
            Tensor(shape=(3,), dtype=np.float32, name="safety_logits"),
        ],
        model_version=1,
    )

    logger.info(f"Starting Triton server on HTTP port {HTTP_PORT}...")
    triton.serve()


if __name__ == "__main__":
    main()
