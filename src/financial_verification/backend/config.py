from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class ApiSettings:
    storage_mode: str = "ephemeral"
    temporary_root: Path = Path("/tmp/financial-verification-api")
    storage_root: Path = Path("/workspace/storage")
    config_dir: Path = Path("/workspace/config")
    result_ttl_seconds: int = 1800
    retention_days: int = 30
    max_files: int = 100
    max_file_size_bytes: int = 50 * 1024 * 1024
    max_total_size_bytes: int = 250 * 1024 * 1024
    job_workers: int = 1
    pipeline_stage_timeout_seconds: int = 600
    ocr_confidence_warning: float = 0.80

    @classmethod
    def from_env(cls) -> "ApiSettings":
        storage_mode = os.getenv("STORAGE_MODE", "ephemeral").strip().lower()
        if storage_mode not in {"ephemeral", "persistent"}:
            raise ValueError("STORAGE_MODE must be 'ephemeral' or 'persistent'")
        return cls(
            storage_mode=storage_mode,
            temporary_root=Path(
                os.getenv("API_TEMP_ROOT", "/tmp/financial-verification-api")
            ),
            storage_root=Path(os.getenv("STORAGE_ROOT", "/workspace/storage")),
            config_dir=Path(os.getenv("CONFIG_DIR", "/workspace/config")),
            result_ttl_seconds=_positive_int("RESULT_TTL_SECONDS", 1800),
            retention_days=_positive_int("RETENTION_DAYS", 30),
            max_files=_positive_int("MAX_FILES", 100),
            max_file_size_bytes=_positive_int(
                "MAX_FILE_SIZE_BYTES", 50 * 1024 * 1024
            ),
            max_total_size_bytes=_positive_int(
                "MAX_TOTAL_SIZE_BYTES", 250 * 1024 * 1024
            ),
            job_workers=_positive_int("JOB_WORKERS", 1),
            pipeline_stage_timeout_seconds=_positive_int(
                "PIPELINE_STAGE_TIMEOUT_SECONDS", 600
            ),
            ocr_confidence_warning=float(
                os.getenv("OCR_CONFIDENCE_WARNING", "0.80")
            ),
        )

    @property
    def is_persistent(self) -> bool:
        return self.storage_mode == "persistent"

    @property
    def default_context_path(self) -> Path:
        return self.config_dir / "case19-verification-context.json"

    @property
    def default_rules_path(self) -> Path:
        return self.config_dir / "cost-article-rules.json"

