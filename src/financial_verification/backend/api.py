from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import secrets
import tempfile
import unicodedata
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from financial_verification.backend.config import ApiSettings
from financial_verification.backend.models import (
    AnnotateToolRequest,
    CapabilitiesResponse,
    JobStatus,
    PossibleDuplicateDecisionInput,
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
from financial_verification.backend.page_artifacts import resolve_page_path
from financial_verification.backend.spreadsheet_preview import spreadsheet_preview
from financial_verification.backend.tooling import (
    annotate_fields,
    extract_one_document,
    load_rules,
    reconcile_payload,
)
from financial_verification.document_registry import extract_docx


ALLOWED_EXTENSIONS = {".pdf", ".png", ".docx", ".xlsx", ".txt"}
CHUNK_SIZE = 1024 * 1024
WEB_CLIENT_PATH = Path(__file__).with_name("web") / "index.html"
SPREADSHEET_PREVIEW_REQUEST_LIMIT = 20
SPREADSHEET_PREVIEW_RATE_WINDOW_SECONDS = 60.0


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


def _custom_rules(raw: str | None, settings: ApiSettings) -> dict[str, Any] | None:
    """Merge small, user-authored article rules into the reviewed base rules."""
    if not raw or not raw.strip():
        return None
    try:
        additions = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Настройки статей должны быть JSON-массивом."}) from error
    if not isinstance(additions, list) or not additions or len(additions) > 20:
        raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Можно добавить от 1 до 20 статей."})
    rules = load_rules(settings.default_rules_path)
    for article in additions:
        if not isinstance(article, dict):
            raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Каждая статья должна быть объектом."})
        code, name, signals = article.get("code"), article.get("name"), article.get("signals")
        if not isinstance(code, str) or not re.fullmatch(r"\d{4}", code):
            raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Код статьи состоит из четырёх цифр."})
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Укажите название статьи до 160 символов."})
        if not isinstance(signals, list) or not signals or len(signals) > 20:
            raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "У статьи должен быть 1–20 признаков."})
        normalized = []
        for index, signal in enumerate(signals, 1):
            if not isinstance(signal, str) or not signal.strip() or len(signal) > 300:
                raise HTTPException(422, detail={"code": "INVALID_ARTICLE_RULES", "message": "Признак должен быть непустой строкой до 300 символов."})
            normalized.append({"id": f"custom_{code}_{index}", "pattern": re.escape(signal.strip()), "weight": 3})
        rules["articles"][code] = {"name": name.strip(), "signals": normalized}
    rules["version"] = f"{rules['version']}-session-custom"
    return rules


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


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_session_hash(request: Request, settings: ApiSettings) -> str | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token or len(token) > 256:
        return None
    return _session_hash(token)


def _get_or_create_session_hash(
    request: Request, response: Response, settings: ApiSettings
) -> str:
    existing = _request_session_hash(request, settings)
    if existing is not None:
        return existing
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return _session_hash(token)


def _verification_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "VERIFICATION_NOT_FOUND", "message": "Проверка не найдена."},
    )


