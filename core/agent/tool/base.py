from __future__ import annotations

from typing import Protocol

from ..schema import AuditContext, LabelTreeNode, LeafLabelHit, ToolResponse


class AgentToolbox(Protocol):
    def evaluate_leaf(self, context: AuditContext, node: LabelTreeNode) -> LeafLabelHit:
        """Return the leaf label hit contract for one leaf node."""

    def model_route(self, context: AuditContext) -> ToolResponse:
        """Optional model routing hook for future internal API integration."""

    def run_model_inference(
        self, context: AuditContext, route_decision: ToolResponse
    ) -> ToolResponse:
        """Optional model inference hook for future internal API integration."""

    def query_policy_rule(
        self, context: AuditContext, node: LabelTreeNode
    ) -> ToolResponse:
        """Optional policy rule query hook."""

    def evaluate_rule(
        self, context: AuditContext, node: LabelTreeNode
    ) -> ToolResponse:
        """Optional rule engine evaluation hook."""

    def graph_rag_search(self, context: AuditContext, labels: list[str]) -> ToolResponse:
        """Optional Graph RAG evidence lookup hook."""

    def audit_trace_lookup(self, trace_id: str) -> ToolResponse:
        """Optional historical audit trace lookup hook."""

    def threshold_preview(
        self, context: AuditContext, threshold_overrides: dict[str, float]
    ) -> ToolResponse:
        """Optional policy threshold preview hook."""
