"""Agent Censor 试运行 — 使用 MultiAgentModerator 树形架构 + DashScope API

全链路：RootAgent → TreeAgent → TreeAgent → ... → LeafAgent
每层 route_children 只看到当前节点的直接子节点（3-15个），
每个 evaluate_leaf 只判定一条规则，永远不会把所有规则塞给模型。
"""
import json
from pathlib import Path
from typing import Any

import requests

from core.agent import (
    MultiAgentModerator,
    AuditContext,
    LabelTreeNode,
    LeafLabelHit,
    LeafGraphRagIndex,
    ToolResponse,
    NO_ISSUE_REASON,
    NO_ROUTE_REASON,
)


# ═══════════════════════════════════════════════════════════
# 1. 配置加载
# ═══════════════════════════════════════════════════════════

def load_env(path: str = "config/.env") -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


# ═══════════════════════════════════════════════════════════
# 2. DashScope Toolbox —— 实现 AgentToolbox 协议
#    route_children: 每层路由，只看到直接子节点
#    evaluate_leaf:  判定单条规则
# ═══════════════════════════════════════════════════════════

_SYSTEM = "你是内容安全审核助手。严格按 JSON 格式返回，不说废话。"

_ROUTE_PROMPT = """{parent_ctx}
请判断以下内容可以路由到哪些子类别做进一步审核（可多选）。
只要内容可能命中某个子类别或其下级规则，就必须选择该子类别继续下钻；宁可多选，不要在上层提前放过。

【可选子类别】
{children}

【合法标签清单（只能从以下标签中选择）】
{valid_labels}

【待审核内容】
{content}

返回 JSON：
{{"child_labels": [{label_examples}], "reason": "选择理由（30字以内）"}}
其中 child_labels 的每个元素必须是【合法标签清单】中的英文标签。
如都不相关返回 {{"child_labels": [], "reason": "与所有子类别无关"}}"""

_LEAF_PROMPT = """请判断以下内容是否违反指定规则。

【规则】{label}（{name}）
判定标准：{intro}

【Graph RAG证据】
{graph_rag_evidence}

【待审核内容】
{content}

返回 JSON：
{{"hit": true或false, "reason": "理由（30字以内）"}}
注意：Graph RAG只提供证据参考，不能替代你的审核判断；正常讨论、科普、新闻、学术研究、合法批评不命中。不确定就判 false。"""


def _format_child_knowledge(child: LabelTreeNode) -> str:
    lines = []
    desc = _first_rule_line(child.description) or child.description or child.display_name
    lines.append(f"   节点说明：{desc[:260]}")

    if child.children:
        snippets = []
        for leaf in _iter_leaves(child):
            leaf_desc = _first_rule_line(leaf.description) or leaf.description or leaf.display_name
            snippets.append(f"{leaf.display_name}（{leaf_desc[:120]}）")
        if snippets:
            lines.append(f"   [注意] 以下为下级规则覆盖范围，仅供理解本节点含义，不可直接选择：{'；'.join(snippets)[:1600]}")
    return "\n".join(lines)


def _iter_leaves(node: LabelTreeNode):
    if node.is_leaf:
        yield node
        return
    for child in node.children:
        yield from _iter_leaves(child)


def _first_rule_line(description: str) -> str:
    if not description:
        return ""
    first = description.splitlines()[0].strip()
    if first.startswith("1. 拦截条件："):
        return first.removeprefix("1. 拦截条件：").strip()
    return first


def _format_graph_rag_evidence(result: ToolResponse | None) -> str:
    if result is None:
        return "无 Graph RAG 证据。"
    if not result.ok:
        return "Graph RAG 检索失败，仅按规则定义判断。"
    data = result.data
    summary = str(data.get("evidence_summary", "")).strip()
    hits = data.get("hits", [])
    lines = [summary or "Graph RAG 未召回证据。"]
    if isinstance(hits, list):
        for hit in hits[:3]:
            if not isinstance(hit, dict):
                continue
            node_type = hit.get("node_type", "")
            title = hit.get("title", "")
            hit_summary = hit.get("summary", "")
            lines.append(f"- {node_type}: {title}；{hit_summary}")
    return "\n".join(lines)


def _extract_route_labels(parsed: dict[str, Any]) -> tuple[list[str], bool]:
    """返回 (labels, parse_ok). parse_ok=False 表示 labels 字段不是合法列表."""
    raw = parsed.get("child_labels")
    if raw is None:
        raw = parsed.get("labels", [])
    if not isinstance(raw, list):
        return [], False
    labels = [str(label).strip() for label in raw if str(label).strip()]
    return labels, True


