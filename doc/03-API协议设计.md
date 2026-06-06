# 03-API协议设计

## 1. 文档口径

本文档固定 Agent Censor 的目标 API 协议和核心数据结构。当前接口为建议实现，不表示后端代码已经完成。后端建议按 Go 服务设计，接口命名在后续文档和实现中保持一致。

## 2. 协议原则

- 使用 REST API，统一前缀 `/api/v1`。
- 使用 JSON 请求和响应。
- 使用 `tenant_id` 区分公司或租户。
- 使用 `business_id` 区分业务线或业务场景。
- 使用 `modality` 区分文本、图片、视频、音频等内容模态。
- 使用 `policy_id` 和 `version` 管理策略版本。
- 使用 `trace_id` 串联请求、模型、规则、RAG 证据和处置结果。
- 标签字段值统一使用大写英文单词，限制为 1 个 word，例如 `SECURITY`、`PORN`、`NUDITY`；中文名称和说明放在展示名或描述字段中。

## 3. 各层模块骨架

本文档中的 API 不只是接口格式约定，还对应目标系统中各层模块的调用边界。当前仓库仍处于早期骨架阶段，下表描述的是后续实现应遵循的目标模块划分。

### 3.1 模块职责与接口归属

| 模块 | 目标职责 | 提供或调用的接口 | 主要生产/消费对象 |
| --- | --- | --- | --- |
| Frontend | 审核工作台、策略配置、证据展示、人工复核、阈值预览 | 调用 `/api/v1` 对外接口 | 生产 `ModerationRequest`，消费 `ModerationResult`、任务状态、标签体系 |
| API Controller | 对外 REST 入口、鉴权、租户/业务/模态/策略校验、响应封装 | 提供 `/api/v1/moderation/tasks`、`/api/v1/moderation/batch`、`/api/v1/moderation/tasks/{task_id}`、`/api/v1/policies/{policy_id}/rules`、`/api/v1/labels` | 消费 `ModerationRequest`、`PolicyRule`，返回任务状态或审核结果 |
| Task Service | 创建任务、维护任务状态、判断同步或异步、生成 `task_id` 和 `trace_id` | 被 API Controller 调用，必要时写入任务队列 | 生产 `TaskContext`，更新任务状态 |
| Agent Orchestrator | 编排模型路由、模型推理、规则执行、Graph RAG 检索和解释生成 | 调用 `/internal/v1/models/*`、`/internal/v1/rules/*`、`/internal/v1/rag/search` | 消费 `TaskContext`，汇总 `ModelResult`、`RuleResult`、`GraphRagEvidence` |
| Model Router | 根据租户、业务、策略、模态和内容特征选择模型 | 提供 `POST /internal/v1/models/route` | 生产 `ModelRouteDecision` |
| Model Inference Service | 执行文本、图片、视频、音频或多模态推理 | 提供 `POST /internal/v1/models/infer` | 消费 `ModelInferenceRequest`，生产 `ModelResult` |
| Rule Engine | 查询策略规则、执行关键词/正则/model_score/label_combo/rag_node 条件 | 提供 `POST /internal/v1/rules/query`、`POST /internal/v1/rules/evaluate` | 消费 `PolicyRule`、`ModelResult`、RAG 证据，生产 `RuleResult` |
| Graph RAG Service | 进行向量召回、图谱邻域扩展和证据摘要 | 提供 `POST /internal/v1/rag/search` | 消费 `GraphRagSearchRequest`，生产 `GraphRagEvidence` |
| Decision Service | 融合模型、规则和 RAG 结果，形成最终审核结论 | 被 Agent Orchestrator 或 API 服务调用 | 生产 `ModerationResult` |
| Audit Service | 写入和查询审计轨迹，支撑复盘和连续对话 | 提供 `GET /internal/v1/audit/traces/{trace_id}` | 生产并返回 `AuditTrace` |
| Storage/DB | 存储租户、业务、策略、规则、任务、结果、审计、embedding 和图谱关系 | 被各服务读写 | 持久化任务状态、规则、结果、审计、向量和图谱数据 |

### 3.2 调用关系骨架

