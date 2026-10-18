"""Pydantic request models and typed shapes used across the service."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

SourceKind = Literal["web_form", "manual", "csv_fixture"]
Outcome = Literal["SUPPORTED_DRAFT", "REVIEW_REQUIRED", "INFERENCE_FAILED"]
Decision = Literal["PENDING", "APPROVED", "REJECTED", "REQUEST_INFO"]
NewDecision = Literal["APPROVED", "REJECTED", "REQUEST_INFO"]


class IntakeRequest(BaseModel):
    """Payload accepted by POST /api/cases."""

    complaint_text: str = Field(min_length=1, max_length=8000)
    policy_version: str = Field(default="synthetic-v0.1", max_length=64)
    source: SourceKind = "web_form"
    order_csv: Optional[str] = Field(
        default=None,
        max_length=100_000,
        description="Optional anonymized order CSV text (header row required).",
    )


class DecisionRequest(BaseModel):
    """Payload accepted by POST /api/cases/{case_id}/decision."""

    decision: NewDecision
    note: Optional[str] = Field(default=None, max_length=1000)


class BatchIntakeRequest(BaseModel):
    """Payload accepted by POST /api/cases/batch — many complaints, one policy.

    One restaurant CSV can be shared across the whole batch; unknown policy or
    malformed CSV fails the entire batch closed (identical inputs for every
    case, so a partial run would be misleading).
    """

    complaints: list[Annotated[str, Field(min_length=1, max_length=8000)]] = Field(
        min_length=1,
        max_length=25,
        description="1-25 complaint texts; each becomes its own case.",
    )
    policy_version: str = Field(default="synthetic-v0.1", max_length=64)
    source: SourceKind = "web_form"
    order_csv: Optional[str] = Field(
        default=None,
        max_length=100_000,
        description="Optional anonymized order CSV shared by every case in the batch.",
    )


class AmdInference(BaseModel):
    """Provenance block embedded in every packet (schema: review_packet)."""

    attempted: bool
    verified: bool
    model: Optional[str] = None
    request_ref: Optional[str] = None