class DashScopeToolbox:
    """每层用 AI 模型做路由和判定，不改动 MultiAgentModerator 的树形架构"""

    def __init__(self, env: dict[str, str], max_retries: int = 2):
        self.api_key = env.get("PRO_API_KEY", "")
        self.base_url = env.get("PRO_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = env.get("PRO_MODEL_NAME", "qwen-plus")
        self.max_retries = max_retries
        self.call_count = 0  # 统计 API 调用次数
        self.graph_rag_error = ""
        try:
            self.graph_rag = LeafGraphRagIndex.from_files(
                "config/settings.json",
                "data/knowledge.json",
            )
        except Exception as exc:
            self.graph_rag = None
            self.graph_rag_error = str(exc)

    # ── 核心协议方法 ──────────────────────────────────

    def route_children(
        self,
        context: AuditContext,
        node: LabelTreeNode | None,
        child_nodes: list[LabelTreeNode],
    ) -> ToolResponse:
        """在当前树节点，决定路由到哪些子节点。

        关键：只看到 child_nodes（当前节点的直接子节点，通常 3-15 个），
        不是全量规则。非法标签触发重试，耗尽后兜底全选。
        """
        text = context.text
        node_id = node.node_id if node else "ROOT"

        if not child_nodes:
            return ToolResponse(
                status="success",
                data={"child_labels": [], "reason": NO_ROUTE_REASON},
                trace_id=context.trace_id,
            )
        if not text.strip():
            return ToolResponse(
                status="success",
                data={"child_labels": [], "reason": "无文本内容"},
                trace_id=context.trace_id,
            )
        if not self.api_key:
            return self._no_route(context, node, "未配置模型 API，无法执行模型路由")

        # 构建上下文
        parent_ctx = ""
        if node:
            parent_ctx = f"当前审核节点：{node.display_name}"
            if node.description:
                parent_ctx += f"\n节点说明：{node.description[:300]}"

        child_lines = []
        for i, child in enumerate(child_nodes, 1):
            leaf_tag = " [叶子]" if child.is_leaf else ""
            child_lines.append(f"{i}. {child.label}{leaf_tag}（{child.display_name}）")
            child_lines.append(_format_child_knowledge(child))

        # 构建合法标签集合和动态 prompt 参数
        valid = {c.label for c in child_nodes}
        valid_labels_str = "、".join(sorted(valid))
        sorted_valid = sorted(valid)
        label_examples = '"' + '", "'.join(sorted_valid[:2]) + '"' if sorted_valid else '""'

        base_prompt = _ROUTE_PROMPT.format(
            parent_ctx=parent_ctx,
            children="\n".join(child_lines),
            valid_labels=valid_labels_str,
            label_examples=label_examples,
            content=text,
        )

        # 重试循环
        last_reason = NO_ROUTE_REASON
        for attempt in range(self.max_retries + 1):
            if attempt == 0:
                prompt = base_prompt
            else:
                feedback = (
                    f"\n\n[错误反馈] 你上次返回的标签不在合法列表中。"
                    f"合法标签只有：{valid_labels_str}。请重新选择。"
                )
                prompt = base_prompt + feedback

            try:
                parsed = self._chat(prompt)
            except Exception:
                if attempt < self.max_retries:
                    continue
                return self._route_all(context, node, child_nodes,
                                       reason="LLM 连续调用失败，兜底全选")

            if "error" in parsed:
                if attempt < self.max_retries:
                    continue
                return self._route_all(context, node, child_nodes,
                                       reason="LLM 返回错误，兜底全选")

            raw_labels, parse_ok = _extract_route_labels(parsed)
            last_reason = str(parsed.get("reason", NO_ROUTE_REASON))

            # 解析失败 → 重试
            if not parse_ok:
                if attempt < self.max_retries:
                    continue
                return self._route_all(context, node, child_nodes,
                                       reason="LLM 返回格式错误，兜底全选")

            # 空选是合法决策 → 立即返回
            if not raw_labels:
                return ToolResponse(
                    status="success",
                    data={
                        "node_id": node_id,
                        "child_labels": [],
                        "reason": last_reason,
                    },
                    trace_id=context.trace_id,
                )

            rejected = [label for label in raw_labels if label not in valid]

            # 全部合法 → 立即返回
            if not rejected:
                return ToolResponse(
                    status="success",
                    data={
                        "node_id": node_id,
                        "child_labels": raw_labels,
                        "reason": last_reason,
                        "raw_child_labels": raw_labels,
                    },
                    trace_id=context.trace_id,
                )

            # 有非法标签 → 继续重试

        # 循环耗尽 → fallback 全选
        return self._route_all(context, node, child_nodes,
                               reason=f"重试 {self.max_retries} 次仍返回非法标签，兜底全选")

    def evaluate_leaf(
        self,
        context: AuditContext,
        node: LabelTreeNode,
        graph_rag_result: ToolResponse | None = None,
    ) -> LeafLabelHit:
        """判定单条叶子规则。一次只判一条，prompt 极度聚焦。"""
        text = context.text

        if not text.strip():
            return self._no_hit(node)
        if not self.api_key:
            return self._no_hit(node)

        prompt = _LEAF_PROMPT.format(
            label=node.label,
            name=node.display_name,
            intro=node.description or node.display_name,
            graph_rag_evidence=_format_graph_rag_evidence(graph_rag_result),
            content=text,
        )

        try:
            parsed = self._chat(prompt)
        except Exception:
            return self._no_hit(node)

        if "error" in parsed:
            return self._no_hit(node)

        if not parsed.get("hit", False):
            return self._no_hit(node)

        evidence = [{"type": "model", "content": str(parsed.get("reason", ""))}]
        if graph_rag_result and graph_rag_result.ok:
            summary = str(graph_rag_result.data.get("evidence_summary", "")).strip()
            if summary:
                evidence.append({"type": "graph_rag", "content": summary})

        return LeafLabelHit(
            label=node.label,
            reason=str(parsed.get("reason", f"命中 {node.display_name}")),
            needs_review=bool(parsed.get("needs_review", False)),
            node_id=node.node_id,
            domain=node.domain,
            path=node.path,
            evidence=tuple(evidence),
            action=node.default_action,
            action_priority=node.action_priority,
        )

    # ── 其余协议方法（不影响路由和判定）───────────────

    def model_route(self, context: AuditContext) -> ToolResponse:
        return ToolResponse(status="success", data={"model": self.model}, trace_id=context.trace_id)

    def run_model_inference(self, context: AuditContext, route_decision: ToolResponse) -> ToolResponse:
        return ToolResponse(status="success", data={"model": self.model}, trace_id=context.trace_id)

    def query_policy_rule(self, context: AuditContext, node: LabelTreeNode) -> ToolResponse:
        return ToolResponse(status="success", data={"rules": list(node.rule_ids)}, trace_id=context.trace_id)

    def evaluate_rule(self, context: AuditContext, node: LabelTreeNode) -> ToolResponse:
        return ToolResponse(status="success", data={"label": node.label, "hit": False}, trace_id=context.trace_id)

    def graph_rag_search(self, context: AuditContext, labels: list[str]) -> ToolResponse:
        if self.graph_rag is None:
            return ToolResponse(
                status="success",
                data={
                    "hits": [],
                    "paths": [],
                    "evidence_summary": "Graph RAG 本地索引未加载。",
                    "index_error": self.graph_rag_error,
                },
                trace_id=context.trace_id,
            )
        data = self.graph_rag.search(
            query=context.text,
            labels=labels,
            top_k=5,
            max_depth=2,
        )
        return ToolResponse(status="success", data=data, trace_id=context.trace_id)

    def audit_trace_lookup(self, trace_id: str) -> ToolResponse:
        return ToolResponse(status="success", data={"audit_trace": []}, trace_id=trace_id)

    def threshold_preview(self, context: AuditContext, threshold_overrides: dict[str, float]) -> ToolResponse:
        return ToolResponse(status="success", data={"preview_only": True}, trace_id=context.trace_id)

    # ── 内部 ─────────────────────────────────────────

    def _chat(self, user_prompt: str) -> dict[str, Any]:
        self.call_count += 1
        resp = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 512,
            },
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        raw = body["choices"][0]["message"]["content"].strip()
        if not raw:
            return {"error": "模型返回空内容"}
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:]) if len(lines) > 1 else raw
            if raw.endswith("```"):
                raw = raw[:-3]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "JSON 解析失败", "raw": raw[:300]}

    def _no_hit(self, node: LabelTreeNode) -> LeafLabelHit:
        return LeafLabelHit(label="", reason=NO_ISSUE_REASON, node_id=node.node_id, domain=node.domain, path=node.path)

    def _route_all(
        self,
        context: AuditContext,
        node: LabelTreeNode | None,
        child_nodes: list[LabelTreeNode],
        reason: str,
    ) -> ToolResponse:
        """兜底：选择当前层所有子节点继续下钻."""
        return ToolResponse(
            status="success",
            data={
                "node_id": node.node_id if node else "ROOT",
                "child_labels": [c.label for c in child_nodes],
                "reason": reason,
                "fallback": True,
            },
            trace_id=context.trace_id,
        )

    def _no_route(
        self,
        context: AuditContext,
        node: LabelTreeNode | None,
        reason: str,
    ) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={
                "node_id": node.node_id if node is not None else "ROOT",
                "child_labels": [],
                "reason": reason or NO_ROUTE_REASON,
            },
            trace_id=context.trace_id,
        )


