智能体代码。

当前模块实现了文档中的树型多智能体审核编排：

- 从 `config/settings.json` 的 `rules.security` 和 `rules.ecosystem` 构建 `SECURITY` / `ECOSYSTEM` 两棵标签树。
- `RootAgent` 调用两个 0 级子智能体；中间智能体只汇总子智能体标签，不改名、不去重、不排序。
- 叶子智能体返回 `{ "label": "...", "reason": "..." }`，未命中时 `label` 为空字符串。
- 顶级返回 `security_labels`、`ecosystem_labels`、`reason`、`final_decision` 和 `suggested_action`。
- 默认 `LocalRuleToolbox` 支持显式命中标签、规则结果和关键词命中；后续可替换为内部 HTTP 工具实现。

示例：

```python
from core.agent import AuditContext, MultiAgentModerator

moderator = MultiAgentModerator.from_settings_file("config/settings.json")
result = moderator.moderate(
    AuditContext(
        trace_id="trace_demo",
        content={"text": "待审核文本"},
        metadata={"matched_labels": ["LABEL11"]},
    )
)

print(result.to_contract())
```
