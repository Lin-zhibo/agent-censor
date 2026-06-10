from __future__ import annotations

from typing import Any, Mapping

from .rule_tree import build_rule_forest, load_settings_file
from .schema import (
    AuditContext,
    IntermediateAgentResult,
    LabelTreeNode,
    LeafLabelHit,
    NO_ISSUE_REASON,
    NO_ROUTE_REASON,
    RootAgentResult,
)
from .tool import AgentToolbox, LocalRuleToolbox


class LeafAgent:
    def __init__(self, node: LabelTreeNode, toolbox: AgentToolbox):
        self.node = node
        self.toolbox = toolbox
        self.name = f"{node.label}LeafAgent"

    def run(self, context: AuditContext) -> LeafLabelHit:
        route_decision = self.toolbox.model_route(context)
        model_result = self.toolbox.run_model_inference(context, route_decision)
        policy_rules = self.toolbox.query_policy_rule(context, self.node)
        rule_result = self.toolbox.evaluate_rule(context, self.node)
        rag_result = self.toolbox.graph_rag_search(context, [self.node.label])
        result = self.toolbox.evaluate_leaf(context, self.node)
        context.audit_events.append(
            {
                "agent": self.name,
                "node_id": self.node.node_id,
                "level": self.node.level,
                "domain": self.node.domain,
                "model_route": route_decision.to_dict(),
                "model_inference": model_result.to_dict(),
                "policy_rules": policy_rules.to_dict(),
                "rule_result": rule_result.to_dict(),
                "graph_rag": rag_result.to_dict(),
                "result": result.to_contract(),
                "path": list(self.node.path),
            }
        )
        return result


class TreeAgent:
    def __init__(
        self,
        node: LabelTreeNode,
        children: list["TreeAgent | LeafAgent"],
        toolbox: AgentToolbox,
    ):
        self.node = node
        self.children = children
        self.toolbox = toolbox
        self.name = f"{node.label.title()}Agent"

    def run(self, context: AuditContext) -> IntermediateAgentResult:
        route_result = _route_node(
            context=context,
            toolbox=self.toolbox,
            node=self.node,
            children=self.children,
            agent_name=self.name,
        )
        return route_result


class RootAgent:
    def __init__(
        self,
        children: list[TreeAgent | LeafAgent],
        toolbox: AgentToolbox,
    ):
        self.children = children
        self.toolbox = toolbox
        self.name = "RootAgent"

    def run(self, context: AuditContext) -> RootAgentResult:
        root_result = _route_node(
            context=context,
            toolbox=self.toolbox,
            node=None,
            children=self.children,
            agent_name=self.name,
        )

        leaf_results = list(root_result.hits)
        security_labels = [hit.label for hit in leaf_results if hit.domain == "security" and hit.is_hit]
        ecosystem_labels = [hit.label for hit in leaf_results if hit.domain == "ecosystem" and hit.is_hit]
        final_decision, suggested_action = _decide(leaf_results)
        reason = _root_reason(leaf_results)

        context.audit_events.append(
            {
                "agent": self.name,
                "node_id": "ROOT",
                "child_labels": list(root_result.child_labels),
                "security_labels": list(security_labels),
                "ecosystem_labels": list(ecosystem_labels),
                "reason": reason,
                "final_decision": final_decision,
                "suggested_action": suggested_action,
            }
        )

        return RootAgentResult(
            security_labels=security_labels,
            ecosystem_labels=ecosystem_labels,
            reason=reason,
            final_decision=final_decision,
            suggested_action=suggested_action,
            root_result=root_result,
            audit_trace=list(context.audit_events),
        )


