from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from financial_verification.backend.config import ApiSettings
from financial_verification.backend.models import (
    AnnotateToolRequest,
    CapabilitiesResponse,
    JobStatus,
    ReconcileToolRequest,
    StorageMode,
    StorageStatus,
    VerificationAccepted,
    VerificationContextInput,
    VerificationListResponse,
    VerificationStatusResponse,
)
from financial_verification.backend.service import (
    JobConflictError,
    JobExpiredError,
    JobNotFoundError,
    JobRecord,
    VerificationJobManager,
)
from financial_verification.backend.tooling import (
    annotate_fields,
    extract_one_document,
    load_rules,
    reconcile_payload,
)


ALLOWED_EXTENSIONS = {".pdf", ".png", ".docx", ".xlsx", ".txt"}
CHUNK_SIZE = 1024 * 1024


def _safe_file_name(raw: str | None) -> str:
    if not raw:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_FILE_NAME", "message": "Имя файла отсутствует."},
        )
    normalized = unicodedata.normalize("NFC", raw)
    if Path(normalized).name != normalized or normalized in {".", ".."}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSAFE_FILE_NAME",
                "message": "Имя файла не должно содержать путь.",
            },
        )
    if Path(normalized).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_DOCUMENT",
                "message": f"Формат {Path(normalized).suffix or '(без расширения)'} не поддерживается.",
            },
        )
    return normalized


async def _write_upload(
    upload: UploadFile,
    destination: Path,
    *,
    max_file_size: int,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as handle:
            while chunk := await upload.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_file_size:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "FILE_TOO_LARGE",
                            "message": f"Файл {destination.name} превышает допустимый размер.",
                        },
                    )
                digest.update(chunk)
                handle.write(chunk)
    finally:
        await upload.close()
    return {"file_name": destination.name, "size_bytes": size, "sha256": digest.hexdigest()}


def _storage_status(manager: VerificationJobManager, job: JobRecord) -> StorageStatus:
    persistent = manager.settings.is_persistent
    return StorageStatus(
        mode=StorageMode.PERSISTENT if persistent else StorageMode.EPHEMERAL,
        documents_persisted=persistent,
        results_persisted=persistent,
        result_available_until=job.expires_at,
    )


def _status_response(
    manager: VerificationJobManager, job: JobRecord
) -> VerificationStatusResponse:
    return VerificationStatusResponse(
        verification_id=job.verification_id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        message=job.message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error=job.error,
        storage=_storage_status(manager, job),
    )


def _job_or_http(manager: VerificationJobManager, verification_id: str) -> JobRecord:
    try:
        return manager.get(verification_id)
    except JobExpiredError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "VERIFICATION_EXPIRED",
                "message": "Результат удален согласно политике временной обработки.",
            },
        ) from error
    except JobNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "VERIFICATION_NOT_FOUND", "message": "Проверка не найдена."},
        ) from error


def _load_context(settings: ApiSettings, raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
        request_context = VerificationContextInput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CONTEXT", "message": str(error)},
        ) from error
    try:
        base = json.loads(settings.default_context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "CONFIGURATION_UNAVAILABLE", "message": str(error)},
        ) from error
    base.update(
        {
            "case_id": request_context.case_id or "api-verification",
            "report_file": unicodedata.normalize("NFC", request_context.report_file),
            "target_row_label": request_context.target_row_label,
            "contract_number": request_context.contract_number,
            "period_from": request_context.period_from.isoformat(),
            "period_to": request_context.period_to.isoformat(),
        }
    )
    return base