# ═══════════════════════════════════════════════════════════
# 3. 审核追踪 —— 解析 audit_trace 展示全链路
# ═══════════════════════════════════════════════════════════

def print_trace(trace: list[dict], indent: int = 0):
    """递归打印审核链路"""
    prefix = "  " * indent
    for event in trace:
        agent = event.get("agent", "?")
        node_id = event.get("node_id", "?")
        level = event.get("level", "?")
        domain = event.get("domain", "")
        domain_tag = f"[{domain}]" if domain else ""

        if "child_labels" in event and "child_count" in event:
            # Root 和中间节点路由。RootAgent 也会产生路由事件，必须先于最终汇总事件识别。
            selected = event.get("selected_count", 0)
            total = event.get("child_count", 0)
            reason = event.get("reason", "")
            children = event.get("child_labels", [])
            rejected = event.get("rejected_child_labels", [])
            if agent == "RootAgent":
                route_name = "[Root路由]"
            else:
                route_name = f"[L{level}路由]"
            print(f"{prefix}{route_name} {agent} {domain_tag} -> 路由到 {selected}/{total} 子节点: {children}")
            if reason:
                print(f"{prefix}       路由理由: {reason}")
            if rejected:
                print(f"{prefix}       丢弃无效label: {rejected}")
                print(f"{prefix}       当前层合法label: {event.get('valid_child_labels', [])}")
        elif agent == "RootAgent":
            print(f"{prefix}[Root汇总] decision={event.get('decision','?')}")
            reason = event.get("reason", "")
            if reason and reason != NO_ISSUE_REASON:
                print(f"{prefix}       汇总理由: {reason}")
        elif "result" in event:
            # 叶子节点（判定）
            result = event.get("result", {})
            label = result.get("label", "")
            reason = result.get("reason", "")
            hit = bool(label)
            if hit:
                print(f"{prefix}[L{level}] {agent} {domain_tag} -> 命中 {label}: {reason}")
            else:
                print(f"{prefix}[L{level}] {agent} {domain_tag} -> 未命中")


