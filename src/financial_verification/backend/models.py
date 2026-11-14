from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StorageMode(str, Enum):
    EPHEMERAL = "EPHEMERAL"
    PERSISTENT = "PERSISTENT"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DELETED = "DELETED"


TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.DELETED}


class VerificationContextInput(BaseModel):
    # The API accepts the full verification context used by the CLI and reads the
    # five request-level fields it is allowed to override.
    model_config = ConfigDict(extra="ignore")

    report_file: str
    target_row_label: str = "3 этап, 2026 г."
    contract_number: str
    period_from: date
    period_to: date
    case_id: str | None = None

    @field_validator("report_file")
    @classmethod
    def report_file_must_be_a_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("report_file must not be empty")
        return value.strip()

    @field_validator("period_to")
    @classmethod
    def period_must_be_ordered(
        cls, value: date, info: Any
    ) -> date:
        start = info.data.get("period_from")
        if start and value < start:
            raise ValueError("period_to must not precede period_from")
        return value


class StorageStatus(BaseModel):
    mode: StorageMode
    documents_persisted: bool
    results_persisted: bool
    result_available_until: datetime | None = None


class VerificationAccepted(BaseModel):
    verification_id: str
    status: JobStatus
    storage: StorageStatus


class VerificationStatusResponse(BaseModel):
    verification_id: str
    status: JobStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    message: str
    created_at: datetime
    updated_at: datetime
    error: dict[str, Any] | None = None
    storage: StorageStatus


class VerificationListResponse(BaseModel):
    items: list[VerificationStatusResponse]


class CapabilitiesResponse(BaseModel):
    api_version: str
    formats: list[str]
    exports: list[str]
    external_services: bool
    storage: dict[str, Any]
    limits: dict[str, int]


class ToolTextField(BaseModel):
    field_id: str
    text: str
    document_id: str = "inline"
    field_type: Literal["money", "contract", "date", "text"] = "text"
    expected_value: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class AnnotateToolRequest(BaseModel):
    fields: list[ToolTextField]
    confidence_threshold: float = Field(default=0.80, ge=0, le=1)


class ReconcileToolRequest(BaseModel):
    operations: list[dict[str, Any]]
    report_fields: dict[str, Any]
    context: dict[str, Any]
    rules: dict[str, Any] | None = None

