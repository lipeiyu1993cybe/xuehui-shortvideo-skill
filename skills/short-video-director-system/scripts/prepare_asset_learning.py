#!/usr/bin/env python3
"""Prepare resumable evidence batches for videos and non-video course assets.

This command is deliberately evidence-first.  It can extract/attach artifacts
and create review drafts, but never creates canonical SRC/KNW notes or marks an
asset learned.  Run it repeatedly with the same ledger: existing artifacts are
reused and the next invocation can continue from the remaining queue.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPTS = Path(__file__).resolve().parent
TRANSCRIBE = SCRIPTS / "transcribe_course.py"
KEYFRAMES = SCRIPTS / "extract_course_keyframes.py"
DOCS = SCRIPTS / "extract_course_documents.py"
PDF_OCR = SCRIPTS / "ocr_course_pdfs.py"
IMAGE_OCR = SCRIPTS / "ocr_course_images.py"
HTML = SCRIPTS / "extract_course_html.py"
CACHE = Path("/Users/lipeiyu/.codex/skills/research-capture/scripts/capture_cache.py")


def load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("records"), list):
        raise SystemExit(f"账本缺少 records: {path}")
    return payload


def revision(kb_root: Path) -> int:
    text = (kb_root / "_index.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^kb_revision:\s*(\d+)", text)
    return int(match.group(1)) if match else 0


def choose(
    records: list[dict[str, Any]],
    max_items: int,
    max_minutes: float | None,
    include: str | None,
    exclude: str | None,
    kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    include_re = re.compile(include) if include else None
    exclude_re = re.compile(exclude) if exclude else None
    # Select one deterministic representative per SHA/asset ID.  Physical
    # aliases remain in the ledger, but preparing evidence twice for identical
    # bytes wastes ASR/OCR time and can create conflicting drafts.
    candidates_by_key: dict[str, dict[str, Any]] = {}
    for row in records:
        if row.get("download_status") != "complete" or row.get("learning_status") == "learned":
            continue
        if row.get("integrity_status") not in {"valid", "partial"}:
            continue
        if kinds and str(row.get("kind") or "") not in kinds:
            continue
        if include_re and not include_re.search(str(row.get("relative_path", ""))):
            continue
        if exclude_re and exclude_re.search(str(row.get("relative_path", ""))):
            continue
        key = str(row.get("sha256") or row.get("asset_id") or row.get("relative_path") or "")
        old = candidates_by_key.get(key)
        if old is None or str(row.get("relative_path", "")).casefold() < str(old.get("relative_path", "")).casefold():
            candidates_by_key[key] = row
    candidates = list(candidates_by_key.values())
    candidates.sort(key=lambda r: str(r.get("relative_path", "")).casefold())
    selected: list[dict[str, Any]] = []
    seconds = 0.0
    for row in candidates:
        duration = float(row.get("duration_seconds") or 0)
        if selected and max_minutes is not None and seconds + duration > max_minutes * 60:
            break
        selected.append(row)
        seconds += duration
        if len(selected) >= max_items:
            break
    return selected


def run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return result.returncode, output.strip()


def safe(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "-", value).strip()
    return value[:100] or "asset"


def draft(row: dict[str, Any], source: Path, manifest_path: Path, evidence_dir: Path, base_revision: int) -> str:
    aid = str(row.get("asset_id"))
    kind = str(row.get("kind"))
    return f"""---
note_id: DRAFT-ASSET-{aid}
type: course_asset_distillation
title: {json.dumps(source.stem, ensure_ascii=False)}
status: evidence_pending_review
asset_id: {aid}
base_kb_revision: {base_revision}
created_at: {datetime.now().astimezone().date().isoformat()}
source_authority: local_complete_original
---

# 安先生资料｜{source.stem}

## 原件与状态

- 类型：`{kind}`；相对路径：`{row.get('relative_path','')}`
- SHA-256：`{row.get('sha256','')}`；大小：`{row.get('size_bytes','')}` 字节
- 原件：`{source}`
- 别名路径：`{json.dumps(row.get('alias_paths', []), ensure_ascii=False)}`
- 完整性：`{row.get('integrity_status','')}`；阻塞原因：`{row.get('blocked_reason','')}`
- 证据目录：`{evidence_dir}`

