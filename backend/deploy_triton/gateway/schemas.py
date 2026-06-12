"""
Pydantic schemas for agent-censor Model Service API.
Follows the ModelInferenceRequest / ModelResult / ToolResponse protocol.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─── Request Schemas ───

class RouteDecision(BaseModel):
    modality: str
    selected_model: str
    reason: str
    fallback_model: Optional[str] = None


class Content(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelInferenceRequest(BaseModel):
    trace_id: str
    task_id: str
    route_decision: RouteDecision
    content: Content
    labels_requested: List[str] = Field(default_factory=list)
    detail_level: str = "detailed"
    timeout_ms: int = 3000


# ─── Response Schemas ───

class LabelResult(BaseModel):
    label: str
    sub_label: str = ""
    score: float
    normalized_score: float


class Evidence(BaseModel):
    evidence_id: str
    type: str
    content: str


class ModelResult(BaseModel):
    model_name: str
    model_version: str
    modality: str
    labels: List[LabelResult]
    evidence: List[Evidence]
    latency_ms: int
    status: str
    error: Optional[str] = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool


class ToolResponse(BaseModel):
    status: str  # "success" | "error"
    data: Optional[ModelResult] = None
    errors: List[ErrorDetail] = Field(default_factory=list)
    latency_ms: int
    trace_id: str


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_name: str
    model_version: str
