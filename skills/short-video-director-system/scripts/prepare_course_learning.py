#!/usr/bin/env python3
"""Prepare resumable, evidence-first local course learning batches.

This helper is intentionally not an automatic "learn everything" button.  It
can audit a local archive, select one representative per byte-identical video,
create transcripts/keyframes and add review drafts.  Only an SRC record that
passes transcript review, visual verification, deduplication and application
translation may later change a course to learned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPTS = Path(__file__).resolve().parent
AUDIT = SCRIPTS / "audit_course_archive.py"
TRANSCRIBE = SCRIPTS / "transcribe_course.py"
KEYFRAMES = SCRIPTS / "extract_course_keyframes.py"
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取审计 JSON：{path}（{exc}）") from exc
    if not isinstance(payload.get("records"), list):
        raise SystemExit(f"审计 JSON 缺少 records：{path}")
    return payload


def kb_revision(kb_root: Path) -> int:
    index = kb_root / "_index.md"
    if not index.is_file():
        return 0
    match = re.search(r"(?m)^kb_revision:\s*(\d+)\s*$", index.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else 0


def pending_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a deterministic one-file-per-content queue.

    The audit script has already inherited byte-identical learning evidence.
    Selecting the first relative path for each remaining SHA only prevents
    duplicate preparation; it never asserts semantic duplication.
    """
    selected: dict[str, dict[str, Any]] = {}
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            continue
        if raw.get("category") != "course_video":
            continue
        if raw.get("download_status") != "complete" or raw.get("learning_status") != "not_learned":
            continue
        digest = str(raw.get("sha256") or "")
        if len(digest) != 64:
            continue
        old = selected.get(digest)
        if old is None or str(raw.get("relative_path", "")).casefold() < str(old.get("relative_path", "")).casefold():
            selected[digest] = raw
    return sorted(selected.values(), key=lambda item: str(item.get("relative_path", "")).casefold())


