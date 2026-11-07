为智能体提供的工具放置于此。

当前约定：

- 非叶子节点通过 `route_children()` 选择当前层应继续调用的直接子节点 `label`。
- 叶子节点可按 `model_route -> run_model_inference -> query_policy_rule -> evaluate_rule -> graph_rag_search -> evaluate_leaf` 的顺序调用工具补充证据。
- `evaluate_leaf()` 接收 `graph_rag_result`，只能把 Graph RAG 作为叶子裁决证据，不能让 RAG 直接输出最终标签。
- `audit_trace_lookup` 用于连续对话或历史复盘，不要求在每次叶子判定时都调用。
