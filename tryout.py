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
请判断以下内容可以路由到哪些子类别做进一步审核（可多选，宁少勿多）。

【可选子类别】
{children}

【待审核内容】
{content}

返回 JSON：
{{"labels": ["label1"], "reason": "选择理由（30字以内）"}}
如都不相关返回 {{"labels": [], "reason": "与所有子类别无关"}}"""

_LEAF_PROMPT = """请判断以下内容是否违反指定规则。

【规则】{label}（{name}）
判定标准：{intro}

【待审核内容】
{content}

返回 JSON：
{{"hit": true或false, "reason": "理由（30字以内）"}}
注意：正常讨论、科普、新闻、学术研究、合法批评不命中。不确定就判 false。"""


class DashScopeToolbox:
    """每层用 AI 模型做路由和判定，不改动 MultiAgentModerator 的树形架构"""

    def __init__(self, env: dict[str, str]):
        self.api_key = env.get("PRO_API_KEY", "")
        self.base_url = env.get("PRO_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = env.get("PRO_MODEL_NAME", "qwen-plus")
        self.call_count = 0  # 统计 API 调用次数

    # ── 核心协议方法 ──────────────────────────────────

    def route_children(
        self,
        context: AuditContext,
        node: LabelTreeNode | None,
        child_nodes: list[LabelTreeNode],
    ) -> ToolResponse:
        """在当前树节点，决定路由到哪些子节点。

        关键：只看到 child_nodes（当前节点的直接子节点，通常 3-15 个），
        不是全量规则。
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
            return self._local_route(context, node, child_nodes)

        # 构建当前节点的上下文 + 子节点列表
        parent_ctx = ""
        if node:
            parent_ctx = f"当前审核节点：{node.display_name}"
            if node.description:
                parent_ctx += f"\n节点说明：{node.description[:300]}"

        child_lines = []
        for i, child in enumerate(child_nodes, 1):
            desc = child.description or child.display_name
            leaf_tag = " [叶子]" if child.is_leaf else ""
            child_lines.append(f"{i}. {child.label}{leaf_tag}（{child.display_name}）")
            child_lines.append(f"   {desc[:400]}")

        prompt = _ROUTE_PROMPT.format(
            parent_ctx=parent_ctx,
            children="\n".join(child_lines),
            content=text,
        )

        try:
            parsed = self._chat(prompt)
        except Exception:
            return self._local_route(context, node, child_nodes)

        if "error" in parsed:
            return self._local_route(context, node, child_nodes)

        selected = [str(l) for l in parsed.get("labels", []) if str(l)]
        valid = {c.label for c in child_nodes}
        selected = [l for l in selected if l in valid]
        reason = str(parsed.get("reason", NO_ROUTE_REASON))

        return ToolResponse(
            status="success",
            data={
                "node_id": node_id,
                "child_labels": selected,
                "reason": reason,
            },
            trace_id=context.trace_id,
        )

    def evaluate_leaf(self, context: AuditContext, node: LabelTreeNode) -> LeafLabelHit:
        """判定单条叶子规则。一次只判一条，prompt 极度聚焦。"""
        text = context.text

        if not text.strip():
            return self._no_hit(node)
        if not self.api_key:
            return self._local_leaf(context, node)

        prompt = _LEAF_PROMPT.format(
            label=node.label,
            name=node.display_name,
            intro=node.description or node.display_name,
            content=text,
        )

        try:
            parsed = self._chat(prompt)
        except Exception:
            return self._local_leaf(context, node)

        if "error" in parsed:
            return self._local_leaf(context, node)

        if not parsed.get("hit", False):
            return self._no_hit(node)

        return LeafLabelHit(
            label=node.label,
            reason=str(parsed.get("reason", f"命中 {node.display_name}")),
            needs_review=bool(parsed.get("needs_review", False)),
            node_id=node.node_id,
            domain=node.domain,
            path=node.path,
            evidence=({"type": "model", "content": str(parsed.get("reason", ""))},),
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
        return ToolResponse(status="success", data={"hits": []}, trace_id=context.trace_id)

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

    def _keyword_match(self, text: str, keywords: tuple[str, ...]) -> str:
        t = text.lower()
        for kw in keywords:
            if kw and kw.lower() in t:
                return kw
        return ""

    def _local_leaf(self, context: AuditContext, node: LabelTreeNode) -> LeafLabelHit:
        text = context.text
        kw = self._keyword_match(text, node.keywords)
        if kw:
            return LeafLabelHit(
                label=node.label, reason=f"命中关键词: {kw}",
                node_id=node.node_id, domain=node.domain, path=node.path,
                evidence=({"type": "keyword", "content": kw},),
                action=node.default_action,
            )
        return self._no_hit(node)

    def _local_route(self, context: AuditContext, node: LabelTreeNode | None, child_nodes: list[LabelTreeNode]) -> ToolResponse:
        text = context.text.lower()
        labels, reasons = [], []
        for child in child_nodes:
            kw = self._keyword_match(text, child.keywords)
            if kw:
                labels.append(child.label)
                reasons.append(f"{child.label}: kw {kw}")
            else:
                for sub in child.children:
                    skw = self._keyword_match(text, sub.keywords)
                    if skw:
                        labels.append(child.label)
                        reasons.append(f"{child.label}: sub kw {skw}")
                        break
        return ToolResponse(
            status="success",
            data={
                "child_labels": labels,
                "reason": "；".join(reasons) if reasons else NO_ROUTE_REASON,
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

        if agent == "RootAgent":
            print(f"{prefix}[Root] decision={event.get('decision','?')}")
        elif "child_labels" in event and "child_count" in event:
            # 中间节点（路由）
            selected = event.get("selected_count", 0)
            total = event.get("child_count", 0)
            reason = event.get("reason", "")
            children = event.get("child_labels", [])
            print(f"{prefix}[L{level}] {agent} {domain_tag} -> 路由到 {selected}/{total} 子节点: {children}")
            if reason:
                print(f"{prefix}       理由: {reason}")
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

    toolbox = DashScopeToolbox(env)
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
