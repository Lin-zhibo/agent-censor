from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Mapping

from .logging import log_agent_event
from .rule_tree import build_rule_forest, load_settings_file
from .schema import (
    AuditContext,
    IntermediateAgentResult,
    LabelTreeNode,
    LeafLabelHit,
    NO_ISSUE_REASON,
    NO_ROUTE_REASON,
    RootAgentResult,
    ToolResponse,
)
from .tool import AgentToolbox


class LeafAgent:
    def __init__(self, node: LabelTreeNode, toolbox: AgentToolbox):
        self.node = node
        self.toolbox = toolbox
        self.name = f"{node.label}LeafAgent"

    def run(self, context: AuditContext) -> LeafLabelHit:
        started_at = perf_counter()
        log_agent_event(
            "agent.leaf.started",
            **_context_log_fields(context),
            **_node_log_fields(self.node),
            agent=self.name,
        )
        try:
            route_decision = self.toolbox.model_route(context)
            _log_tool_response(context, self.name, "model_route", route_decision, self.node)
            model_result = self.toolbox.run_model_inference(context, route_decision)
            _log_tool_response(
                context, self.name, "run_model_inference", model_result, self.node
            )
            policy_rules = self.toolbox.query_policy_rule(context, self.node)
            _log_tool_response(
                context, self.name, "query_policy_rule", policy_rules, self.node
            )
            rule_result = self.toolbox.evaluate_rule(context, self.node)
            _log_tool_response(context, self.name, "evaluate_rule", rule_result, self.node)
            rag_result = self.toolbox.graph_rag_search(context, [self.node.label])
            _log_tool_response(context, self.name, "graph_rag_search", rag_result, self.node)
            result = self.toolbox.evaluate_leaf(context, self.node)
            _log_leaf_evaluation(context, self.name, result, self.node)
        except Exception as exc:
            log_agent_event(
                "agent.leaf.failed",
                logging.ERROR,
                **_context_log_fields(context),
                **_node_log_fields(self.node),
                agent=self.name,
                duration_ms=_elapsed_ms(started_at),
                exception_type=type(exc).__name__,
            )
            raise
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
        log_agent_event(
            "agent.leaf.completed",
            **_context_log_fields(context),
            **_node_log_fields(self.node),
            agent=self.name,
            duration_ms=_elapsed_ms(started_at),
            hit=result.is_hit,
            hit_label=result.label,
            needs_review=result.needs_review,
            action=result.action,
            evidence_count=len(result.evidence),
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
        started_at = perf_counter()
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
        decision = _decide(leaf_results)
        reason = _root_reason(leaf_results)

        context.audit_events.append(
            {
                "agent": self.name,
                "node_id": "ROOT",
                "child_labels": list(root_result.child_labels),
                "security_labels": list(security_labels),
                "ecosystem_labels": list(ecosystem_labels),
                "reason": reason,
                "decision": decision,
            }
        )
        log_agent_event(
            "agent.root.completed",
            **_context_log_fields(context),
            agent=self.name,
            node_id="ROOT",
            duration_ms=_elapsed_ms(started_at),
            decision=decision,
            security_labels=security_labels,
            ecosystem_labels=ecosystem_labels,
            selected_child_labels=list(root_result.child_labels),
            hit_count=len(leaf_results),
        )

        return RootAgentResult(
            security_labels=security_labels,
            ecosystem_labels=ecosystem_labels,
            reason=reason,
            decision=decision,
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
        if toolbox is None:
            raise ValueError(
                "MultiAgentModerator requires an explicit AgentToolbox; "
                "no local text-matching toolbox is provided."
            )
        forest = build_rule_forest(settings)

        root_children: list[TreeAgent | LeafAgent] = []
        for domain_label in ("SECURITY", "ECOSYSTEM"):
            domain_root = forest.get(domain_label)
            if domain_root is None:
                continue
            root_children.extend(_build_agent(child, toolbox) for child in domain_root.children)

        log_agent_event(
            "agent.graph.built",
            root_child_count=len(root_children),
            root_child_labels=[child.node.label for child in root_children],
        )
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
        started_at = perf_counter()
        log_agent_event(
            "audit.started",
            **_context_log_fields(audit_context),
            root_child_count=len(self.root_agent.children),
        )
        try:
            result = self.root_agent.run(audit_context)
        except Exception as exc:
            log_agent_event(
                "audit.failed",
                logging.ERROR,
                **_context_log_fields(audit_context),
                duration_ms=_elapsed_ms(started_at),
                exception_type=type(exc).__name__,
            )
            raise
        log_agent_event(
            "audit.completed",
            **_context_log_fields(audit_context),
            duration_ms=_elapsed_ms(started_at),
            decision=result.decision,
            security_labels=list(result.security_labels),
            ecosystem_labels=list(result.ecosystem_labels),
            audit_event_count=len(result.audit_trace),
        )
        return result


def _context_log_fields(context: AuditContext) -> dict[str, Any]:
    return {
        "trace_id": context.trace_id,
        "tenant_id": context.tenant_id,
        "business_id": context.business_id,
        "policy_id": context.policy_id,
        "policy_version": context.policy_version,
        "modality": context.modality,
        "detail_level": context.detail_level,
        "text_length": len(context.text),
        "labels_requested": list(context.labels_requested),
        "labels_requested_count": len(context.labels_requested),
        "candidate_models_count": len(context.candidate_models),
    }


def _node_log_fields(node: LabelTreeNode | None) -> dict[str, Any]:
    if node is None:
        return {
            "node_id": "ROOT",
            "node_label": "ROOT",
            "node_level": -1,
            "domain": "",
            "path": [],
            "is_leaf": False,
        }
    return {
        "node_id": node.node_id,
        "node_label": node.label,
        "node_level": node.level,
        "domain": node.domain,
        "path": list(node.path),
        "is_leaf": node.is_leaf,
    }


def _log_tool_response(
    context: AuditContext,
    agent_name: str,
    tool_name: str,
    response: ToolResponse,
    node: LabelTreeNode | None = None,
) -> None:
    log_agent_event(
        "tool.call.completed",
        **_context_log_fields(context),
        **_node_log_fields(node),
        agent=agent_name,
        tool_name=tool_name,
        status=response.status,
        ok=response.ok,
        tool_latency_ms=response.latency_ms,
        error_count=len(response.errors),
        error_codes=[error.code for error in response.errors],
        response_trace_id=response.trace_id,
    )


def _log_leaf_evaluation(
    context: AuditContext,
    agent_name: str,
    result: LeafLabelHit,
    node: LabelTreeNode,
) -> None:
    log_agent_event(
        "tool.call.completed",
        **_context_log_fields(context),
        **_node_log_fields(node),
        agent=agent_name,
        tool_name="evaluate_leaf",
        status="success",
        ok=True,
        hit=result.is_hit,
        hit_label=result.label,
        needs_review=result.needs_review,
        action=result.action,
        evidence_count=len(result.evidence),
    )


def _elapsed_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


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
    started_at = perf_counter()
    node_id = node.node_id if node is not None else "ROOT"
    child_nodes = [child.node for child in children]
    log_agent_event(
        "agent.route.started",
        **_context_log_fields(context),
        **_node_log_fields(node),
        agent=agent_name,
        child_count=len(children),
        visible_child_labels=[child.node.label for child in children],
    )
    try:
        route_response = toolbox.route_children(context, node, child_nodes)
        child_labels = _normalize_child_labels(route_response.data.get("child_labels"))
        selected_children = _select_children(children, child_labels)
        _log_tool_response(context, agent_name, "route_children", route_response, node)

        child_results: list[Mapping[str, Any]] = []
        leaf_results: list[LeafLabelHit] = []
        for child in selected_children:
            log_agent_event(
                "agent.child.dispatched",
                **_context_log_fields(context),
                **_node_log_fields(node),
                agent=agent_name,
                child_agent=child.name,
                child_node_id=child.node.node_id,
                child_label=child.node.label,
                child_level=child.node.level,
                child_is_leaf=child.node.is_leaf,
            )
            result = child.run(context)
            child_results.append(result.to_contract())
            if isinstance(result, LeafLabelHit):
                if result.is_hit or result.needs_review:
                    leaf_results.append(result)
            else:
                leaf_results.extend(result.hits)

        reason = str(route_response.data.get("reason") or NO_ROUTE_REASON)
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
                "raw_child_labels": _normalize_child_labels(
                    route_response.data.get("raw_child_labels", child_labels)
                ),
                "rejected_child_labels": _normalize_child_labels(
                    route_response.data.get("rejected_child_labels", [])
                ),
                "valid_child_labels": _normalize_child_labels(
                    route_response.data.get(
                        "valid_child_labels", [child.node.label for child in children]
                    )
                ),
                "child_count": len(children),
                "selected_count": len(selected_children),
            }
        )
    except Exception as exc:
        log_agent_event(
            "agent.route.failed",
            logging.ERROR,
            **_context_log_fields(context),
            **_node_log_fields(node),
            agent=agent_name,
            duration_ms=_elapsed_ms(started_at),
            exception_type=type(exc).__name__,
        )
        raise
    log_agent_event(
        "agent.route.completed",
        **_context_log_fields(context),
        **_node_log_fields(node),
        agent=agent_name,
        duration_ms=_elapsed_ms(started_at),
        selected_child_labels=list(child_labels),
        selected_count=len(selected_children),
        child_count=len(children),
        hit_count=len(leaf_results),
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


def _decide(leaf_results: list[LeafLabelHit]) -> str:
    """统一口径：security → ban，ecosystem → limit，ban > limit > pass"""
    security_hit = any(hit.is_hit for hit in leaf_results if hit.domain == "security")
    ecosystem_hit = any(hit.is_hit for hit in leaf_results if hit.domain == "ecosystem")
    if security_hit:
        return "ban"
    if ecosystem_hit:
        return "limit"
    return "pass"