```mermaid
flowchart LR
  Frontend["Frontend<br/>审核工作台"]
  API["API Controller<br/>/api/v1"]
  Task["Task Service"]
  Queue["消息队列"]
  Worker["Task Worker"]
  Agent["Agent Orchestrator"]
  Router["Model Router"]
  Model["Model Inference Service"]
  Rule["Rule Engine"]
  Rag["Graph RAG Service"]
  Decision["Decision Service"]
  Audit["Audit Service"]
  DB[("Storage/DB")]

  Frontend -->|"ModerationRequest/PolicyRule"| API
  API --> Task
  Task -->|"同步 TaskContext"| Agent
  Task -->|"异步任务"| Queue
  Queue --> Worker
  Worker --> Agent
  Agent -->|"ModelRouteRequest"| Router
  Router -->|"ModelRouteDecision"| Agent
  Agent -->|"ModelInferenceRequest"| Model
  Model -->|"ModelResult"| Agent
  Agent -->|"RuleEvaluationRequest"| Rule
  Rule -->|"RuleResult"| Agent
  Agent -->|"GraphRagSearchRequest"| Rag
  Rag -->|"GraphRagEvidence"| Agent
  Agent --> Decision
  Decision -->|"ModerationResult"| API
  Decision --> Audit
  Audit -->|"AuditTrace"| DB
  Task --> DB
  Rule --> DB
  Rag --> DB
  API -->|"任务状态/审核结果"| Frontend
```

### 3.3 接口到模块映射

| 接口 | 提供模块 | 主要调用方 | 主要输入 | 主要输出 |
| --- | --- | --- | --- | --- |
| `POST /api/v1/moderation/tasks` | API Controller | Frontend、外部系统 | `ModerationRequest` | `ModerationResult` 或 `task_id`、`status` |
| `POST /api/v1/moderation/batch` | API Controller | Frontend、外部系统 | 多个 `ModerationRequest` | `batch_id`、任务列表、初始状态 |
| `GET /api/v1/moderation/tasks/{task_id}` | API Controller | Frontend、外部系统 | `task_id` | 任务状态、失败原因或 `ModerationResult` |
| `POST /api/v1/policies/{policy_id}/rules` | API Controller | 策略配置前端 | `PolicyRule` | 规则 ID、版本、启用状态 |
| `GET /api/v1/labels` | API Controller | Frontend、外部系统 | 可选过滤条件 | 标签体系、默认阈值、建议动作 |
| `POST /internal/v1/models/route` | Model Router | Agent Orchestrator | `ModelRouteRequest` | `ModelRouteDecision` |
| `POST /internal/v1/models/infer` | Model Inference Service | Agent Orchestrator、Task Worker | `ModelInferenceRequest` | `ModelResult` |
| `POST /internal/v1/rules/query` | Rule Engine | Agent Orchestrator、策略预览流程 | 规则查询条件 | `PolicyRule` 列表 |
| `POST /internal/v1/rules/evaluate` | Rule Engine | Agent Orchestrator、策略预览流程 | `RuleEvaluationRequest` | `RuleResult` 列表 |
| `POST /internal/v1/rag/search` | Graph RAG Service | Agent Orchestrator | `GraphRagSearchRequest` | `GraphRagEvidence` |
| `GET /internal/v1/audit/traces/{trace_id}` | Audit Service | Agent Orchestrator、运营追问流程 | `trace_id` | `AuditTrace` |
| `POST /internal/v1/policies/{policy_id}/preview` | Rule Engine 或 Decision Service | Frontend 经 API 服务触发、策略预览流程 | 阈值覆盖、模型结果、RAG 证据 | 预览版 `ModerationResult` 字段 |

## 4. 固定接口

### 4.1 提交单条审核任务

`POST /api/v1/moderation/tasks`

请求体为 `ModerationRequest`。后端目标行为：

- 校验租户、业务、模态和策略。
- 创建审核任务。
- 对可同步完成的轻量任务直接返回 `ModerationResult`。
- 对耗时任务返回 `task_id` 和任务状态，前端再查询结果。

### 4.2 提交批量审核任务

`POST /api/v1/moderation/batch`

请求体包含多个 `ModerationRequest`。后端目标行为：

- 为每条内容生成独立 `task_id`。
- 支持批量排队、批量模型推理和批量规则执行。
- 返回批次 ID、任务列表和初始状态。

### 4.3 查询审核结果

`GET /api/v1/moderation/tasks/{task_id}`

目标响应：

- 如果任务未完成，返回 `status`、排队位置或预计完成时间。
- 如果任务完成，返回 `ModerationResult`。
- 如果任务失败，返回失败原因和可重试状态。

### 4.4 新增或更新规则

`POST /api/v1/policies/{policy_id}/rules`

请求体为 `PolicyRule`。目标行为：