def create_app(
    settings: ApiSettings | None = None,
    manager: VerificationJobManager | None = None,
) -> FastAPI:
    resolved_settings = settings or ApiSettings.from_env()
    resolved_manager = manager or VerificationJobManager(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        application.state.manager.shutdown()

    application = FastAPI(
        title="Financial Verification API",
        version="1.0.0",
        description=(
            "Локальная проверка КФЗ по первичным документам с доказательствами и "
            "двумя режимами хранения."
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.manager = resolved_manager

    @application.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def health_ready() -> dict[str, Any]:
        ready = (
            resolved_settings.default_context_path.exists()
            and resolved_settings.default_rules_path.exists()
        )
        if not ready:
            raise HTTPException(
                status_code=503,
                detail={"code": "CONFIGURATION_UNAVAILABLE"},
            )
        return {"status": "ready", "storage_mode": resolved_manager.storage_mode}

    @application.get(
        "/api/v1/capabilities", response_model=CapabilitiesResponse
    )
    def capabilities() -> CapabilitiesResponse:
        persistent = resolved_settings.is_persistent
        return CapabilitiesResponse(
            api_version="1.0",
            formats=sorted(ALLOWED_EXTENSIONS),
            exports=["json", "markdown"],
            external_services=False,
            storage={
                "mode": resolved_manager.storage_mode,
                "documents_persisted": persistent,
                "results_persisted": persistent,
                "result_ttl_seconds": (
                    None if persistent else resolved_settings.result_ttl_seconds
                ),
                "retention_days": resolved_settings.retention_days if persistent else None,
                "history_available": persistent,
                "rerun_without_upload": persistent,
            },
            limits={
                "max_files": resolved_settings.max_files,
                "max_file_size_bytes": resolved_settings.max_file_size_bytes,
                "max_total_size_bytes": resolved_settings.max_total_size_bytes,
                "pipeline_stage_timeout_seconds": (
                    resolved_settings.pipeline_stage_timeout_seconds
                ),
            },
        )

    @application.post(
        "/api/v1/verifications",
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_verification(
        context: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
    ) -> VerificationAccepted:
        if not files:
            raise HTTPException(
                status_code=422,
                detail={"code": "EMPTY_DOCUMENT_SET", "message": "Добавьте документы."},
            )
        if len(files) > resolved_settings.max_files:
            raise HTTPException(
                status_code=413,
                detail={"code": "TOO_MANY_FILES", "message": "Слишком много файлов."},
            )
        verification_context = _load_context(resolved_settings, context)
        job = resolved_manager.allocate(verification_context)
        assert job.workspace is not None
        input_dir = job.workspace / "input"
        metadata = []
        seen: set[str] = set()
        total_size = 0
        try:
            for upload in files:
                file_name = _safe_file_name(upload.filename)
                if file_name in seen:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "DUPLICATE_FILE_NAME",
                            "message": f"Имя {file_name} встречается несколько раз.",
                        },
                    )
                seen.add(file_name)
                item = await _write_upload(
                    upload,
                    input_dir / file_name,
                    max_file_size=resolved_settings.max_file_size_bytes,
                )
                total_size += item["size_bytes"]
                if total_size > resolved_settings.max_total_size_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "DOCUMENT_SET_TOO_LARGE",
                            "message": "Комплект превышает допустимый общий размер.",
                        },
                    )
                metadata.append(item)
            if verification_context["report_file"] not in seen:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "REPORT_FILE_MISSING",
                        "message": "Настроенный итоговый отчет отсутствует в комплекте.",
                    },
                )
            resolved_manager.record_inputs(job.verification_id, metadata)
            resolved_manager.submit(job.verification_id)
        except Exception:
            resolved_manager.abandon(job.verification_id)
            raise
        return VerificationAccepted(
            verification_id=job.verification_id,
            status=JobStatus.QUEUED,
            storage=_storage_status(resolved_manager, job),
        )

    @application.get(
        "/api/v1/verifications", response_model=VerificationListResponse
    )
    def list_verifications() -> VerificationListResponse:
        return VerificationListResponse(
            items=[_status_response(resolved_manager, job) for job in resolved_manager.list()]
        )

    @application.get(
        "/api/v1/verifications/{verification_id}",
        response_model=VerificationStatusResponse,
    )
    def get_verification(verification_id: str) -> VerificationStatusResponse:
        return _status_response(
            resolved_manager, _job_or_http(resolved_manager, verification_id)
        )

    @application.get("/api/v1/verifications/{verification_id}/events")
    async def verification_events(
        verification_id: str, request: Request
    ) -> StreamingResponse:
        _job_or_http(resolved_manager, verification_id)

        async def stream():
            cursor = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    events, terminal = resolved_manager.events(verification_id, cursor)
                except (JobNotFoundError, JobExpiredError):
                    break
                for item in events:
                    cursor += 1
                    yield (
                        f"event: {item['event']}\n"
                        f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                    )
                if terminal and not events:
                    break
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get("/api/v1/verifications/{verification_id}/result")
    def get_result(verification_id: str) -> JSONResponse:
        _job_or_http(resolved_manager, verification_id)
        try:
            result, _ = resolved_manager.result(verification_id)
        except JobConflictError as error:
            job = resolved_manager.get(verification_id)
            code = "VERIFICATION_FAILED" if job.status == JobStatus.FAILED else "RESULT_NOT_READY"
            raise HTTPException(
                status_code=409,
                detail={"code": code, "message": str(error), "error": job.error},
            ) from error
        return JSONResponse(result)

    @application.get("/api/v1/verifications/{verification_id}/documents")
    def get_documents(verification_id: str) -> dict[str, Any]:
        job = _job_or_http(resolved_manager, verification_id)
        if job.result:
            return {"items": job.result["documents"]}
        return {"items": job.input_files}

    @application.get(
        "/api/v1/verifications/{verification_id}/documents/{document_id}/content"
    )
    def get_document_content(verification_id: str, document_id: str):
        job = _job_or_http(resolved_manager, verification_id)
        if not resolved_settings.is_persistent:
            raise HTTPException(
                status_code=410,
                detail={
                    "code": "DOCUMENT_CONTENT_EXPIRED",
                    "message": "Документ не хранится в EPHEMERAL-режиме.",
                    "can_restore_by_reupload": True,
                },
            )
        if not job.result or job.workspace is None:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"})
        document = next(
            (item for item in job.result["documents"] if item["document_id"] == document_id),
            None,
        )
        if document is None:
            raise HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND"})
        input_dir = (job.workspace / "input").resolve()
        path = (input_dir / document["file_name"]).resolve()
        if path.parent != input_dir:
            raise HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND"})
        if not path.exists():
            raise HTTPException(status_code=410, detail={"code": "DOCUMENT_CONTENT_EXPIRED"})
        return FileResponse(path, filename=document["file_name"])

    @application.get(
        "/api/v1/verifications/{verification_id}/evidence/{evidence_id:path}"
    )
    def get_evidence(verification_id: str, evidence_id: str) -> dict[str, Any]:
        job = _job_or_http(resolved_manager, verification_id)
        if not job.result:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"})
        item = next(
            (value for value in job.result["evidence"] if value["evidence_id"] == evidence_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "EVIDENCE_NOT_FOUND"})
        return item

    @application.get("/api/v1/verifications/{verification_id}/export")
    def export_result(verification_id: str, format: str = "json"):
        _job_or_http(resolved_manager, verification_id)
        try:
            result, markdown = resolved_manager.result(verification_id)
        except JobConflictError as error:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"}) from error
        normalized = format.lower()
        if normalized == "json":
            return JSONResponse(result)
        if normalized in {"md", "markdown"}:
            return PlainTextResponse(
                markdown,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="verification-{verification_id}.md"'
                    )
                },
            )
        raise HTTPException(
            status_code=422,
            detail={"code": "UNSUPPORTED_EXPORT_FORMAT", "supported": ["json", "markdown"]},
        )

    @application.post(
        "/api/v1/verifications/{verification_id}/rerun",
        response_model=VerificationAccepted,
        status_code=202,
    )
    def rerun_verification(verification_id: str) -> VerificationAccepted:
        _job_or_http(resolved_manager, verification_id)
        try:
            job = resolved_manager.rerun(verification_id)
        except JobConflictError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SOURCE_DOCUMENTS_NOT_AVAILABLE",
                    "message": "Для повторной проверки загрузите документы заново.",
                },
            ) from error
        return VerificationAccepted(
            verification_id=job.verification_id,
            status=job.status,
            storage=_storage_status(resolved_manager, job),
        )

    @application.delete(
        "/api/v1/verifications/{verification_id}", status_code=204
    )
    def delete_verification(verification_id: str):
        _job_or_http(resolved_manager, verification_id)
        try:
            resolved_manager.delete(verification_id)
        except JobConflictError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERIFICATION_RUNNING", "message": str(error)},
            ) from error
        return None

    @application.post("/api/v1/tools/extract")
    async def tool_extract(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        file_name = _safe_file_name(file.filename)
        with tempfile.TemporaryDirectory(prefix="financial-extract-") as directory:
            root = Path(directory)
            await _write_upload(
                file,
                root / file_name,
                max_file_size=resolved_settings.max_file_size_bytes,
            )
            try:
                return extract_one_document(root / file_name, root)
            except Exception as error:  # noqa: BLE001 - stateless tool boundary
                raise HTTPException(
                    status_code=422,
                    detail={"code": "EXTRACTION_FAILED", "message": str(error)},
                ) from error

    @application.post("/api/v1/tools/annotate")
    def tool_annotate(payload: AnnotateToolRequest) -> dict[str, Any]:
        return {
            "annotations": annotate_fields(
                [item.model_dump() for item in payload.fields],
                payload.confidence_threshold,
            )
        }

    @application.post("/api/v1/tools/reconcile")
    def tool_reconcile(payload: ReconcileToolRequest) -> dict[str, Any]:
        try:
            rules = payload.rules or load_rules(resolved_settings.default_rules_path)
            return reconcile_payload(
                operations=payload.operations,
                report_fields=payload.report_fields,
                context=payload.context,
                rules=rules,
            )
        except Exception as error:  # noqa: BLE001 - stateless tool boundary
            raise HTTPException(
                status_code=422,
                detail={"code": "RECONCILIATION_FAILED", "message": str(error)},
            ) from error

    return application


app = create_app()