## 证据准备

本草稿由自动抽取/OCR/转写产生，仅供逐段、逐页或逐文件核读。它不会自动创建 `SRC`、`KNW`、`MOC`，也不会把 `learning_status` 提升为 `learned`。

## 正式入库门槛

1. 回到原件核验完整覆盖（视频到片尾、文档逐页/逐表、HTML 正文/日期/链接/外部依赖、图片视觉内容）。
2. 分离课程原词、课程观点、执行归纳、平台历史事实和待核验项。
3. 与 canonical 来源/方法地图判断重复、补充、冲突、版本和适用边界。
4. 写应用转译：账号问题、真实输入、动作、验证信号、失效条件、安全边界。
5. 建立可回查 `SRC` 后才可标记 learned，并同步 `kb_revision`。

批次清单：`{manifest_path}`
"""


def attach(cache_db: Path, source: Path, artifact: Path, kind: str) -> str:
    if not artifact.is_file():
        return ""
    code, output = run([sys.executable, str(CACHE), "attach-artifact", "--db", str(cache_db), "--source", str(source), "--artifact", str(artifact), "--kind", kind])
    return output if code == 0 else f"attach failed: {output[-500:]}"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成跨媒介课程学习证据批次（不自动入库）")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--kb-root", type=Path, required=True)
    parser.add_argument("--transcript-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--cache-db", type=Path, default=Path("/Users/lipeiyu/.codex/research-capture/cache.sqlite3"))
    parser.add_argument("--max-items", type=int, default=1)
    parser.add_argument("--max-minutes", type=float)
    parser.add_argument("--include-regex")
    parser.add_argument("--exclude-regex")
    parser.add_argument(
        "--kind",
        dest="kinds",
        action="append",
        choices=["video", "document", "image", "html"],
        help="只选择指定媒介类型；可重复指定，默认包含全部类型",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--keyframes", action="store_true")
    parser.add_argument("--documents", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--html", action="store_true")
    parser.add_argument("--ffmpeg", type=Path, default=Path("/Applications/ChatCut.app/Contents/Resources/app.asar.unpacked/node_modules/ffmpeg-ffprobe-static/ffmpeg"))
    parser.add_argument("--vision-script", type=Path, default=SCRIPTS / "vision_ocr.swift")
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--chunk-seconds", type=int, default=1800)
    parser.add_argument(
        "--python",
        dest="python_bin",
        type=Path,
        help="用于调用转写/文档/OCR 子脚本的 Python；默认当前解释器",
    )
    args = parser.parse_args()
    for name in ("ledger", "archive_root", "kb_root", "transcript_root", "manifest", "inbox", "cache_db", "ffmpeg", "vision_script"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    # Do not resolve an explicitly supplied Python executable.  A virtualenv's
    # ``bin/python3`` is commonly a symlink to its base interpreter; resolving
    # it bypasses the virtualenv prefix and makes optional packages such as
    # ``mlx_whisper`` disappear from ``sys.path``.  We only need an existing
    # executable path here, so preserve the user-provided launcher verbatim.
    python_bin = args.python_bin.expanduser() if args.python_bin else Path(sys.executable)
    if not python_bin.is_file():
        raise SystemExit(f"Python 不存在：{python_bin}")
    payload = load(args.ledger)
    selected = choose(
        payload["records"],
        args.max_items,
        args.max_minutes,
        args.include_regex,
        args.exclude_regex,
        set(args.kinds or []),
    )
    base_revision = revision(args.kb_root)
    report: dict[str, Any] = {"ok": True, "mode": "dry_run" if args.dry_run else "evidence_preparation", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "base_kb_revision": base_revision, "ledger": str(args.ledger), "selected": len(selected), "records": selected, "results": []}
    if args.dry_run:
        report["selected_relative_paths"] = [str(row.get("relative_path")) for row in selected]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.transcript_root.mkdir(parents=True, exist_ok=True)
    args.inbox.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps({"schema_version": 1, "status": "evidence_preparation", "base_kb_revision": base_revision, "generated_at": report["generated_at"], "records": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in selected:
        source = args.archive_root / str(row.get("relative_path"))
        aid = str(row.get("asset_id"))
        evidence_dir = args.transcript_root / "全量" / aid
        evidence_dir.mkdir(parents=True, exist_ok=True)
        item: dict[str, Any] = {"asset_id": aid, "source": str(source), "kind": row.get("kind"), "actions": []}
        if source.suffix.lower() in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
            if args.transcribe:
                code, output = run([str(python_bin), str(TRANSCRIBE), str(source), "--output-dir", str(evidence_dir), "--model", args.model, "--language", "zh", "--chunk-seconds", str(args.chunk_seconds), "--ffmpeg", str(args.ffmpeg)])
                item["actions"].append({"action": "transcribe", "code": code, "output": output[-1000:]})
            if args.keyframes:
                # The keyframe utility accepts the batch manifest and retains
                # the original relative path/SHA in its manifest.  Feed it a
                # one-record manifest here; otherwise every selected asset
                # would be re-extracted into each asset's evidence directory.
                kdir = evidence_dir / "keyframes"
                single_manifest = evidence_dir / "keyframe-input.manifest.json"
                single_manifest.write_text(
                    json.dumps(
                        {"archive_root": str(args.archive_root), "records": [row]},
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                code, output = run([str(python_bin), str(KEYFRAMES), str(single_manifest), "--output-dir", str(kdir), "--ffmpeg", str(args.ffmpeg), "--positions", "0.05,0.25,0.5,0.75,0.95"])
                item["actions"].append({"action": "keyframes", "code": code, "output": output[-1000:]})
        elif str(row.get("kind")) == "document":
            if args.documents:
                code, output = run([str(python_bin), str(DOCS), str(source), "--output-dir", str(evidence_dir)])
                item["actions"].append({"action": "document_extract", "code": code, "output": output[-1000:]})
            if args.ocr and source.suffix.lower() == ".pdf":
                code, output = run([str(python_bin), str(PDF_OCR), str(source), "--output-dir", str(evidence_dir / "ocr"), "--vision-script", str(args.vision_script)])
                item["actions"].append({"action": "pdf_ocr", "code": code, "output": output[-1000:]})
        elif str(row.get("kind")) == "image" and args.ocr:
            code, output = run([str(python_bin), str(IMAGE_OCR), str(source), "--output-dir", str(evidence_dir / "ocr"), "--vision-script", str(args.vision_script)])
            item["actions"].append({"action": "image_ocr", "code": code, "output": output[-1000:]})
        elif str(row.get("kind")) == "html" and args.html:
            code, output = run([str(python_bin), str(HTML), str(source), "--output-dir", str(evidence_dir)])
            item["actions"].append({"action": "html_extract", "code": code, "output": output[-1000:]})
        # Attach every evidence file produced for this asset.  Artifact kinds
        # remain explicit so later lookup can reuse them without guessing.
        for artifact in sorted(evidence_dir.rglob("*")):
            if not artifact.is_file() or artifact.name == args.manifest.name:
                continue
            suffix = artifact.suffix.lower()
            if ".transcript." in artifact.name:
                kind = "transcript"
            elif ".ocr." in artifact.name or artifact.name.endswith(".ocr.txt"):
                kind = "ocr"
            elif artifact.name.endswith(".extract.txt"):
                kind = "document_text"
            elif artifact.name.endswith(".html.json") or artifact.name == "html-extraction-manifest.json":
                kind = "html_text"
            elif "keyframe" in str(artifact.parent).lower() or artifact.name.endswith(".manifest.json"):
                kind = "keyframes" if suffix in {".jpg", ".jpeg", ".png", ".webp"} else "keyframes_manifest"
            else:
                continue
            attached = attach(args.cache_db, source, artifact, kind)
            if attached:
                item.setdefault("attached", []).append({"artifact": str(artifact), "kind": kind, "result": attached[-500:]})
        note = args.inbox / f"{datetime.now().date().isoformat()}-{safe(aid)}-{safe(source.stem)}.md"
        if not note.exists():
            note.write_text(draft(row, source, args.manifest, evidence_dir, base_revision), encoding="utf-8")
        item["draft"] = str(note)
        report["results"].append(item)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
