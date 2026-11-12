#!/usr/bin/env python3
"""Refresh evidence fields in a course batch manifest.

``prepare_asset_learning.py`` intentionally writes a manifest before its
long-running extraction jobs start.  If a job is interrupted or finishes in a
child process, the original manifest can therefore remain stale even though
the evidence files are present.  This read-only-with-respect-to-course-assets
helper reconciles each record with the evidence directory and never promotes
``learning_status``; a human still has to create/verify a stable ``SRC``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def refresh_record(record: dict[str, Any], evidence_root: Path, kb_root: Path | None) -> dict[str, Any]:
    out = dict(record)
    asset_id = str(out.get("asset_id") or "")
    directory = evidence_root / asset_id
    files = [p for p in directory.rglob("*") if p.is_file()] if directory.is_dir() else []
    names = {p.name for p in files}
    transcripts = sorted(p for p in files if p.name.endswith(".transcript.json"))
    transcript_txt = sorted(p for p in files if p.name.endswith(".transcript.txt"))
    frames = sorted(p for p in files if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and "keyframe" in str(p.parent).lower())
    keyframe_manifests = sorted(p for p in files if p.name == "keyframes.manifest.json")
    extracts = sorted(p for p in files if p.name.endswith(".extract.txt") or p.name.endswith(".html.json"))
    ocr = sorted(p for p in files if ".ocr." in p.name or p.name.endswith(".ocr.txt"))

    kind = str(out.get("kind") or out.get("category") or "").lower()
    artifact_paths: list[str] = []
    for path in sorted(set(transcripts + transcript_txt + frames + keyframe_manifests + extracts + ocr)):
        if kb_root:
            try:
                artifact_paths.append(str(path.resolve().relative_to(kb_root.resolve())))
                continue
            except (OSError, ValueError, RuntimeError):
                pass
        artifact_paths.append(str(path))
    if artifact_paths:
        out["artifact_paths"] = sorted(set(artifact_paths), key=str.casefold)

    if kind == "video":
        out["transcript_status"] = "complete" if transcripts and transcript_txt else "missing"
        out["keyframes_status"] = "complete" if keyframe_manifests and len(frames) >= 5 else ("partial" if frames else "missing")
        if out["transcript_status"] == "complete" and out["keyframes_status"] == "complete":
            out["evidence_status"] = "transcript_and_keyframes"
        elif out["transcript_status"] == "complete":
            out["evidence_status"] = "transcript_complete"
        elif out["keyframes_status"] == "complete":
            out["evidence_status"] = "keyframes_complete"
    elif kind in {"document", "image", "html"}:
        out["extraction_status"] = "complete" if extracts else out.get("extraction_status", "pending")
        if ocr:
            out["ocr_status"] = "complete"
        if kind == "html" and extracts:
            # External resources remain a declared boundary; this helper only
            # confirms that the local snapshot was parsed.
            out["external_dependency_status"] = out.get("external_dependency_status") or "links_only"
            out["evidence_status"] = "external_unverified" if out["external_dependency_status"] == "links_only" else "extraction_complete"
        elif extracts or ocr:
            out["evidence_status"] = "ocr_or_extraction_complete"

    out["evidence_refreshed_at"] = now()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新批次 manifest 的证据状态，不提升学习状态")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True, help="通常为知识库/02-转写与OCR/全量")
    parser.add_argument("--kb-root", type=Path)
    args = parser.parse_args()
    manifest = args.manifest.expanduser().resolve()
    evidence_root = args.evidence_root.expanduser().resolve()
    kb_root = args.kb_root.expanduser().resolve() if args.kb_root else None
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("manifest 缺少 records 数组")
    payload["records"] = [refresh_record(item, evidence_root, kb_root) for item in records if isinstance(item, dict)]
    payload["evidence_refreshed_at"] = now()
    # Keep the batch in evidence_preparation unless a human has already
    # declared a terminal learning state.  In particular, never infer
    # completed_learning from the presence of transcript/OCR files.
    if payload.get("status") not in {"completed_learning", "blocked", "superseded"}:
        payload["status"] = "evidence_preparation"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "manifest": str(manifest), "records": len(payload["records"]), "status": payload.get("status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
