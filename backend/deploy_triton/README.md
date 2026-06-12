# KobeDetect v2 - Triton Inference Server Deployment

本目录提供基于 NVIDIA Triton Inference Server 的 KobeDetect v2 模型生产部署方案，包含 Triton 服务与 FastAPI Gateway 的 Docker Compose 编排。

---

## 目录结构

```
deploy_triton/
├── docker-compose.yml          # Docker Compose 编排文件
├── gateway/                    # FastAPI 网关服务
│   ├── Dockerfile
│   ├── main.py
│   ├── requirements.txt
│   └── schemas.py
├── triton/
│   └── kobe_detect_v2/         # Triton 模型仓库
│       ├── 1/
│       │   └── model.onnx
│       └── config.pbtxt
└── README.md                   # 本文档
```

---

## 前置条件

- [Docker](https://docs.docker.com/get-docker/) >= 20.10
- [Docker Compose](https://docs.docker.com/compose/install/) >= 1.29
- [NVIDIA Docker Runtime](https://github.com/NVIDIA/nvidia-docker) (nvidia-docker2)
- NVIDIA GPU (计算能力 >= 6.0)

验证 NVIDIA Docker 运行时：

```bash
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.0-base nvidia-smi
```

---

## 快速开始

```bash
# 1. 进入部署目录
cd deploy_triton

# 2. 启动服务（首次会自动构建 Gateway 镜像）
docker-compose up --build

# 3. 后台运行
docker-compose up -d --build

# 4. 查看日志
docker-compose logs -f

# 5. 停止服务
docker-compose down
```

---

## 服务端口

| 服务 | 地址 | 说明 |
|------|------|------|
| Gateway | http://localhost:8001 | 业务网关，agent-censor 协议 |
| Triton HTTP | http://localhost:8000 | Triton 原生推理接口 |
| Triton Metrics | http://localhost:8002/metrics | Prometheus 监控指标 |

---

## 架构

```
┌─────────────┐      ┌──────────────┐      ┌─────────────────┐
│   Client    │─────▶│   Gateway    │─────▶│     Triton      │
│             │      │  (FastAPI)   │      │ Inference Server│
│  Agent SDK  │      │ :8001        │      │  :8000          │
└─────────────┘      └──────────────┘      └─────────────────┘
                            │                       │
                            ▼                       ▼
                     预处理 (CPU)           模型推理 (GPU)
                     协议转换               ONNX Runtime
```

**数据流：**
1. Client 通过 agent-censor 协议发送请求到 Gateway (:8001)
2. Gateway 进行输入校验、预处理（CPU）
3. Gateway 将预处理后的张量通过 Triton HTTP 协议转发到 Triton (:8000)
4. Triton 在 GPU 上执行 ONNX 模型推理
5. 结果经 Gateway 后处理，按协议格式返回 Client

---

## API 示例

### POST /internal/v1/models/infer

**请求（agent-censor 协议）：**

```bash
curl -X POST http://localhost:8001/internal/v1/models/infer \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "test-001",
    "task_id": "task-001",
    "route_decision": {
      "modality": "image",
      "selected_model": "kobedetect",
      "reason": "primary routing"
    },
    "content": {
      "url": "/path/to/image.jpg",
      "text": null
    },
    "labels_requested": [],
    "detail_level": "detailed",
    "timeout_ms": 3000
  }'
```

**响应（ToolResponse 包装 ModelResult）：**

```json
{
  "status": "success",
  "data": {
    "model_name": "kobe_detect_v2",
    "model_version": "v2.0",
    "modality": "image",
    "labels": [
      {"label": "SAFE", "sub_label": "", "score": 1.0, "normalized_score": 1.0},
      {"label": "NEUTRAL", "sub_label": "", "score": 0.0, "normalized_score": 0.0},
      {"label": "HARMFUL", "sub_label": "", "score": 0.0, "normalized_score": 0.0}
    ],
    "evidence": [
      {
        "evidence_id": "ev_kd_identity",
        "type": "model_score",
        "content": "人物识别: 目标人物 (is_target=0.6100)"
      }
    ],
    "latency_ms": 85,
    "status": "success",
    "error": null
  },
  "errors": [],
  "latency_ms": 120,
  "trace_id": "test-001"
}
```

---

## 监控

Triton 内置 Prometheus 指标，暴露于 `http://localhost:8002/metrics`。

**关键指标：**

| 指标 | 说明 |
|------|------|
| `nv_inference_request_success` | 推理成功次数 |
| `nv_inference_request_fail` | 推理失败次数 |
| `nv_inference_compute_infer_duration_us` | 推理计算耗时 (us) |
| `nv_inference_queue_duration_us` | 排队耗时 (us) |
| `nv_gpu_utilization` | GPU 利用率 |
| `nv_gpu_memory_used_bytes` | GPU 显存使用 |

**Prometheus 配置片段：**

```yaml
scrape_configs:
  - job_name: 'triton'
    static_configs:
      - targets: ['localhost:8002']
```

---

## 扩展

### 单 GPU 多实例

编辑 `triton/kobe_detect_v2/config.pbtxt`，增加 `instance_group` 数量：

```protobuf
instance_group [
  {
    count: 2
    kind: KIND_GPU
    gpus: [0]
  }
]
```

Triton 将在同一张 GPU 上启动 2 个模型实例，通过动态批处理提升吞吐。

### 多 GPU 扩展

修改 `docker-compose.yml`：

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 2          # 使用 2 张 GPU
          capabilities: [gpu]
```

并调整 `config.pbtxt`：

```protobuf
instance_group [
  {
    count: 1
    kind: KIND_GPU
    gpus: [0, 1]          # 分布在 2 张 GPU 上
  }
]
```

### 多节点部署

生产环境建议使用 Kubernetes + Triton Helm Chart：

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm install triton-inference-server nvidia/triton-inference-server \
  --set image.imageName=nvcr.io/nvidia/tritonserver:24.01-py3 \
  --set modelRepositoryPath=gs://your-bucket/models
```

---

## 故障排查

| 问题 | 排查步骤 |
|------|----------|
| Triton 启动失败 | 检查 `triton/kobe_detect_v2/config.pbtxt` 格式；确认 `model.onnx` 存在 |
| GPU 不可见 | 运行 `nvidia-smi` 验证驱动；检查 `nvidia-docker2` 安装 |
| Gateway 连接 Triton 失败 | 确认 Triton healthcheck 通过；检查 `TRITON_URL` 环境变量 |
| 推理延迟高 | 查看 `:8002/metrics` 中的 queue_duration；考虑增加 instance_group count |

---

## 验证与测试

部署完成后，按以下步骤验证服务是否正常。

### 1. Health 检查

```bash
curl http://localhost:8001/internal/v1/models/health
```

预期响应：
```json
{
  "status": "healthy",
  "model_loaded": true,
  "model_name": "kobe_detect_v2",
  "model_version": "v2.0"
}
```

### 2. 推理测试

```bash
curl -X POST http://localhost:8001/internal/v1/models/infer \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "test-001",
    "task_id": "task-001",
    "route_decision": {
      "modality": "image",
      "selected_model": "kobedetect",
      "reason": "test"
    },
    "content": {
      "url": "https://picsum.photos/224/224",
      "text": null
    },
    "labels_requested": [],
    "detail_level": "detailed",
    "timeout_ms": 3000
  }'
```

### 3. Triton 原生接口验证

```bash
# 查看模型状态
curl http://localhost:8000/v2/models/kobe_detect_v2

# 查看模型统计（含推理计数、延迟）
curl http://localhost:8000/v2/models/kobe_detect_v2/stats
```

### 4. 性能压测（可选）

使用 `ab` 或 `wrk` 进行并发压测：

```bash
# 安装 wrk
# brew install wrk  # macOS
# apt-get install wrk # Ubuntu

# 运行压测（10 线程，100 并发，30 秒）
wrk -t10 -c100 -d30s -s infer.lua http://localhost:8001/internal/v1/models/infer
```

---

## 参考

- [Triton Inference Server 文档](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/)
- [Triton ONNX Backend](https://github.com/triton-inference-server/onnxruntime_backend)
- [NVIDIA NGC Triton 容器](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/tritonserver)