def select_batch(records: list[dict[str, Any]], max_items: int, max_minutes: float | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seconds = 0.0
    for record in records:
        duration = float(record.get("duration_seconds") or 0)
        if result and max_minutes is not None and seconds + duration > max_minutes * 60:
            break
        result.append(record)
        seconds += duration
        if len(result) >= max_items:
            break
    return result


def safe_filename(value: str) -> str:
    compact = re.sub(r"[\\/:*?\"<>|]", "-", value).strip().replace("\n", " ")
    return compact[:120] or "untitled"


def existing_draft(inbox: Path, digest: str) -> Path | None:
    if not inbox.is_dir():
        return None
    for note in inbox.rglob("*.md"):
        try:
            if digest in note.read_text(encoding="utf-8"):
                return note
        except (OSError, UnicodeDecodeError):
            continue
    return None


def transcript_paths(transcript_root: Path, source: Path, archive_root: Path) -> tuple[Path, Path]:
    # transcribe_course.py receives one source file at a time, so its source
    # root is that file's parent and it writes directly beneath output-dir.
    # Callers should provide one course-version output folder per batch to
    # avoid same-title collisions across different courses.
    base = transcript_root / source.stem
    return base.parent / f"{base.name}.transcript.txt", base.parent / f"{base.name}.transcript.json"


def course_label(source: Path) -> str:
    for part, label in (("薛辉课程", "薛辉"), ("安先生", "安先生"), ("本地推", "本地推"), ("编导录播回放", "编导录播")):
        if part in source.parts:
            return label
    return "课程资料"


def process_running_for(source: Path) -> bool:
    """Avoid spawning a second local ASR worker for the same source."""
    probe = subprocess.run(["pgrep", "-af", "transcribe_course.py"], capture_output=True, text=True, check=False)
    return str(source) in probe.stdout


def render_draft(record: dict[str, Any], source: Path, revision: int, transcript_json: Path, keyframe_dir: Path) -> str:
    digest = str(record["sha256"])
    now = datetime.now().astimezone().date().isoformat()
    title = source.stem
    label = course_label(source)
    return f"""---
note_id: DRAFT-LOCAL-{digest[:12].upper()}
type: course_local_distillation
title: {json.dumps(title, ensure_ascii=False)}
status: evidence_pending_review
base_kb_revision: {revision}
created_at: {now}
source_authority: local_complete_original
tags: [{label}, 课程蒸馏, 本地原件, pending_evidence]
---

# {label}课程｜{title}

## 原件审计

- 完整本地原件：`{source}`
- 相对路径：`{record['relative_path']}`
- SHA-256：`{digest}`
- 文件大小：`{record.get('size_bytes', '')}` 字节；审计时长：`{record.get('duration', '')}`。
- 此草稿只来自完整原件审计；未使用 `._`、`.downloading` 或云端下载队列文件。

## 自动准备状态

- 时间轴转写预期位置：`{transcript_json}`。
- 关键帧预期目录：`{keyframe_dir}`。
- 自动化产物只作核读和回看片段的输入，不代表课程已学习、也不自动修改 canonical 内容或 Skill 版本。

## 正式入库门槛

1. 校验完整时间轴文字覆盖至原片结尾，逐段核读并隔离 ASR 异常。
2. 实看全片分位及定义、案例、公式、风险与总结的精确画面。
3. 与 canonical 知识卡/MOC 完成重复、补充、独立模型、冲突或过时判断。
4. 写入 SRC、必要的 KNW/MOC、应用转译、索引、更新日志；合并前复核 `kb_revision`。
5. 仅当该课程改变执行规则时更新 Skill；随后运行 `resolve_kb.py`，使 Skill 检查点与知识库版本一致。
"""


def write_drafts(batch: list[dict[str, Any]], archive_root: Path, transcript_root: Path, inbox: Path, revision: int) -> list[str]:
    created: list[str] = []
    inbox.mkdir(parents=True, exist_ok=True)
    for record in batch:
        digest = str(record["sha256"])
        old = existing_draft(inbox, digest)
        if old:
            created.append(f"复用草稿：{old}")
            continue
        source = archive_root / str(record["relative_path"])
        text_path, json_path = transcript_paths(transcript_root, source, archive_root)
        keyframe_dir = transcript_root / "关键帧待核验"
        draft = inbox / f"{datetime.now().date().isoformat()}-{course_label(source)}-{safe_filename(source.stem)}-{digest[:12]}.md"
        draft.write_text(render_draft(record, source, revision, json_path, keyframe_dir), encoding="utf-8")
        created.append(f"新建草稿：{draft}")
    return created


def write_manifest(path: Path, batch: list[dict[str, Any]], revision: int, archive_root: Path) -> None:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base_kb_revision": revision,
        "archive_root": str(archive_root),
        "status": "evidence_preparation_only",
        "records": batch,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_transcription(batch: list[dict[str, Any]], archive_root: Path, transcript_root: Path, ffmpeg: Path, model: str, chunk_seconds: int) -> list[str]:
    results: list[str] = []
    for record in batch:
        source = archive_root / str(record["relative_path"])
        transcript_txt, transcript_json = transcript_paths(transcript_root, source, archive_root)
        if transcript_json.is_file():
            try:
                prior = json.loads(transcript_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                prior = {}
            if prior.get("source_path") and prior["source_path"] != str(source.resolve()):
                results.append(f"转写目标名称冲突，保留原文件：{source.name}")
                continue
            if (prior.get("status") == "complete"
                    and prior.get("source_path") == str(source.resolve())
                    and prior.get("source_sha256") == record.get("sha256")
                    and transcript_txt.is_file() and transcript_txt.stat().st_size > 0):
                results.append(f"已有转写：{source.name}")
                continue
        if process_running_for(source):
            results.append(f"转写正在运行，未重复启动：{source.name}")
            continue
        command = [
            sys.executable, str(TRANSCRIBE), str(source), "--output-dir", str(transcript_root),
            "--model", model, "--language", "zh", "--chunk-seconds", str(chunk_seconds),
            "--ffmpeg", str(ffmpeg),
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode:
            results.append(f"转写失败：{source.name}（{completed.stderr.strip()[-300:]}）")
        else:
            results.append(f"转写完成：{source.name}")
    return results


def run_keyframes(manifest: Path, transcript_root: Path, ffmpeg: Path) -> str:
    keyframe_dir = transcript_root / "关键帧待核验"
    command = [
        sys.executable, str(KEYFRAMES), str(manifest), "--output-dir", str(keyframe_dir),
        "--ffmpeg", str(ffmpeg), "--positions", "0.05,0.25,0.5,0.75,0.95",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        return f"关键帧失败：{completed.stderr.strip()[-500:]}"
    return f"关键帧完成：{keyframe_dir}"


def command_audit(args: argparse.Namespace) -> None:
    command = [
        sys.executable, str(AUDIT), str(args.archive_root), "--transcript-root", str(args.transcript_root),
        "--kb-root", str(args.kb_root), "--output-prefix", str(args.output_prefix),
    ]
    if args.ffmpeg:
        command.extend(["--ffmpeg", str(args.ffmpeg)])
    subprocess.run(command, check=True)


def command_prepare(args: argparse.Namespace) -> None:
    payload = load_json(args.audit_json)
    all_pending = pending_records(payload)
    if args.include_regex:
        try:
            include = re.compile(args.include_regex)
        except re.error as exc:
            raise SystemExit(f"include-regex 无效：{exc}") from exc
        all_pending = [record for record in all_pending if include.search(str(record["relative_path"]))]
    if args.exclude_regex:
        try:
            exclude = re.compile(args.exclude_regex)
        except re.error as exc:
            raise SystemExit(f"exclude-regex 无效：{exc}") from exc
        all_pending = [record for record in all_pending if not exclude.search(str(record["relative_path"]))]
    batch = select_batch(all_pending, args.max_items, args.max_minutes)
    revision = kb_revision(args.kb_root)
    report: dict[str, Any] = {
        "ok": True,
        "all_pending_unique_videos": len(all_pending),
        "selected": len(batch),
        "base_kb_revision": revision,
        "manifest": str(args.manifest),
        "drafts": [],
        "mode": "evidence_preparation_only",
    }
    if args.dry_run:
        report["mode"] = "dry_run"
        report["selected_relative_paths"] = [str(record["relative_path"]) for record in batch]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    write_manifest(args.manifest, batch, revision, args.archive_root)
    report["drafts"] = write_drafts(batch, args.archive_root, args.transcript_root, args.inbox, revision)
    if args.transcribe:
        report["transcription"] = run_transcription(
            batch, args.archive_root, args.transcript_root, args.ffmpeg, args.model, args.chunk_seconds,
        )
    if args.keyframes:
        report["keyframes"] = run_keyframes(args.manifest, args.transcript_root, args.ffmpeg)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--transcript-root", type=Path, required=True)
    parser.add_argument("--kb-root", type=Path, required=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地课程扫描、证据准备与可恢复学习队列")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="扫描本地课程原件，生成审计 JSON/CSV/Markdown")
    add_common(audit)
    audit.add_argument("--output-prefix", type=Path, required=True)
    audit.add_argument("--ffmpeg", type=Path)
    audit.set_defaults(handler=command_audit)

    prepare = sub.add_parser("prepare", help="从审计 JSON 生成唯一学习批次与待核验草稿")
    add_common(prepare)
    prepare.add_argument("--audit-json", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--inbox", type=Path, required=True)
    prepare.add_argument("--max-items", type=int, default=1)
    prepare.add_argument("--max-minutes", type=float)
    prepare.add_argument("--include-regex", help="仅选择相对路径匹配此正则的未学习唯一内容")
    prepare.add_argument("--exclude-regex", help="排除相对路径匹配此正则的未学习唯一内容")
    prepare.add_argument("--dry-run", action="store_true", help="只显示将被选中的唯一内容，不写文件也不启动处理")
    prepare.add_argument("--transcribe", action="store_true", help="串行启动本地转写；不会重复运行中的转写")
    prepare.add_argument("--keyframes", action="store_true", help="为批次抽取五个全片分位关键帧")
    prepare.add_argument("--ffmpeg", type=Path, default=Path("/Applications/ChatCut.app/Contents/Resources/app.asar.unpacked/node_modules/ffmpeg-ffprobe-static/ffmpeg"))
    prepare.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    prepare.add_argument("--chunk-seconds", type=int, default=1800)
    prepare.set_defaults(handler=command_prepare)

    args = parser.parse_args()
    for name in ("archive_root", "transcript_root", "kb_root"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if hasattr(args, "ffmpeg") and args.ffmpeg:
        args.ffmpeg = args.ffmpeg.expanduser().resolve()
    args.handler(args)


if __name__ == "__main__":
    main()
