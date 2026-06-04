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

## 3. 固定接口

### 3.1 提交单条审核任务

`POST /api/v1/moderation/tasks`

请求体为 `ModerationRequest`。后端目标行为：

- 校验租户、业务、模态和策略。
- 创建审核任务。
- 对可同步完成的轻量任务直接返回 `ModerationResult`。
- 对耗时任务返回 `task_id` 和任务状态，前端再查询结果。

### 3.2 提交批量审核任务

`POST /api/v1/moderation/batch`

请求体包含多个 `ModerationRequest`。后端目标行为：

- 为每条内容生成独立 `task_id`。
- 支持批量排队、批量模型推理和批量规则执行。
- 返回批次 ID、任务列表和初始状态。

### 3.3 查询审核结果

`GET /api/v1/moderation/tasks/{task_id}`

目标响应：

- 如果任务未完成，返回 `status`、排队位置或预计完成时间。
- 如果任务完成，返回 `ModerationResult`。
- 如果任务失败，返回失败原因和可重试状态。

### 3.4 新增或更新规则

`POST /api/v1/policies/{policy_id}/rules`

请求体为 `PolicyRule`。目标行为：

- 新增或更新策略下的规则。
- 生成规则版本。
- 返回规则 ID、版本和启用状态。
- 写入规则变更审计记录。

### 3.5 查询标签体系

`GET /api/v1/labels`

目标响应：

- 返回一级标签、二级标签、说明、默认阈值、建议处置动作。
- 支持按模态、业务或策略过滤。

## 4. 核心数据结构

### 4.1 ModerationRequest

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

### 4.2 ModerationResult

```json
{
  "task_id": "task_0001",
  "decision": "review",
  "risk_score": 0.82,
  "labels": [
    {
      "label": "political_sensitive",
      "sub_label": "political_symbol",
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
  "model_results": [],
  "rule_results": [],
  "suggested_action": "manual_review",
  "explanation": "规则和模型均提示存在政治敏感风险，建议人工复核。"
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

### 4.3 PolicyRule

```json
{
  "rule_id": "rule_sensitive_word_001",
  "label": "political_sensitive",
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

### 4.4 ModelRouteDecision

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

### 4.5 AuditTrace

```json
{
  "trace_id": "trace_20260604_0001",
  "task_id": "task_0001",
  "request_snapshot": {},
  "model_results": [],
  "rule_results": [],
  "rag_evidence": [],
  "final_result": {},
  "human_action": null,
  "created_at": "2026-06-04T00:00:00+08:00"
}
```

`AuditTrace` 必须记录请求、模型、规则、RAG 证据、最终结果和人工处置结果，用于复盘、验收和问题定位。

## 5. 前端结果对象要求

前端审核结果对象必须能支持：

- 证据片段：文本 span、图片框、视频时间段、音频转写片段。
- 命中规则：规则 ID、规则名称、版本、命中条件。
- 风险等级：低、中、高或具体分数。
- 来源解释：模型来源、规则来源、Graph RAG 来源。
- 人工动作：通过、打回、改标、备注。

## 6. Graph RAG 结果对象要求

Graph RAG 检索结果必须能支持：

- 命中节点：政策、案例、标签、规则、样本。
- 关系路径：例如“标签 -> 政策条款 -> 典型案例 -> 规则”。
- 相似度或置信度：用于排序和解释。
- 证据摘要：面向前端展示和审核解释。