- 新增或更新策略下的规则。
- 生成规则版本。
- 返回规则 ID、版本和启用状态。
- 写入规则变更审计记录。

### 4.5 查询标签体系

`GET /api/v1/labels`

目标响应：

- 返回一级标签、二级标签、说明、默认阈值、建议处置动作。
- 支持按模态、业务或策略过滤。

## 5. 核心数据结构

### 5.1 ModerationRequest

```json
{
  "tenant_id": "tenant_demo",
  "business_id": "community_post",
  "modality": "text",
  "content": {
    "text": "待审核文本",
    "url": null,
    "metadata": {}
  },
  "policy_id": "default_policy",
  "detail_level": "detailed",
  "trace_id": "trace_20260604_0001"
}
```

字段说明：

- `tenant_id`：公司或租户 ID。
- `business_id`：业务线或业务场景 ID。
- `modality`：`text`、`image`、`video`、`audio`、`multimodal`。
- `content`：内容本体，可包含文本、文件地址、对象存储地址或元数据。
- `policy_id`：策略 ID。
- `detail_level`：`basic` 仅返回结论，`detailed` 返回标签、证据和解释。
- `trace_id`：链路追踪 ID。

产生/消费关系：`ModerationRequest` 由 Frontend 或外部系统产生，由 API Controller 校验，并由 Task Service 转换为 `TaskContext` 交给同步链路或异步队列。

### 5.2 ModerationResult

```json
{
  "task_id": "task_0001",
  "decision": "review",
  "risk_score": 0.82,
  "labels": [
    {
        "label": "PORN",
        "sub_label": "NUDITY",
      "score": 0.82
    }
  ],
  "evidence": [
    {
      "type": "text_span",
      "source": "rule_engine",
      "content": "命中证据片段",
      "start": 0,
      "end": 6
    }
  ],
  "model_results": [
    {
      "model_name": "text_safety_v1",
      "model_version": "2026-06",
      "modality": "text",
      "labels": [
        {
            "label": "PORN",
            "sub_label": "NUDITY",
          "score": 0.82,
          "normalized_score": 0.82
        }
      ],
      "evidence": [
        {
          "evidence_id": "ev_text_001",
          "type": "text_span",
          "content": "命中证据片段"
        }
      ],
      "latency_ms": 420,
      "status": "success",
      "error": null
    }
  ],
  "rule_results": [
    {
        "rule_id": "rule_porn_nudity_001",
      "version": "v1",
        "label": "PORN",
      "condition_type": "model_score",
      "threshold": 0.8,
      "observed_value": 0.82,
      "matched": true,
      "action": "review",
      "evidence_refs": ["ev_text_001"],
        "reason": "模型 PORN/NUDITY 分数 0.82 超过阈值 0.8"
    }
  ],
  "suggested_action": "manual_review",
    "explanation": "规则和模型均提示存在色情内容风险，建议人工复核。"
}
```

字段说明：

- `decision`：`pass`、`review`、`reject`。
- `risk_score`：综合风险分数，范围 0 到 1。
- `labels`：详细标签和分数。
- `evidence`：证据片段、检测框、视频时间段或 RAG 证据。
- `model_results`：模型推理结果。
- `rule_results`：规则命中结果。
- `suggested_action`：建议处置动作，例如 `manual_review`、`block`、`pass_with_limit`。

产生/消费关系：`ModerationResult` 由 Decision Service 产生，由 API Controller 返回给前端或外部系统；同步任务直接返回，异步任务通过 `GET /api/v1/moderation/tasks/{task_id}` 查询返回。

### 5.3 PolicyRule

```json
{
  "rule_id": "rule_sensitive_word_001",
  "label": "PORN",
  "condition": {
    "type": "keyword",
    "value": ["示例敏感词"]
  },
  "threshold": 0.8,
  "action": "review",
  "enabled": true,
  "version": "v1"
}
```

规则条件建议支持：

- `keyword`：关键词命中。
- `regex`：正则命中。
- `model_score`：模型标签分数超过阈值。
- `label_combo`：多个标签组合命中。
- `rag_node`：Graph RAG 命中特定政策或案例节点。

产生/消费关系：`PolicyRule` 由策略配置前端通过 API Controller 新增或更新，持久化后由 Rule Engine 查询和执行；规则变更必须写入审计。

### 5.4 ModelRouteDecision

```json
{
  "modality": "image",
  "selected_model": "image_safety_v1",
  "reason": "图片模态审核，优先选择图片安全模型",
  "fallback_model": "vision_general_baseline"
}
```