class MultiAgentModerator:
    def __init__(self, root_agent: RootAgent):
        self.root_agent = root_agent

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        toolbox: AgentToolbox | None = None,
    ) -> "MultiAgentModerator":
        forest = build_rule_forest(settings)
        toolbox = toolbox or LocalRuleToolbox()

        root_children: list[TreeAgent | LeafAgent] = []
        for domain_label in ("SECURITY", "ECOSYSTEM"):
            domain_root = forest.get(domain_label)
            if domain_root is None:
                continue
            root_children.extend(_build_agent(child, toolbox) for child in domain_root.children)

        return cls(RootAgent(root_children, toolbox))

    @classmethod
    def from_settings_file(
        cls,
        path: str,
        toolbox: AgentToolbox | None = None,
    ) -> "MultiAgentModerator":
        return cls.from_settings(load_settings_file(path), toolbox=toolbox)

    def moderate(self, context: AuditContext | Mapping[str, Any]) -> RootAgentResult:
        if isinstance(context, AuditContext):
            audit_context = context
        else:
            payload = dict(context)
            payload.setdefault("trace_id", "")
            audit_context = AuditContext(**payload)
        return self.root_agent.run(audit_context)


def _build_agent(node: LabelTreeNode, toolbox: AgentToolbox) -> TreeAgent | LeafAgent:
    if node.is_leaf:
        return LeafAgent(node, toolbox)
    children = [_build_agent(child, toolbox) for child in node.children]
    return TreeAgent(node, children, toolbox)


def _route_node(
    context: AuditContext,
    toolbox: AgentToolbox,
    node: LabelTreeNode | None,
    children: list[TreeAgent | LeafAgent],
    agent_name: str,
) -> IntermediateAgentResult:
    child_nodes = [child.node for child in children]
    route_response = toolbox.route_children(context, node, child_nodes)
    child_labels = _normalize_child_labels(route_response.data.get("child_labels"))
    selected_children = _select_children(children, child_labels)

    child_results: list[Mapping[str, Any]] = []
    leaf_results: list[LeafLabelHit] = []
    for child in selected_children:
        result = child.run(context)
        child_results.append(result.to_contract())
        if isinstance(result, LeafLabelHit):
            if result.is_hit or result.needs_review:
                leaf_results.append(result)
        else:
            leaf_results.extend(result.hits)

    reason = str(route_response.data.get("reason") or NO_ROUTE_REASON)
    node_id = node.node_id if node is not None else "ROOT"
    route_result = IntermediateAgentResult(
        child_labels=child_labels,
        reason=reason,
        node_id=node_id,
        hits=leaf_results,
        child_results=child_results,
    )
    context.audit_events.append(
        {
            "agent": agent_name,
            "node_id": node_id,
            "level": node.level if node is not None else -1,
            "domain": node.domain if node is not None else "",
            "child_labels": list(child_labels),
            "reason": reason,
            "child_count": len(children),
            "selected_count": len(selected_children),
        }
    )
    return route_result


def _normalize_child_labels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _select_children(
    children: list[TreeAgent | LeafAgent],
    child_labels: list[str],
) -> list[TreeAgent | LeafAgent]:
    selected: list[TreeAgent | LeafAgent] = []
    index = 0
    for child in children:
        if index >= len(child_labels):
            break
        if child.node.label != child_labels[index]:
            continue
        selected.append(child)
        index += 1
    return selected


def _root_reason(leaf_results: list[LeafLabelHit]) -> str:
    reasons = [
        result.reason
        for result in leaf_results
        if result.reason and result.reason != NO_ISSUE_REASON and (result.is_hit or result.needs_review)
    ]
    return "；".join(reasons) if reasons else NO_ISSUE_REASON


def _decide(leaf_results: list[LeafLabelHit]) -> tuple[str, str]:
    security_results = [hit for hit in leaf_results if hit.domain == "security"]
    ecosystem_results = [hit for hit in leaf_results if hit.domain == "ecosystem"]

    security_actions = {hit.action for hit in security_results if hit.is_hit and hit.action}
    ecosystem_actions = {hit.action for hit in ecosystem_results if hit.is_hit and hit.action}

    if security_actions.intersection({"ban", "reject"}):
        return "reject", "ban"
    if any(hit.is_hit or hit.needs_review for hit in security_results):
        return "review", "manual_review"
    if ecosystem_actions.intersection({"limit", "pass_with_limit"}):
        return "pass_with_limit", "limit"
    if any(hit.is_hit or hit.needs_review for hit in ecosystem_results):
        return "review", "manual_review"
    return "pass", "pass"
