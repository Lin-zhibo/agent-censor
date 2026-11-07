智能体代码。

当前模块实现了文档中的树型多智能体审核编排：

- 从 `config/settings.json` 的 `rules.security` 和 `rules.ecosystem` 构建标签树，并把 `domain` 透传到所有节点。
- `RootAgent` 和中间智能体只做当前层路由，输出 `child_labels`；编排器按这些 `label` 递归调用下一层。
- 叶子智能体返回 `{ "label": "...", "reason": "...", "needs_review": false, "evidence": [] }`。
- 叶子智能体在裁决前可调用 Graph RAG；RAG 结果只作为叶子证据输入和审计日志，不参与 RootAgent 或中间智能体路由。
- 外部 `moderate()` 返回 `security_labels`、`ecosystem_labels`、`reason` 和 `decision`，由编排器统一汇总生成。`decision` 取值：`ban`（security 命中）、`limit`（ecosystem 命中）、`pass`（无命中）。
- 调用方必须显式提供实现 `AgentToolbox` 协议的工具；系统不提供本地文本匹配兜底。
- `data/knowledge.json` 中的 `keywords` 只作为 Graph RAG 知识节点加载，不允许作为本地关键词命中规则。

示例：

```python
from core.agent import AuditContext, MultiAgentModerator
from your_toolbox import ModelBackedToolbox

toolbox = ModelBackedToolbox()
moderator = MultiAgentModerator.from_settings_file(
    "config/settings.json",
    toolbox=toolbox,
)
result = moderator.moderate(
    AuditContext(
        trace_id="trace_demo",
        content={"text": "待审核文本"},
    )
)

print(result.to_contract())
```