字段说明：

- `modality`：任务模态。
- `selected_model`：被选中的模型。
- `reason`：路由原因。
- `fallback_model`：主模型不可用时的降级模型。

产生/消费关系：`ModelRouteDecision` 由 Model Router 产生，由 Agent Orchestrator 消费，用于后续调用模型推理服务；fallback 信息必须进入审计轨迹。

### 5.5 树型智能体顶级返回对象

树型智能体顶级返回对象为内部对象，不新增对外 API。RootAgent 必须按以下结构返回：

```json
{
  "security_labels": ["LABEL1"],
  "ecosystem_labels": ["LABEL2"],
  "reason": ""
}
```

约束：

- `security_labels` 来自 `SECURITY` 子树。
- `ecosystem_labels` 来自 `ECOSYSTEM` 子树。
- `reason` 是 RootAgent 对各个子智能体 `reason` 的综合洞察。
- 父智能体不得修改、添加、删除收集到的子智能体标签，包括不改名、不去重、不排序、不新增、不删除。

产生/消费关系：该对象由 RootAgent 产生，由 Decision Service 和 Audit Service 消费；Decision Service 可基于该对象、规则结果、模型结果和 Graph RAG 证据生成 `ModerationResult`。

### 5.6 AuditTrace

```json
{
  "trace_id": "trace_20260604_0001",
  "task_id": "task_0001",
  "request_snapshot": {},
  "agent_tree_result": {
    "security_labels": ["LABEL1"],
    "ecosystem_labels": ["LABEL2"],
    "reason": ""
  },
  "model_results": [],
  "rule_results": [],
  "rag_evidence": [],
  "final_result": {},
  "human_action": null,
  "created_at": "2026-06-04T00:00:00+08:00"
}
```

`AuditTrace` 必须记录请求、树型智能体返回、模型、规则、RAG 证据、最终结果和人工处置结果，用于复盘、验收和问题定位。

产生/消费关系：`AuditTrace` 由 Audit Service 在审核链路末尾写入，由审计查询接口、连续对话和问题复盘流程消费。

## 6. 前端结果对象要求

前端审核结果对象必须能支持：

- 证据片段：文本 span、图片框、视频时间段、音频转写片段。
- 命中规则：规则 ID、规则名称、版本、命中条件。
- 风险等级：低、中、高或具体分数。
- 来源解释：模型来源、规则来源、Graph RAG 来源。
- 人工动作：通过、打回、改标、备注。

## 7. Graph RAG 结果对象要求

Graph RAG 检索结果在内部链路中统称为 `GraphRagEvidence`，由 Graph RAG Service 产生，由 Agent Orchestrator、Rule Engine 和 Decision Service 消费。该结果必须能支持：

- 命中节点：政策、案例、标签、规则、样本。
- 关系路径：例如“标签 -> 政策条款 -> 典型案例 -> 规则”。
- 相似度或置信度：用于排序和解释。
- 证据摘要：面向前端展示和审核解释。

## 8. 内部服务 API

内部服务 API 用于 Go API 服务、智能体、模型服务、规则引擎和 Graph RAG 服务之间通信。该部分为目标设计，后续实现应保持与对外 API 的字段命名一致。

### 8.1 通信方式

- 内部同步调用默认使用 HTTP/JSON，统一前缀 `/internal/v1`。
- 批量审核、视频推理、大文件处理、失败重试和离线评估使用任务队列异步处理。
- 所有内部请求必须携带 `trace_id`，并尽量携带 `task_id`、`tenant_id`、`business_id`、`policy_id`。
- 所有工具和内部服务响应统一包装为 `ToolResponse`。

```json
{
  "status": "success",
  "data": {},
  "errors": [],
  "latency_ms": 120,
  "trace_id": "trace_20260604_0001"
}
```

错误对象结构：

```json
{
  "code": "MODEL_TIMEOUT",
  "message": "模型推理超时",
  "retryable": true
}
```

### 8.2 模型路由

`POST /internal/v1/models/route`

请求体 `ModelRouteRequest`：

```json
{
  "trace_id": "trace_20260604_0001",
  "tenant_id": "tenant_demo",
  "business_id": "community_post",
  "policy_id": "default_policy",
  "modality": "image",
  "content_features": {
    "text_length": 0,
    "image_count": 1,
    "video_duration_sec": null,
    "language": null
  },
  "candidate_models": ["image_safety_v1", "vision_general_baseline"]
}
```

响应 `ToolResponse.data` 为 `ModelRouteDecision`。

