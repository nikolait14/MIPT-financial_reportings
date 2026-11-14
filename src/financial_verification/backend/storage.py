from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class WorkspaceStore(ABC):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_path(self, verification_id: str) -> Path:
        path = (self.root / verification_id).resolve()
        if path.parent != self.root:
            raise ValueError("Invalid verification identifier")
        return path

    def create(self, verification_id: str) -> Path:
        run_path = self.run_path(verification_id)
        run_path.mkdir(parents=False, exist_ok=False)
        (run_path / "input").mkdir()
        (run_path / "artifacts").mkdir()
        return run_path

    def delete(self, verification_id: str) -> None:
        run_path = self.run_path(verification_id)
        if run_path.exists():
            shutil.rmtree(run_path)

    def cleanup_orphans(self) -> None:
        """Remove workspaces that cannot be associated with a live in-memory job."""

        return None

    @abstractmethod
    def persist_manifest(self, verification_id: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_manifests(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class EphemeralWorkspaceStore(WorkspaceStore):
    def cleanup_orphans(self) -> None:
        for path in self.root.iterdir():
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)

    def persist_manifest(self, verification_id: str, payload: dict[str, Any]) -> None:
        return None

    def load_manifests(self) -> list[dict[str, Any]]:
        return []


class PersistentWorkspaceStore(WorkspaceStore):
    def persist_manifest(self, verification_id: str, payload: dict[str, Any]) -> None:
        run_path = self.run_path(verification_id)
        run_path.mkdir(parents=True, exist_ok=True)
        temporary = run_path / "manifest.json.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(run_path / "manifest.json")

    def load_manifests(self) -> list[dict[str, Any]]:
        manifests = []
        for path in sorted(self.root.glob("*/manifest.json")):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return manifests

    def persist_result(
        self,
        verification_id: str,
        result: dict[str, Any],
        markdown: str,
    ) -> None:
        run_path = self.run_path(verification_id)
        (run_path / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_path / "report.md").write_text(markdown, encoding="utf-8")

    def load_result(self, verification_id: str) -> tuple[dict[str, Any], str] | None:
        run_path = self.run_path(verification_id)
        result_path = run_path / "result.json"
        markdown_path = run_path / "report.md"
        if not result_path.exists() or not markdown_path.exists():
            return None
        return (
            json.loads(result_path.read_text(encoding="utf-8")),
            markdown_path.read_text(encoding="utf-8"),
        )
