from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..rule_tree import normalize_label
from ..schema import (
    AuditContext,
    LabelTreeNode,
    LeafLabelHit,
    NO_ISSUE_REASON,
    ToolResponse,
)


class LocalRuleToolbox:
    """Deterministic fallback toolbox used before external services exist."""

    def evaluate_leaf(self, context: AuditContext, node: LabelTreeNode) -> LeafLabelHit:
        explicit = self._explicit_hit(context, node)
        if explicit is not None:
            return explicit

        keyword = self._first_keyword_match(context, node.keywords)
        if keyword:
            return LeafLabelHit(
                label=node.label,
                reason=f"命中 {node.label} 关键词: {keyword}",
                node_id=node.node_id,
                path=node.path,
                evidence=({"type": "keyword", "content": keyword},),
                action=node.default_action,
                action_priority=node.action_priority,
            )

        return LeafLabelHit(
            label="",
            reason=NO_ISSUE_REASON,
            node_id=node.node_id,
            path=node.path,
            action=node.default_action,
            action_priority=node.action_priority,
        )

    def model_route(self, context: AuditContext) -> ToolResponse:
        selected = context.candidate_models[0] if context.candidate_models else ""
        fallback = context.candidate_models[1] if len(context.candidate_models) > 1 else ""
        return ToolResponse(
            status="success",
            data={
                "modality": context.modality,
                "selected_model": selected,
                "reason": "local fallback route",
                "fallback_model": fallback,
            },
            trace_id=context.trace_id,
        )

    def run_model_inference(
        self, context: AuditContext, route_decision: ToolResponse
    ) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={
                "model_name": route_decision.data.get("selected_model", ""),
                "modality": context.modality,
                "labels": [],
                "evidence": [],
                "status": "success",
            },
            trace_id=context.trace_id,
        )

    def query_policy_rule(
        self, context: AuditContext, node: LabelTreeNode
    ) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={
                "rules": [
                    {
                        "rule_id": rule_id,
                        "label": node.label,
                        "enabled": True,
                    }
                    for rule_id in node.rule_ids
                ]
            },
            trace_id=context.trace_id,
        )

    def evaluate_rule(
        self, context: AuditContext, node: LabelTreeNode
    ) -> ToolResponse:
        hit = self.evaluate_leaf(context, node)
        return ToolResponse(
            status="success",
            data={
                "label": node.label,
                "hit": hit.is_hit,
                "reason": hit.reason,
                "action": hit.action,
                "evidence": list(hit.evidence),
            },
            trace_id=context.trace_id,
        )

    def graph_rag_search(self, context: AuditContext, labels: list[str]) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={"hits": [], "paths": [], "evidence_summary": ""},
            trace_id=context.trace_id,
        )

    def audit_trace_lookup(self, trace_id: str) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={"trace_id": trace_id, "audit_trace": []},
            trace_id=trace_id,
        )

    def threshold_preview(
        self, context: AuditContext, threshold_overrides: dict[str, float]
    ) -> ToolResponse:
        return ToolResponse(
            status="success",
            data={
                "decision": "pass",
                "risk_score": 0,
                "labels": [],
                "rule_results": [],
                "suggested_action": "pass",
                "preview_only": True,
                "threshold_overrides": dict(threshold_overrides),
            },
            trace_id=context.trace_id,
        )

    def _explicit_hit(
        self, context: AuditContext, node: LabelTreeNode
    ) -> LeafLabelHit | None:
        for record in _iter_hit_records(context):
            record_label = normalize_label(record.get("label", ""))
            if record_label != node.label:
                continue
            if not _record_is_hit(record):
                continue
            return LeafLabelHit(
                label=node.label,
                reason=str(record.get("reason") or f"命中 {node.label} 规则"),
                needs_review=bool(record.get("needs_review", False)),
                node_id=node.node_id,
                path=node.path,
                evidence=tuple(_as_mappings(record.get("evidence", ()))),
                action=str(record.get("action", node.default_action)).lower(),
                action_priority=_as_int(record.get("action_priority", node.action_priority)),
            )

        explicit_labels = _explicit_label_set(context)
        if node.label in explicit_labels or node.node_id in explicit_labels:
            return LeafLabelHit(
                label=node.label,
                reason=f"命中 {node.label} 显式标签",
                node_id=node.node_id,
                path=node.path,
                action=node.default_action,
                action_priority=node.action_priority,
            )
        return None

    def _first_keyword_match(
        self, context: AuditContext, keywords: tuple[str, ...]
    ) -> str:
        text = _content_text(context.content).lower()
        for keyword in keywords:
            if keyword and keyword.lower() in text:
                return keyword
        return ""


def _iter_hit_records(context: AuditContext) -> Iterable[Mapping[str, Any]]:
    for source in _sources(context):
        for key in ("label_hits", "rule_results"):
            value = source.get(key)
            if isinstance(value, Mapping):
                for label, detail in value.items():
                    if isinstance(detail, Mapping):
                        yield {"_source": key, "label": label, **detail}
                    elif detail:
                        yield {"_source": key, "label": label, "hit": True}
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        yield {"_source": key, **item}


def _explicit_label_set(context: AuditContext) -> set[str]:
    labels: set[str] = set()
    for source in _sources(context):
        for key in ("matched_labels", "hit_labels"):
            value = source.get(key)
            if isinstance(value, Mapping):
                labels.update(normalize_label(label) for label, hit in value.items() if hit)
            elif isinstance(value, list):
                labels.update(normalize_label(label) for label in value)
    return labels


def _sources(context: AuditContext) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    if isinstance(context.metadata, Mapping):
        sources.append(context.metadata)
    if isinstance(context.content, Mapping):
        sources.append(context.content)
    return sources


def _record_is_hit(record: Mapping[str, Any]) -> bool:
    for key in ("hit", "matched", "is_hit"):
        if key in record:
            return bool(record[key])
    result = str(record.get("result", record.get("status", ""))).lower()
    if result in {"hit", "matched", "violation", "reject", "review", "limit"}:
        return True
    action = str(record.get("action", "")).lower()
    if action in {"ban", "reject", "review", "limit", "pass_with_limit"}:
        return True
    if "score" in record and "threshold" in record:
        try:
            return float(record["score"]) >= float(record["threshold"])
        except (TypeError, ValueError):
            return False
    return record.get("_source") == "label_hits"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        parts: list[str] = []
        for value in content.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, Mapping):
                parts.append(_content_text(value))
            elif isinstance(value, list):
                parts.extend(_content_text(item) for item in value)
        return " ".join(part for part in parts if part)
    if isinstance(content, list):
        return " ".join(_content_text(item) for item in content)
    return ""


def _as_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
