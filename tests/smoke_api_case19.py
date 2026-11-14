#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--data-dir", type=Path, default=Path("/app/data/case-19/all"))
    parser.add_argument(
        "--context",
        type=Path,
        default=Path("/app/config/case19-verification-context.json"),
    )
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    args = parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    paths = sorted(path for path in args.data_dir.iterdir() if path.is_file())
    require(paths, f"No input files in {args.data_dir}")

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        ready = None
        ready_deadline = time.monotonic() + 60
        while time.monotonic() < ready_deadline:
            try:
                ready = client.get("/health/ready")
                if ready.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.5)
        require(ready is not None and ready.status_code == 200, "API did not become ready")
        capabilities = client.get("/api/v1/capabilities")
        capabilities.raise_for_status()
        require(
            capabilities.json()["storage"]["mode"] == "EPHEMERAL",
            "Smoke test expects EPHEMERAL mode",
        )

        with ExitStack() as stack:
            files = [
                (
                    "files",
                    (
                        path.name,
                        stack.enter_context(path.open("rb")),
                        "application/octet-stream",
                    ),
                )
                for path in paths
            ]
            response = client.post(
                "/api/v1/verifications",
                data={"context": json.dumps(context, ensure_ascii=False)},
                files=files,
            )
        response.raise_for_status()
        verification_id = response.json()["verification_id"]

        deadline = time.monotonic() + args.timeout
        status_payload = None
        while time.monotonic() < deadline:
            status_response = client.get(f"/api/v1/verifications/{verification_id}")
            status_response.raise_for_status()
            status_payload = status_response.json()
            if status_payload["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        require(status_payload is not None, "No status response")
        require(
            status_payload["status"] == "COMPLETED",
            f"Verification failed: {status_payload}",
        )

        result_response = client.get(
            f"/api/v1/verifications/{verification_id}/result"
        )
        result_response.raise_for_status()
        result = result_response.json()
        summary = result["summary"]
        require(
            summary["conclusion"]["status"] == "REPORT_UNRELIABLE",
            f"Unexpected conclusion: {summary['conclusion']}",
        )
        require(summary["report_total"] == "11580377.40", str(summary))
        require(summary["supported_total"] == "11567877.40", str(summary))
        require(summary["delta"] == "12500.00", str(summary))
        require(summary["included_documents"] == 19, str(summary))
        require(summary["review_documents"] == 0, str(summary))
        require(summary["technical_errors"] == 0, str(summary))
        require(len(result["findings"]) == 1, str(result["findings"]))

        finding = result["findings"][0]
        require(finding["article_code"] == "0800", str(finding))
        require(finding["delta"] == "12500.00", str(finding))
        require(len(finding["source_evidence_ids"]) == 4, str(finding))
        for evidence_id in [
            finding["report_evidence_id"],
            *finding["source_evidence_ids"],
        ]:
            evidence = client.get(
                f"/api/v1/verifications/{verification_id}/evidence/{evidence_id}"
            )
            evidence.raise_for_status()
            evidence_payload = evidence.json()
            require(
                evidence_payload["document_sha256"],
                f"Evidence {evidence_id} has no source hash",
            )
            if evidence_id == finding["report_evidence_id"]:
                bbox = evidence_payload["locator"].get("bbox")
                require(bbox is not None and len(bbox) == 4, str(evidence_payload))
                require(
                    all(0 <= value <= 1 for value in bbox),
                    f"Report bbox is not normalized: {bbox}",
                )

        source_document = next(
            item
            for item in result["documents"]
            if item["role"] == "SOURCE_DOCUMENT"
        )
        content = client.get(
            f"/api/v1/verifications/{verification_id}/documents/"
            f"{source_document['document_id']}/content"
        )
        require(content.status_code == 410, f"Expected 410, got {content.status_code}")

        markdown = client.get(
            f"/api/v1/verifications/{verification_id}/export?format=markdown"
        )
        markdown.raise_for_status()
        require("Обнаруженные расхождения" in markdown.text, "Missing report section")

        print(
            json.dumps(
                {
                    "status": "PASS",
                    "verification_id": verification_id,
                    "files_uploaded": len(paths),
                    "conclusion": summary["conclusion"],
                    "delta": summary["delta"],
                    "evidence_checked": 1 + len(finding["source_evidence_ids"]),
                    "ephemeral_document_response": content.status_code,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
