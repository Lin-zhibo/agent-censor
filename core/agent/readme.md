智能体代码。

当前模块实现了文档中的树型多智能体审核编排：

- 从 `config/settings.json` 的 `rules.security` 和 `rules.ecosystem` 构建标签树，并把 `domain` 透传到所有节点。
- `RootAgent` 和中间智能体只做当前层路由，输出 `child_labels`；编排器按这些 `label` 递归调用下一层。
- 叶子智能体返回 `{ "label": "...", "reason": "...", "needs_review": false, "evidence": [] }`。
- 外部 `moderate()` 返回 `security_labels`、`ecosystem_labels`、`reason` 和 `decision`，由编排器统一汇总生成。`decision` 取值：`ban`（security 命中）、`limit`（ecosystem 命中）、`pass`（无命中）。
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
