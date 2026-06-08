from __future__ import annotations

from typing import Any, Mapping

from .rule_tree import build_rule_forest, load_settings_file
from .schema import (
    AuditContext,
    IntermediateAgentResult,
    LabelTreeNode,
    LeafLabelHit,
    NO_ISSUE_REASON,
    RootAgentResult,
)
from .tool import AgentToolbox, LocalRuleToolbox


class LeafAgent:
    def __init__(self, node: LabelTreeNode, toolbox: AgentToolbox):
        self.node = node
        self.toolbox = toolbox
        self.name = f"{node.label}LeafAgent"

    def run(self, context: AuditContext) -> LeafLabelHit:
        result = self.toolbox.evaluate_leaf(context, self.node)
        context.audit_events.append(
            {
                "agent": self.name,
                "node_id": self.node.node_id,
                "level": self.node.level,
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
    ):
        self.node = node
        self.children = children
        self.name = f"{node.label.title()}Agent"

    def run(self, context: AuditContext) -> IntermediateAgentResult:
        labels: list[str] = []
        hits: list[LeafLabelHit] = []
        child_results: list[Mapping[str, Any]] = []
        reasons: list[str] = []

        for child in self.children:
            result = child.run(context)
            if isinstance(result, LeafLabelHit):
                child_results.append(result.to_contract())
                if result.is_hit:
                    labels.append(result.label)
                    hits.append(result)
                    if result.reason and result.reason != NO_ISSUE_REASON:
                        reasons.append(result.reason)
            else:
                child_results.append(result.to_contract())
                labels.extend(result.label)
                hits.extend(result.hits)
                if result.reason and result.reason != NO_ISSUE_REASON:
                    reasons.append(result.reason)

        reason = "；".join(reasons) if reasons else NO_ISSUE_REASON
        aggregate = IntermediateAgentResult(
            label=labels,
            reason=reason,
            node_id=self.node.node_id,
            hits=hits,
            child_results=child_results,
        )
        context.audit_events.append(
            {
                "agent": self.name,
                "node_id": self.node.node_id,
                "level": self.node.level,
                "labels": list(labels),
                "reason": reason,
                "child_count": len(self.children),
            }
        )
        return aggregate


class RootAgent:
    def __init__(
        self,
        security_agent: TreeAgent | None,
        ecosystem_agent: TreeAgent | None,
    ):
        self.security_agent = security_agent
        self.ecosystem_agent = ecosystem_agent
        self.name = "RootAgent"

    def run(self, context: AuditContext) -> RootAgentResult:
        security_result = (
            self.security_agent.run(context)
            if self.security_agent is not None
            else _empty_result("SECURITY")
        )
        ecosystem_result = (
            self.ecosystem_agent.run(context)
            if self.ecosystem_agent is not None
            else _empty_result("ECOSYSTEM")
        )

        final_decision, suggested_action = _decide(
            security_result.hits,
            ecosystem_result.hits,
        )
        reason = _root_reason(security_result, ecosystem_result)
        root_event = {
            "agent": self.name,
            "security_labels": list(security_result.label),
            "ecosystem_labels": list(ecosystem_result.label),
            "reason": reason,
            "final_decision": final_decision,
            "suggested_action": suggested_action,
        }
        context.audit_events.append(root_event)

        return RootAgentResult(
            security_labels=list(security_result.label),
            ecosystem_labels=list(ecosystem_result.label),
            reason=reason,
            final_decision=final_decision,
            suggested_action=suggested_action,
            security_result=security_result,
            ecosystem_result=ecosystem_result,
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
        security_agent = (
            _build_agent(forest["SECURITY"], toolbox, force_tree=True)
            if "SECURITY" in forest
            else None
        )
        ecosystem_agent = (
            _build_agent(forest["ECOSYSTEM"], toolbox, force_tree=True)
            if "ECOSYSTEM" in forest
            else None
        )
        return cls(RootAgent(security_agent, ecosystem_agent))

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
            audit_context = AuditContext(**context)
        return self.root_agent.run(audit_context)


def _build_agent(
    node: LabelTreeNode,
    toolbox: AgentToolbox,
    force_tree: bool = False,
) -> TreeAgent | LeafAgent:
    if node.is_leaf and not force_tree:
        return LeafAgent(node, toolbox)
    children = [_build_agent(child, toolbox) for child in node.children]
    return TreeAgent(node, children)


def _empty_result(node_id: str) -> IntermediateAgentResult:
    return IntermediateAgentResult(label=[], reason=NO_ISSUE_REASON, node_id=node_id)


def _root_reason(
    security_result: IntermediateAgentResult,
    ecosystem_result: IntermediateAgentResult,
) -> str:
    reasons = [
        result.reason
        for result in (security_result, ecosystem_result)
        if result.reason and result.reason != NO_ISSUE_REASON
    ]
    return "；".join(reasons) if reasons else NO_ISSUE_REASON


def _decide(
    security_hits: list[LeafLabelHit],
    ecosystem_hits: list[LeafLabelHit],
) -> tuple[str, str]:
    security_actions = {hit.action for hit in security_hits if hit.action}
    ecosystem_actions = {hit.action for hit in ecosystem_hits if hit.action}

    if security_actions.intersection({"ban", "reject"}):
        return "reject", "ban"
    if security_hits:
        return "review", "manual_review"
    if ecosystem_actions.intersection({"limit", "pass_with_limit"}):
        return "pass_with_limit", "limit"
    if ecosystem_hits:
        return "review", "manual_review"
    return "pass", "pass"