# ═══════════════════════════════════════════════════════════
# 4. 主入口
# ═══════════════════════════════════════════════════════════

def main():
    env = load_env()
    if not env.get("PRO_API_KEY"):
        print("[错误] 未配置 PRO_API_KEY，请在 config/.env 中设置")
        return

    # 读取路由重试配置
    settings_path = Path("config/settings.json")
    max_retries = 2
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text("utf-8"))
            raw = int(settings.get("route_max_retries", 2))
            max_retries = max(0, min(raw, 5))
        except (json.JSONDecodeError, (TypeError, ValueError)):
            max_retries = 2

    toolbox = DashScopeToolbox(env, max_retries=max_retries)
    moderator = MultiAgentModerator.from_settings_file("config/settings.json", toolbox=toolbox)

    print(f"API:  {env['PRO_URL']}")
    print(f"模型: {env['PRO_MODEL_NAME']}")
    print(f"Key:  已配置\n")

    test_cases = [
        "今天天气真好，适合出去玩",
        "加我微信 xxx，进群有福利，懂的来",
        "你这个废物，全家都是垃圾，去死吧",
        "代办假证 学历证书 贷款 征信修复 联系QQ12345",
        "裸聊上门服务包夜，加微信看照片",
    ]

    for i, text in enumerate(test_cases, 1):
        toolbox.call_count = 0
        print(f"\n{'='*60}")
        print(f"测试 {i}: {text}")
        print(f"{'='*60}")

        result = moderator.moderate(
            AuditContext(trace_id=f"test_{i}", content={"text": text})
        )

        # 结果摘要
        if result.security_labels:
            print(f"结果: [安全] 命中 {result.security_labels}")
        if result.ecosystem_labels:
            print(f"      [生态] 命中 {result.ecosystem_labels}")
        if not result.security_labels and not result.ecosystem_labels:
            print(f"结果: [通过] 未命中任何规则")
        print(f"决策: {result.decision}")
        if result.reason and result.reason != NO_ISSUE_REASON:
            print(f"理由: {result.reason}")

        # 完整链路
        print(f"\n[审核链路] ({toolbox.call_count} 次 API 调用):")
        print_trace(result.audit_trace)

        print()


if __name__ == "__main__":
    main()