### 8.3 模型推理

`POST /internal/v1/models/infer`

请求体 `ModelInferenceRequest`：

```json
{
  "trace_id": "trace_20260604_0001",
  "task_id": "task_0001",
  "route_decision": {
    "modality": "image",
    "selected_model": "image_safety_v1",
    "reason": "图片模态审核，优先选择图片安全模型",
    "fallback_model": "vision_general_baseline"
  },
  "content": {
    "url": "object://bucket/sample.jpg",
    "metadata": {}
  },
  "labels_requested": ["PORN"],
  "detail_level": "detailed",
  "timeout_ms": 3000
}
```

响应 `ToolResponse.data` 为 `ModelResult`：

```json
{
  "model_name": "image_safety_v1",
  "model_version": "2026-06",
  "modality": "image",
  "labels": [
    {
      "label": "PORN",
      "sub_label": "NUDITY",
      "score": 0.82,
      "normalized_score": 0.82
    }
  ],
  "evidence": [
    {
      "evidence_id": "ev_img_001",
      "type": "image_box",
      "content": "疑似色情裸露内容",
      "box": [120, 80, 240, 160]
    }
  ],
  "latency_ms": 780,
  "status": "success",
  "error": null
}
```

`ModelResult.labels[].normalized_score` 用于规则引擎跨模型比较；如模型只返回原始 `score`，后续实现应在模型服务或智能体侧归一化到 0 到 1。

### 8.4 规则查询

`POST /internal/v1/rules/query`

请求体：

```json
{
  "trace_id": "trace_20260604_0001",
  "tenant_id": "tenant_demo",
  "business_id": "community_post",
  "policy_id": "default_policy",
  "policy_version": "v1",
  "modality": "text",
  "labels": ["PORN"]
}
```

响应 `ToolResponse.data.rules` 为 `PolicyRule` 列表。

### 8.5 规则执行

`POST /internal/v1/rules/evaluate`

请求体 `RuleEvaluationRequest`：

```json
{
  "trace_id": "trace_20260604_0001",
  "task_id": "task_0001",
  "tenant_id": "tenant_demo",
  "business_id": "community_post",
  "policy_id": "default_policy",
  "policy_version": "v1",
  "modality": "text",
  "model_results": [],
  "rag_evidence": []
}
```

响应 `ToolResponse.data.rule_results` 为 `RuleResult` 列表：

```json
{
  "rule_id": "rule_model_score_001",
  "version": "v1",
  "label": "PORN",
  "condition_type": "model_score",
  "threshold": 0.8,
  "observed_value": 0.82,
  "matched": true,
  "action": "review",
  "evidence_refs": ["ev_text_001"],
  "reason": "模型分数超过策略阈值"
}
```

### 8.6 Graph RAG 检索

`POST /internal/v1/rag/search`

请求体 `GraphRagSearchRequest`：

```json
{
  "trace_id": "trace_20260604_0001",
  "query": "PORN NUDITY",
  "labels": ["PORN"],
  "evidence": [],
  "policy_id": "default_policy",
  "business_id": "community_post",
  "top_k": 5,
  "max_depth": 2,
  "node_types": ["Policy", "Case", "Rule", "Label"]
}
```

响应 `ToolResponse.data`：

```json
{
  "hits": [
    {
      "node_id": "policy_P001",
      "node_type": "Policy",
      "title": "色情内容审核政策",
      "similarity": 0.87,
      "confidence": 0.81,
      "summary": "该政策说明色情裸露内容需要进入复核。"
    }
  ],
  "paths": [
    {
      "path": ["PORN", "Policy:P001", "Rule:rule_model_score_001"],
      "score": 0.84
    }
  ],
  "evidence_summary": "命中色情内容标签相关政策和规则，建议复核。"
}
```

### 8.7 审计轨迹查询

`GET /internal/v1/audit/traces/{trace_id}`

响应 `ToolResponse.data` 为 `AuditTrace`。

### 8.8 策略阈值预览

`POST /internal/v1/policies/{policy_id}/preview`

请求体：

```json
{
  "trace_id": "trace_20260604_0001",
  "task_id": "task_0001",
  "policy_version": "v1",
  "threshold_overrides": {
    "PORN": 0.9
  },
  "model_results": [],
  "rag_evidence": []
}
```

响应 `ToolResponse.data` 返回预览后的 `decision`、`risk_score`、`labels`、`rule_results` 和 `suggested_action`，不写入正式审核结果。