def _job_or_http(
    manager: VerificationJobManager,
    verification_id: str,
    owner_session_hash: str | None,
) -> JobRecord:
    try:
        job = manager.get(verification_id)
    except JobExpiredError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "VERIFICATION_EXPIRED",
                "message": "Результат удален согласно политике временной обработки.",
            },
        ) from error
    except JobNotFoundError as error:
        raise _verification_not_found() from error
    if (
        owner_session_hash is None
        or not job.owner_session_hash
        or not hmac.compare_digest(job.owner_session_hash, owner_session_hash)
    ):
        raise _verification_not_found()
    return job


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

    @application.get("/", include_in_schema=False, response_model=None)
    def web_client() -> Response:
        if not WEB_CLIENT_PATH.exists():
            return HTMLResponse("Web client is not installed", status_code=503)
        return FileResponse(WEB_CLIENT_PATH, media_type="text/html; charset=utf-8")

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
        request: Request,
        response: Response,
        context: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
        article_rules: Annotated[str | None, Form()] = None,
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
        rules = _custom_rules(article_rules, resolved_settings)
        owner_session_hash = _get_or_create_session_hash(
            request, response, resolved_settings
        )
        job = resolved_manager.allocate(verification_context, owner_session_hash)
        assert job.workspace is not None
        if rules is not None:
            job.rules_path = job.workspace / "rules.json"
            job.rules_path.write_text(
                json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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
    def list_verifications(request: Request) -> VerificationListResponse:
        owner_session_hash = _request_session_hash(request, resolved_settings)
        if owner_session_hash is None:
            return VerificationListResponse(items=[])
        return VerificationListResponse(
            items=[
                _status_response(resolved_manager, job)
                for job in resolved_manager.list()
                if job.owner_session_hash
                and hmac.compare_digest(job.owner_session_hash, owner_session_hash)
            ]
        )

    @application.get(
        "/api/v1/verifications/{verification_id}",
        response_model=VerificationStatusResponse,
    )
    def get_verification(
        verification_id: str, request: Request
    ) -> VerificationStatusResponse:
        return _status_response(
            resolved_manager,
            _job_or_http(
                resolved_manager,
                verification_id,
                _request_session_hash(request, resolved_settings),
            ),
        )

    @application.get("/api/v1/verifications/{verification_id}/events")
    async def verification_events(
        verification_id: str, request: Request
    ) -> StreamingResponse:
        _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )

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
    def get_result(verification_id: str, request: Request) -> JSONResponse:
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
        try:
            result, _ = resolved_manager.result(verification_id)
        except JobConflictError as error:
            code = "VERIFICATION_FAILED" if job.status == JobStatus.FAILED else "RESULT_NOT_READY"
            raise HTTPException(
                status_code=409,
                detail={"code": code, "message": str(error), "error": job.error},
            ) from error
        return JSONResponse(result)

    @application.get("/api/v1/verifications/{verification_id}/documents")
    def get_documents(verification_id: str, request: Request) -> dict[str, Any]:
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
        if job.result:
            return {"items": job.result["documents"]}
        return {"items": job.input_files}

    @application.get(
        "/api/v1/verifications/{verification_id}/documents/{document_id}/content"
    )
    def get_document_content(
        verification_id: str, document_id: str, request: Request
    ):
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
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
        "/api/v1/verifications/{verification_id}/documents/{document_id}/pages/{page}"
    )
    def get_document_page(
        verification_id: str, document_id: str, page: int, request: Request
    ):
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
        if not job.result or job.workspace is None:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"})
        document = next(
            (item for item in job.result["documents"] if item["document_id"] == document_id),
            None,
        )
        if document is None:
            raise HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND"})
        artifact = next((item for item in document.get("pages", []) if item["page"] == page), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail={"code": "PAGE_NOT_FOUND"})
        path = resolve_page_path(job.workspace, artifact)
        if path is None:
            raise HTTPException(status_code=410, detail={"code": "PAGE_EXPIRED"})
        return FileResponse(path, media_type=artifact.get("media_type", "image/png"))

    @application.get(
        "/api/v1/verifications/{verification_id}/documents/{document_id}/spreadsheet"
    )
    def get_spreadsheet_preview(
        verification_id: str, document_id: str, request: Request, sheet: str | None = None
    ) -> dict[str, Any]:
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
        if not job.result or job.workspace is None:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"})
        if not resolved_manager.allow_spreadsheet_preview(
            job,
            limit=SPREADSHEET_PREVIEW_REQUEST_LIMIT,
            window_seconds=SPREADSHEET_PREVIEW_RATE_WINDOW_SECONDS,
        ):
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "SPREADSHEET_PREVIEW_RATE_LIMITED",
                    "message": "Слишком много запросов предпросмотра. Повторите позже.",
                },
            )
        document = next(
            (item for item in job.result["documents"] if item["document_id"] == document_id),
            None,
        )
        if document is None or document.get("extension") != ".xlsx":
            raise HTTPException(status_code=404, detail={"code": "SPREADSHEET_NOT_FOUND"})
        input_dir = (job.workspace / "input").resolve()
        path = (input_dir / document["file_name"]).resolve()
        if path.parent != input_dir or not path.is_file():
            raise HTTPException(status_code=410, detail={"code": "SPREADSHEET_EXPIRED"})
        try:
            return spreadsheet_preview(path, sheet)
        except KeyError as error:
            raise HTTPException(status_code=404, detail={"code": "SHEET_NOT_FOUND"}) from error
        except Exception as error:  # noqa: BLE001 - unsafe/invalid workbook boundary
            raise HTTPException(
                status_code=422,
                detail={"code": "SPREADSHEET_PREVIEW_FAILED", "message": str(error)},
            ) from error

    @application.get(
        "/api/v1/verifications/{verification_id}/documents/{document_id}/text-preview"
    )
    def get_document_text_preview(
        verification_id: str, document_id: str, request: Request
    ) -> dict[str, Any]:
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
        if not job.result or job.workspace is None:
            raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY"})
        document = next(
            (item for item in job.result["documents"] if item["document_id"] == document_id),
            None,
        )
        if document is None or document.get("extension") not in {".docx", ".txt"}:
            raise HTTPException(status_code=404, detail={"code": "TEXT_PREVIEW_NOT_FOUND"})
        input_dir = (job.workspace / "input").resolve()
        path = (input_dir / document["file_name"]).resolve()
        if path.parent != input_dir or not path.is_file():
            raise HTTPException(status_code=410, detail={"code": "TEXT_PREVIEW_EXPIRED"})
        try:
            if path.suffix.lower() == ".docx":
                content = extract_docx(path)
                kind = "DOCX"
            else:
                lines = path.read_text(encoding="utf-8").splitlines()
                return {
                    "kind": "TEXT",
                    "items": [
                        {"locator": f"line:{index}", "text": line}
                        for index, line in enumerate(lines, 1)
                        if line.strip()
                    ],
                }
            return {
                "kind": kind,
                "items": [
                    {"locator": evidence.locator, "text": evidence.raw}
                    for evidence in content.evidence_by_line
                ],
            }
        except Exception as error:  # noqa: BLE001 - document preview boundary
            raise HTTPException(
                status_code=422,
                detail={"code": "TEXT_PREVIEW_FAILED", "message": str(error)},
            ) from error

    @application.get(
        "/api/v1/verifications/{verification_id}/evidence/{evidence_id:path}"
    )
    def get_evidence(
        verification_id: str, evidence_id: str, request: Request
    ) -> dict[str, Any]:
        job = _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
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
    def export_result(
        verification_id: str, request: Request, format: str = "json"
    ):
        _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
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
    def rerun_verification(
        verification_id: str, request: Request
    ) -> VerificationAccepted:
        _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
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

    @application.post(
        "/api/v1/verifications/{verification_id}/possible-duplicate-decisions",
        response_model=VerificationAccepted,
        status_code=202,
    )
    def decide_possible_duplicate(
        verification_id: str, payload: PossibleDuplicateDecisionInput, request: Request
    ) -> VerificationAccepted:
        source = _job_or_http(
            resolved_manager, verification_id, _request_session_hash(request, resolved_settings)
        )
        if source.result is None:
            raise HTTPException(409, detail={"code": "RESULT_NOT_READY"})
        candidate = next(
            (item for item in source.result["documents"]
             if item["file_name"] == payload.file_name
             and item.get("reason_code") == "POSSIBLE_DUPLICATE_DOCUMENT"),
            None,
        )
        if candidate is None:
            raise HTTPException(422, detail={"code": "POSSIBLE_DUPLICATE_NOT_FOUND", "message": "В текущем результате нет такого вероятного дубля."})
        decisions = dict(source.context.get("possible_duplicate_decisions", {}))
        decisions[payload.file_name] = payload.decision
        try:
            job = resolved_manager.rerun(
                verification_id, context_patch={"possible_duplicate_decisions": decisions}
            )
        except JobConflictError as error:
            raise HTTPException(409, detail={"code": "SOURCE_DOCUMENTS_NOT_AVAILABLE", "message": str(error)}) from error
        return VerificationAccepted(
            verification_id=job.verification_id,
            status=job.status,
            storage=_storage_status(resolved_manager, job),
        )

    @application.delete(
        "/api/v1/verifications/{verification_id}", status_code=204
    )
    def delete_verification(verification_id: str, request: Request):
        _job_or_http(
            resolved_manager,
            verification_id,
            _request_session_hash(request, resolved_settings),
        )
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
