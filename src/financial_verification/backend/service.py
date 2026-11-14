from __future__ import annotations

import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from financial_verification.backend.config import ApiSettings
from financial_verification.backend.models import JobStatus, TERMINAL_STATUSES
from financial_verification.backend.pipeline_adapter import (
    LocalPipelineRunner,
    PipelineExecutionError,
    VerificationRunner,
)
from financial_verification.backend.result_builder import (
    build_verification_result,
    render_informative_markdown,
)
from financial_verification.backend.storage import (
    EphemeralWorkspaceStore,
    PersistentWorkspaceStore,
    WorkspaceStore,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobNotFoundError(KeyError):
    pass


class JobExpiredError(KeyError):
    pass


class JobConflictError(RuntimeError):
    pass


@dataclass
class JobRecord:
    verification_id: str
    context: dict[str, Any]
    workspace: Path | None
    status: JobStatus = JobStatus.QUEUED
    stage: str = "QUEUED"
    progress: int = 0
    message: str = "Задание принято"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    markdown: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    input_files: list[dict[str, Any]] = field(default_factory=list)

    def manifest(self, storage_mode: str) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "context": self.context,
            "status": self.status.value,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "error": self.error,
            "input_files": self.input_files,
            "storage_mode": storage_mode,
        }


class VerificationJobManager:
    def __init__(
        self,
        settings: ApiSettings,
        *,
        runner: VerificationRunner | None = None,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or LocalPipelineRunner(
            stage_timeout_seconds=settings.pipeline_stage_timeout_seconds
        )
        if workspace_store is not None:
            self.storage = workspace_store
        elif settings.is_persistent:
            self.storage = PersistentWorkspaceStore(settings.storage_root)
        else:
            self.storage = EphemeralWorkspaceStore(settings.temporary_root)
        if not settings.is_persistent:
            self.storage.cleanup_orphans()
        self._jobs: dict[str, JobRecord] = {}
        self._expired: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.job_workers,
            thread_name_prefix="financial-verification",
        )
        if settings.is_persistent:
            self._load_persistent_jobs()
            self.cleanup_expired()

    @property
    def storage_mode(self) -> str:
        return "PERSISTENT" if self.settings.is_persistent else "EPHEMERAL"

    def _load_persistent_jobs(self) -> None:
        assert isinstance(self.storage, PersistentWorkspaceStore)
        for manifest in self.storage.load_manifests():
            try:
                status = JobStatus(manifest["status"])
                job = JobRecord(
                    verification_id=manifest["verification_id"],
                    context=manifest["context"],
                    workspace=self.storage.run_path(manifest["verification_id"]),
                    status=status,
                    stage=manifest.get("stage", status.value),
                    progress=int(manifest.get("progress", 0)),
                    message=manifest.get("message", ""),
                    created_at=datetime.fromisoformat(manifest["created_at"]),
                    updated_at=datetime.fromisoformat(manifest["updated_at"]),
                    expires_at=(
                        datetime.fromisoformat(manifest["expires_at"])
                        if manifest.get("expires_at")
                        else None
                    ),
                    error=manifest.get("error"),
                    input_files=manifest.get("input_files", []),
                )
                loaded = self.storage.load_result(job.verification_id)
                if loaded:
                    job.result, job.markdown = loaded
                if status not in TERMINAL_STATUSES:
                    job.status = JobStatus.FAILED
                    job.stage = "INTERRUPTED"
                    job.message = "Обработка прервана перезапуском backend"
                    job.error = {
                        "code": "BACKEND_RESTARTED",
                        "message": job.message,
                    }
                    job.updated_at = utc_now()
                    if job.expires_at is None:
                        job.expires_at = utc_now() + timedelta(
                            days=self.settings.retention_days
                        )
                    self.storage.persist_manifest(
                        job.verification_id, job.manifest(self.storage_mode)
                    )
                self._jobs[job.verification_id] = job
            except (KeyError, ValueError, TypeError):
                continue

    def allocate(self, context: dict[str, Any]) -> JobRecord:
        self.cleanup_expired()
        verification_id = str(uuid.uuid4())
        workspace = self.storage.create(verification_id)
        job = JobRecord(
            verification_id=verification_id,
            context=context,
            workspace=workspace,
        )
        job.events.append(
            {
                "event": "accepted",
                "data": {
                    "verification_id": verification_id,
                    "status": JobStatus.QUEUED.value,
                },
            }
        )
        with self._lock:
            self._jobs[verification_id] = job
            self._persist_manifest(job)
        return job

    def abandon(self, verification_id: str) -> None:
        with self._lock:
            self._jobs.pop(verification_id, None)
        self.storage.delete(verification_id)

    def submit(self, verification_id: str) -> None:
        job = self.get(verification_id)
        if job.status != JobStatus.QUEUED:
            raise JobConflictError("Only queued jobs can be submitted")
        self._executor.submit(self._run_job, verification_id)

    def record_inputs(
        self, verification_id: str, input_files: list[dict[str, Any]]
    ) -> None:
        job = self.get(verification_id)
        with self._lock:
            job.input_files = input_files
            job.updated_at = utc_now()
            self._persist_manifest(job)

    def _run_job(self, verification_id: str) -> None:
        job = self.get(verification_id)
        self._update(job, JobStatus.PROCESSING, "STARTING", 1, "Запуск обработки")
        try:
            if job.workspace is None:
                raise RuntimeError("Job workspace is unavailable")
            output = self.runner.run(
                workspace=job.workspace,
                context=job.context,
                rules_path=self.settings.default_rules_path,
                progress=lambda stage, progress, message: self._update(
                    job, JobStatus.PROCESSING, stage, progress, message
                ),
            )
            result = build_verification_result(
                verification_id=job.verification_id,
                context=job.context,
                registry=output.registry,
                reconciliation=output.reconciliation,
                extraction=output.extraction,
                artifacts_dir=output.artifacts_dir,
                storage_mode=self.storage_mode,
                confidence_warning=self.settings.ocr_confidence_warning,
            )
            markdown = render_informative_markdown(result)
            with self._lock:
                job.result = result
                job.markdown = markdown
                if self.settings.is_persistent:
                    job.expires_at = utc_now() + timedelta(
                        days=self.settings.retention_days
                    )
                else:
                    job.expires_at = utc_now() + timedelta(
                        seconds=self.settings.result_ttl_seconds
                    )
                if self.settings.is_persistent:
                    assert isinstance(self.storage, PersistentWorkspaceStore)
                    self.storage.persist_result(job.verification_id, result, markdown)
            self._update(
                job,
                JobStatus.COMPLETED,
                "COMPLETED",
                100,
                "Проверка завершена",
                event="completed",
            )
        except PipelineExecutionError as error:
            self._fail(job, error.stage, "PIPELINE_STAGE_FAILED", str(error))
        except Exception as error:  # noqa: BLE001 - worker boundary
            self._fail(job, "INTERNAL", "INTERNAL_ERROR", str(error))
        finally:
            if not self.settings.is_persistent:
                self.storage.delete(job.verification_id)
                with self._lock:
                    job.workspace = None

    def _fail(self, job: JobRecord, stage: str, code: str, message: str) -> None:
        with self._lock:
            job.error = {"code": code, "stage": stage, "message": message}
            if self.settings.is_persistent:
                job.expires_at = utc_now() + timedelta(
                    days=self.settings.retention_days
                )
            else:
                job.expires_at = utc_now() + timedelta(
                    seconds=self.settings.result_ttl_seconds
                )
        self._update(
            job,
            JobStatus.FAILED,
            stage,
            job.progress,
            "Проверка завершилась технической ошибкой",
            event="failed",
        )

    def _update(
        self,
        job: JobRecord,
        status: JobStatus,
        stage: str,
        progress: int,
        message: str,
        *,
        event: str = "progress",
    ) -> None:
        with self._lock:
            job.status = status
            job.stage = stage
            job.progress = max(0, min(100, progress))
            job.message = message
            job.updated_at = utc_now()
            job.events.append(
                {
                    "event": event,
                    "data": {
                        "verification_id": job.verification_id,
                        "status": status.value,
                        "stage": stage,
                        "progress": job.progress,
                        "message": message,
                    },
                }
            )
            self._persist_manifest(job)

    def _persist_manifest(self, job: JobRecord) -> None:
        self.storage.persist_manifest(
            job.verification_id, job.manifest(self.storage_mode)
        )

    def get(self, verification_id: str) -> JobRecord:
        self.cleanup_expired()
        with self._lock:
            if verification_id in self._expired:
                raise JobExpiredError(verification_id)
            try:
                return self._jobs[verification_id]
            except KeyError as error:
                raise JobNotFoundError(verification_id) from error

    def list(self) -> list[JobRecord]:
        self.cleanup_expired()
        with self._lock:
            return sorted(
                self._jobs.values(), key=lambda item: item.created_at, reverse=True
            )

    def events(self, verification_id: str, cursor: int) -> tuple[list[dict[str, Any]], bool]:
        job = self.get(verification_id)
        with self._lock:
            events = list(job.events[cursor:])
            terminal = job.status in TERMINAL_STATUSES
        return events, terminal

    def result(self, verification_id: str) -> tuple[dict[str, Any], str]:
        job = self.get(verification_id)
        if job.status == JobStatus.FAILED:
            raise JobConflictError("Verification failed")
        if job.status != JobStatus.COMPLETED or job.result is None or job.markdown is None:
            raise JobConflictError("Verification is not complete")
        return job.result, job.markdown

    def delete(self, verification_id: str) -> None:
        job = self.get(verification_id)
        if job.status not in TERMINAL_STATUSES:
            raise JobConflictError("A running verification cannot be deleted")
        self.storage.delete(verification_id)
        with self._lock:
            job.status = JobStatus.DELETED
            self._jobs.pop(verification_id, None)

    def rerun(self, verification_id: str) -> JobRecord:
        if not self.settings.is_persistent:
            raise JobConflictError("Source documents are not available")
        source = self.get(verification_id)
        if source.workspace is None or not (source.workspace / "input").exists():
            raise JobConflictError("Source documents are not available")
        target = self.allocate(dict(source.context))
        assert target.workspace is not None
        for path in (source.workspace / "input").iterdir():
            if path.is_file():
                shutil.copy2(path, target.workspace / "input" / path.name)
        target.input_files = list(source.input_files)
        self._persist_manifest(target)
        self.submit(target.verification_id)
        return target

    def cleanup_expired(self) -> None:
        now = utc_now()
        with self._lock:
            expired_ids = [
                verification_id
                for verification_id, job in self._jobs.items()
                if job.expires_at is not None and job.expires_at <= now
            ]
            for verification_id in expired_ids:
                self.storage.delete(verification_id)
                self._jobs.pop(verification_id, None)
                self._expired[verification_id] = now
            old_tombstones = [
                verification_id
                for verification_id, expired_at in self._expired.items()
                if expired_at + timedelta(seconds=self.settings.result_ttl_seconds) <= now
            ]
            for verification_id in old_tombstones:
                self._expired.pop(verification_id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
