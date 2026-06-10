from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


NO_ISSUE_REASON = "未检出问题"
NO_ROUTE_REASON = "当前层未发现需要继续下钻的标签路径"


@dataclass
class AuditContext:
    trace_id: str
    content: Any = field(default_factory=dict)
    detail_level: str = "detailed"
    tenant_id: str = ""
    business_id: str = ""
    policy_id: str = ""
    policy_version: str = ""
    modality: str = "text"
    content_features: Mapping[str, Any] = field(default_factory=dict)
    candidate_models: list[str] = field(default_factory=list)
    labels_requested: list[str] = field(default_factory=list)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, Mapping):
            value = self.content.get("text")
            if isinstance(value, str):
                return value
        return ""


@dataclass(frozen=True)
class LabelTreeNode:
    node_id: str
    level: int
    label: str
    domain: str = ""
    display_name: str = ""
    description: str = ""
    parent_id: str = ""
    path: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    rule_ids: tuple[str, ...] = field(default_factory=tuple)
    default_action: str = ""
    action_priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    children: tuple["LabelTreeNode", ...] = field(default_factory=tuple)

    @property
    def is_leaf(self) -> bool:
        return not self.children


@dataclass(frozen=True)
class LeafLabelHit:
    label: str
    reason: str = NO_ISSUE_REASON
    needs_review: bool = False
    node_id: str = ""
    domain: str = ""
    path: tuple[str, ...] = field(default_factory=tuple)
    evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    action: str = ""
    action_priority: int = 0

    @property
    def is_hit(self) -> bool:
        return bool(self.label)

    def to_contract(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "reason": self.reason,
            "needs_review": self.needs_review,
            "evidence": [dict(item) for item in self.evidence],
        }


@dataclass(frozen=True)
class IntermediateAgentResult:
    child_labels: list[str]
    reason: str
    node_id: str = ""
    hits: list[LeafLabelHit] = field(default_factory=list)
    child_results: list[Mapping[str, Any]] = field(default_factory=list)

    def to_contract(self) -> dict[str, Any]:
        return {"child_labels": list(self.child_labels), "reason": self.reason}


@dataclass(frozen=True)
class RootAgentResult:
    security_labels: list[str]
    ecosystem_labels: list[str]
    reason: str
    final_decision: str
    suggested_action: str
    root_result: IntermediateAgentResult
    audit_trace: list[Mapping[str, Any]] = field(default_factory=list)

    def to_contract(self) -> dict[str, Any]:
        return {
            "security_labels": list(self.security_labels),
            "ecosystem_labels": list(self.ecosystem_labels),
            "reason": self.reason,
            "final_decision": self.final_decision,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class ToolResponse:
    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[ToolError, ...] = field(default_factory=tuple)
    latency_ms: int = 0
    trace_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": dict(self.data),
            "errors": [error.to_dict() for error in self.errors],
            "latency_ms": self.latency_ms,
            "trace_id": self.trace_id,
        }
