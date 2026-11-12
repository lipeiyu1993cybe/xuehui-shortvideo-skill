#!/usr/bin/env python3
"""Build a stable, evidence-aware asset ledger from a course audit.

This is a read-only companion to ``audit_course_archive.py``.  The audit owns
filesystem discovery and SHA-256 calculation; this script normalizes its
records into a durable ledger, joins reusable transcript/OCR/keyframe evidence
by original SHA-256, and emits queue-friendly views without changing course
files or Obsidian canonical notes.

Typical use::

    python3 build_asset_ledger.py \
      --audit-json /path/to/课程资产审计-最新.json \
      --archive-root '/Volumes/T7 Shield/安先生' \
      --transcript-root '/.../短视频编导知识库/02-转写与OCR' \
      --kb-root '/.../短视频编导知识库' \
      --output-prefix '/.../03-批次清单与校验/安先生-20260831/安先生全量资产账本'

The resulting main JSON preserves the audit's ``records`` array, while the
``-unique`` / ``-duplicates`` / ``-course-gaps`` / ``-blocked`` sidecars make
the all-course queue independently inspectable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
SOURCE_ID_RE = re.compile(r"\bSRC-\d+\b")
MEDIA_CATEGORIES = {"course_video"}
DOCUMENT_CATEGORIES = {"course_document", "ancillary_document"}
IMAGE_CATEGORIES = {"course_image", "ancillary_image"}
HTML_CATEGORIES = {"course_html", "reference_html"}
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}
INCOMPLETE_MARKER_RE = re.compile(r"(?i)(\.mp4|\.mov|\.mkv|\.m4v|\.avi)(?:\.[^.]+)*\.downloading$")
GAP_ELIGIBLE_PATTERNS = (
    "安先生短视频学院1.0",
    "安先生短视频学院2.0",
    "安先生短视频学院3.0",
    "安先生短视频学院4.0",
    "安先生短视频学院5.0",
    "创始人ip营销大课",
    "第一人称创作社区课程",
)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def valid_sha(value: object) -> str:
    if isinstance(value, str) and SHA_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return ""


def normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(Path(value).expanduser())


def kind_for_category(category: str) -> str:
    if category in MEDIA_CATEGORIES:
        return "video"
    if category in DOCUMENT_CATEGORIES:
        return "document"
    if category in IMAGE_CATEGORIES:
        return "image"
    if category in HTML_CATEGORIES:
        return "html"
    if category == "course_note":
        return "note"
    return "other"


def bundle_for_relative(relative: str) -> str:
    parts = Path(relative).parts
    if not parts:
        return ""
    if parts[0] == "01-百度网盘原始课程" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[0] == "安先生往期所有课" and len(parts) > 1:
        return parts[1]
    if parts and parts[0] == "安先生2026" and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else ""


def asset_id(relative: str, digest: str) -> str:
    if digest:
        return f"ASSET-{digest[:16]}"
    fallback = hashlib.sha256(relative.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"ASSET-PATH-{fallback}"


def course_number(relative: str) -> int | None:
    """Read a likely lesson number without treating arbitrary years as one."""
    name = Path(relative).name
    patterns = (
        r"^\s*(\d{1,3})\s*[_ .、-]",
        r"^\s*第\s*(\d{1,3})\s*[节课集]",
    )
    for pattern in patterns:
        match = re.match(pattern, name)
        if match:
            return int(match.group(1))
    return None


def inferred_media_category(record: dict[str, Any]) -> str:
    """Treat downloader-suffixed media (``*.mp4.*.downloading``) as video."""
    category = str(record.get("category") or "other")
    if category == "course_video":
        return category
    relative = str(record.get("relative_path") or "")
    if INCOMPLETE_MARKER_RE.search(relative):
        return "course_video"
    return category


def expected_count(bundle: str, observed_numbers: Iterable[int]) -> int | None:
    """Derive only an explicitly labelled or well-known course count."""
    if not any(token in bundle for token in GAP_ELIGIBLE_PATTERNS):
        return None
    explicit = re.search(r"(\d{1,3})\s*节", bundle)
    if explicit:
        return int(explicit.group(1))
    if "短视频学院5.0" in bundle or "创始人ip营销大课" in bundle.lower():
        return 39
    # Do not invent a count for an unlabelled bundle.  A partial batch (for
    # example 034–035 only) must not be reported as if lessons 001–033 were
    # missing from the archive.  Known bundles use the explicit labels above.
    return None


def file_mime(path: Path) -> str:
    file_bin = shutil.which("file") or "/usr/bin/file"
    try:
        result = subprocess.run(
            [file_bin, "--brief", "--mime-type", str(path)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def probe_video(path: Path, ffprobe: Path | None) -> dict[str, Any]:
    if ffprobe is None or not ffprobe.is_file():
        found = shutil.which("ffprobe")
        ffprobe = Path(found) if found else None
    if ffprobe is None:
        return {"probe_status": "unavailable", "container_format": "", "has_audio": None, "has_video": None, "stream_types": []}
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        return {"probe_status": "failed", "container_format": "", "has_audio": None, "has_video": None, "stream_types": [], "error": detail[:500]}
    try:
        payload = json.loads(result.stdout)
        streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
        kinds = [str(item.get("codec_type")) for item in streams if item.get("codec_type")]
        fmt = payload.get("format") or {}
        raw_duration = fmt.get("duration")
        duration = float(raw_duration) if raw_duration not in (None, "", "N/A") else None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"probe_status": "failed", "container_format": "", "has_audio": None, "has_video": None, "stream_types": [], "error": f"ffprobe JSON 无法解析：{exc}"}
    return {
        "probe_status": "ok" if duration is not None else "partial",
        "container_format": str(fmt.get("format_name") or ""),
        "duration_seconds": duration,
        "has_audio": "audio" in kinds,
        "has_video": "video" in kinds,
        "stream_types": kinds,
    }


def document_format(record: dict[str, Any], path: Path | None) -> str:
    extension = str(record.get("content_extension") or record.get("extension") or "").lower()
    reason = str(record.get("failure_reason") or "").lower()
    description = str(record.get("file_description") or "").lower()
    if "ole" in reason or "composite document file" in description:
        return "ole_mislabeled" if extension.endswith("x") else "ole"
    if "crc" in reason:
        return "ooxml_crc_partial"
    if extension == ".pdf":
        return "pdf"
    if extension in {".docx", ".pptx", ".xlsx"}:
        return "ooxml_zip"
    if path and path.is_file():
        try:
            head = path.read_bytes()[:8]
        except OSError:
            return "unknown"
        if head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            return "ole_mislabeled" if extension.endswith("x") else "ole"
        if head.startswith(b"PK"):
            return "ooxml_zip"
    return "unknown"


def artifact_display(path: Path, kb_root: Path | None, transcript_root: Path) -> str:
    for base, prefix in ((kb_root, ""), (transcript_root, "02-转写与OCR")):
        if base is None:
            continue
        try:
            relative = path.resolve(strict=False).relative_to(base.resolve(strict=False))
        except (OSError, ValueError, RuntimeError):
            continue
        return f"{prefix}/{relative}" if prefix else str(relative)
    return str(path)


def extract_paths(value: object, base: Path) -> list[str]:
    if isinstance(value, str):
        if not value or len(value) > 4096:
            return []
        if "/" not in value and "\\" not in value and not Path(value).suffix:
            return []
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return [normalize_path(str(candidate))]
    if isinstance(value, list):
        return [result for item in value for result in extract_paths(item, base)]
    if isinstance(value, dict):
        return [result for item in value.values() for result in extract_paths(item, base)]
    return []


def evidence_index(transcript_root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """Return ``by_sha``, ``by_path`` and typed evidence kinds.

    It recognizes the local transcript schema plus generic extraction/OCR and
    keyframe manifests.  The archive is never rehashed: SHA joins use metadata
    already produced by the corresponding processing tool.
    """
    by_sha: dict[str, set[str]] = defaultdict(set)
    by_path: dict[str, set[str]] = defaultdict(set)
    kinds: dict[str, set[str]] = defaultdict(set)
    if not transcript_root.is_dir():
        return by_sha, by_path, kinds
    for manifest in transcript_root.rglob("*.json"):
        if manifest.name.startswith("._"):
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        def visit(node: object, inherited_sha: str = "", inherited_source: str = "") -> None:
            if isinstance(node, dict):
                source = normalize_path(node.get("source_path") or node.get("source") or inherited_source)
                digest = valid_sha(node.get("source_sha256"))
                if not digest and source:
                    digest = valid_sha(node.get("sha256"))
                status = str(node.get("status") or "").lower()
                paths = {str(manifest.resolve(strict=False))}
                for key in (
                    "output", "text_path", "frames", "pages", "files", "outputs",
                    "keyframe_evidence", "frame_path", "image_path", "page_image",
                ):
                    if key in node:
                        paths.update(extract_paths(node.get(key), manifest.parent))
                if manifest.name.endswith(".transcript.json"):
                    sibling = manifest.with_name(manifest.name.replace(".transcript.json", ".transcript.txt"))
                    if sibling.is_file():
                        paths.add(str(sibling.resolve(strict=False)))
                # The evidence root is itself named ``转写与OCR``.  Infer the
                # artifact from the manifest filename/explicit output first;
                # checking the whole path would mislabel every transcript as
                # OCR merely because the parent directory contains ``OCR``.
                # Older runs use ``transcript-manifest.json`` or a generic
                # manifest whose output ends in ``.transcript.txt``/``.ocr``;
                # recognise those forms too, while keeping parent-directory
                # checks as a final, narrow fallback.
                lower_name = manifest.name.casefold()
                explicit_lower = " ".join(str(value).casefold() for value in paths)
                transcript_hint = (
                    lower_name.endswith(".transcript.json")
                    or "transcript" in lower_name
                    or ".transcript." in explicit_lower
                    or "transcript.txt" in explicit_lower
                )
                ocr_hint = (
                    "ocr" in lower_name
                    or ".ocr." in explicit_lower
                    or ".ocr.txt" in explicit_lower
                    or any(part.casefold() in {"ocr", "ocr结果", "ocr输出"} for part in manifest.parts)
                )
                keyframe_hint = (
                    "keyframe" in lower_name
                    or "frame" in lower_name
                    or any("keyframe" in part.casefold() or part.casefold() in {"抽帧", "关键帧"} for part in manifest.parts)
                )
                extraction_hint = (
                    "extract" in lower_name
                    or "extraction" in lower_name
                    or ".extract." in explicit_lower
                    or any("extract" in part.casefold() or "正文" in part for part in manifest.parts)
                )
                if transcript_hint:
                    category = "transcript"
                elif ocr_hint:
                    category = "ocr"
                elif keyframe_hint:
                    category = "keyframes"
                elif extraction_hint:
                    category = "extraction"
                else:
                    category = "manifest"
                if status not in {"", "complete", "completed", "ok"}:
                    category += "_pending"
                for key, index in ((digest, by_sha), (source, by_path)):
                    if not key:
                        continue
                    index[key].update(paths)
                    kinds[key].add(category)
                for child in node.values():
                    visit(child, digest or inherited_sha, source or inherited_source)
            elif isinstance(node, list):
                for child in node:
                    visit(child, inherited_sha, inherited_source)

        visit(payload)
    return by_sha, by_path, kinds


def source_ids(kb_root: Path | None) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    by_sha: dict[str, set[str]] = defaultdict(set)
    by_path: dict[str, set[str]] = defaultdict(set)
    if kb_root is None:
        return by_sha, by_path
    source_root = kb_root / "02-来源档案"
    if not source_root.is_dir():
        return by_sha, by_path
    for note in source_root.glob("SRC-*.md"):
        try:
            content = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        status = re.search(r"(?m)^status:\s*([^\n#]+)", content)
        if not status or status.group(1).strip().strip("\"'") not in {"learned", "evidence", "canonical"}:
            continue
        source_id = "-".join(note.stem.split("-", 2)[:2])
        for digest in re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", content.lower()):
            by_sha[digest].add(source_id)
        for raw in re.findall(r"`([^`\n]+)`", content):
            if Path(raw).suffix:
                by_path[normalize_path(raw)].add(source_id)
    return by_sha, by_path


def source_learning_targets(
    kb_root: Path | None,
    archive_root: Path | None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """Index only explicit *primary* assets from learned source records.

    Source notes often cite supporting material (for example a course note
    used to cross-check a video).  Treating every backtick path in a learned
    note as a learned asset caused those supporting files to be promoted by
    accident.  Promotion is therefore driven by explicit ``source_asset_*``
    frontmatter, ``asset_ids`` lists, or a line labelled as an original/T7
    source.  Generic evidence and related-material citations remain evidence
    links only.
    """
    by_sha: dict[str, set[str]] = defaultdict(set)
    by_path: dict[str, set[str]] = defaultdict(set)
    by_asset: dict[str, set[str]] = defaultdict(set)
    if kb_root is None:
        return by_sha, by_path, by_asset
    source_root = kb_root / "02-来源档案"
    if not source_root.is_dir():
        return by_sha, by_path, by_asset
    hash_re = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.I)
    asset_re = re.compile(r"\bASSET-[0-9a-f]{16}\b", re.I)
    path_re = re.compile(r"`([^`\n]+)`")
    primary_label_re = re.compile(
        r"(?:原件|完整原件|本地完整原件|T7|来源文件|原始文件)", re.I
    )
    for note in source_root.glob("SRC-*.md"):
        try:
            content = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        status = re.search(r"(?m)^status:\s*([^\n#]+)", content)
        learning = re.search(r"(?m)^learning_status:\s*([^\n#]+)", content)
        if not status or status.group(1).strip().strip("\"'").casefold() not in {"canonical", "learned"}:
            continue
        # This index is used only to promote assets into the learned queue.
        # ``semantic_duplicate`` is a terminal *non-learned* disposition and
        # must be handled by an explicit, separate semantic-review record;
        # it is never equivalent to a completed learning gate here.
        if not learning or learning.group(1).strip().strip("\"'").casefold() != "learned":
            continue
        match = SOURCE_ID_RE.search(note.stem)
        if not match:
            continue
        source_id = match.group(0).upper()
        hashes: set[str] = set()
        paths: set[str] = set()
        assets: set[str] = set()

        # Explicit frontmatter is authoritative and unambiguous.
        for key in ("source_asset_sha256", "source_asset_sha", "primary_sha256"):
            field = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", content)
            if field:
                hashes.update(value.lower() for value in hash_re.findall(field.group(1)))
        for key in ("source_asset_path", "primary_asset_path"):
            field = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\"'\n]+)", content)
            if field:
                value = field.group(1).strip().strip('`')
                if value:
                    paths.add(value)
        # Aggregate source notes (such as a camera bundle) may enumerate IDs.
        for line in content.splitlines():
            if "asset_ids" in line or "source_asset_id" in line or line.lstrip().startswith("-"):
                assets.update(value.upper() for value in asset_re.findall(line))

        # Backward-compatible primary-line parsing for existing notes.  Do
        # not scan all backtick paths: supporting transcripts and cross-check
        # documents must not become learned originals.
        for line in content.splitlines():
            if not primary_label_re.search(line):
                continue
            # Exclude statements that explicitly describe a supporting note or
            # an evidence artifact rather than the source original.
            lowered = line.casefold()
            line_hashes = {value.lower() for value in hash_re.findall(line)}
            explicit_original = (
                any(token in lowered for token in ("t7", "原件", "完整原件", "本地完整原件"))
                and bool(line_hashes)
            )
            if (
                any(token in lowered for token in ("仅作", "交叉核对", "关键帧", "转写", "提取文本", "证据路径"))
                and not explicit_original
            ):
                continue
            hashes.update(line_hashes)
            for raw in path_re.findall(line):
                candidate = raw.strip()
                if Path(candidate).suffix.lower() in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".html", ".htm"}:
                    paths.add(candidate)

        for digest in hashes:
            by_sha[digest].add(source_id)
        for asset_id in assets:
            by_asset[asset_id].add(source_id)
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute() and archive_root is not None:
                candidate = archive_root / candidate
            by_path[normalize_path(str(candidate))].add(source_id)
    return by_sha, by_path, by_asset


def source_learning_ids(kb_root: Path | None) -> set[str]:
    """Return source IDs whose frontmatter explicitly says ``learned``.

    A citation alone is not enough to promote an asset: this separate
    allow-list requires the source author to record the completed learning
    gate (核读、原页/画面核验、去重和应用转译).  It keeps evidence-only
    sources from being silently treated as learned while allowing a fresh
    ledger rebuild to pick up sources created after the archive scan.
    """
    learned: set[str] = set()
    if kb_root is None:
        return learned
    source_root = kb_root / "02-来源档案"
    if not source_root.is_dir():
        return learned
    for note in source_root.glob("SRC-*.md"):
        try:
            content = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        status = re.search(r"(?m)^learning_status:\s*([^\n#]+)", content)
        if status and status.group(1).strip().strip("\"'").casefold() == "learned":
            match = SOURCE_ID_RE.search(note.stem)
            if match:
                learned.add(match.group(0))
    return learned


def status_for_evidence(record: dict[str, Any], artifact_kinds: set[str]) -> str:
    if record.get("learning_status") == "learned":
        return "canonical"
    if record.get("integrity_status") in {"incomplete", "corrupt", "unsupported_format"}:
        return "blocked_integrity"
    # A manifest can contain both complete and pending rows.  Keep the
    # complete marker when any complete artifact is present, but do not lose
    # the media-specific distinction when only ``*_pending`` is available.
    transcript_complete = "transcript" in artifact_kinds
    transcript_seen = transcript_complete or any(kind.startswith("transcript_") for kind in artifact_kinds)
    keyframes_complete = "keyframes" in artifact_kinds
    keyframes_seen = keyframes_complete or any(kind.startswith("keyframes_") for kind in artifact_kinds)
    extraction_complete = "extraction" in artifact_kinds or "ocr" in artifact_kinds
    extraction_seen = extraction_complete or any(
        kind.startswith("extraction_") or kind.startswith("ocr_") for kind in artifact_kinds
    )
    if transcript_seen and keyframes_seen:
        return "transcript_and_keyframes" if transcript_complete and keyframes_complete else "transcript_and_keyframes_pending"
    if transcript_seen:
        return "transcript_complete" if transcript_complete else "transcript_pending"
    if extraction_seen:
        return "ocr_or_extraction_complete" if extraction_complete else "ocr_or_extraction_pending"
    if keyframes_seen:
        return "keyframes_complete" if keyframes_complete else "keyframes_pending"
    if record.get("external_dependency_status") == "links_only":
        return "external_unverified"
    return "pending"


def blocking_reason(record: dict[str, Any]) -> str:
    reasons: list[str] = []
    if record.get("download_status") != "complete":
        reasons.append("download_incomplete")
    integrity = str(record.get("integrity_status") or "")
    if integrity in {"corrupt", "unsupported_format", "incomplete"}:
        reasons.append(f"integrity_{integrity}")
    elif integrity == "partial":
        reasons.append("integrity_partial")
    if record.get("kind") == "video" and record.get("has_video") is False:
        reasons.append("no_video_stream")
    # A video without an audio stream is still learnable within a declared
    # visual-only boundary.  Keep ``has_audio=false`` in the ledger rather than
    # treating it as an integrity blocker; the review draft must explicitly
    # state that no transcript is fabricated.
    # A local HTML snapshot remains learnable when it has outbound links or
    # image URLs that were deliberately not fetched.  ``links_only`` is an
    # evidence/applicability boundary (kept in ``external_dependency_status``
    # and surfaced as ``external_unverified``), not an integrity defect in the
    # local source.  Treating it as a blocker would silently remove every
    # readable historical HTML page from the normal learning queue.
    if record.get("failure_reason") and integrity != "valid":
        reasons.append(str(record["failure_reason"]))
    return "; ".join(dict.fromkeys(reasons))


def build_gaps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("kind") == "video":
            grouped[str(record.get("course_bundle") or "")].append(record)
    results: list[dict[str, Any]] = []
    for bundle, items in sorted(grouped.items(), key=lambda item: item[0].casefold()):
        numbered: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            number = item.get("course_number")
            if isinstance(number, int):
                numbered[number].append(item)
        if not numbered:
            continue
        target = expected_count(bundle, numbered)
        if target is None:
            continue
        present = set(numbered)
        incomplete = sorted(number for number, entries in numbered.items() if any(entry.get("download_status") != "complete" for entry in entries))
        missing = [number for number in range(1, target + 1) if number not in present]
        duplicates = {
            str(number): [str(entry.get("relative_path") or "") for entry in entries]
            for number, entries in numbered.items() if len(entries) > 1
        }
        if missing or incomplete or duplicates:
            results.append({
                "course_bundle": bundle,
                "expected_count": target,
                "observed_number_count": len(present),
                "missing_numbers": missing,
                "incomplete_numbers": incomplete,
                "duplicate_number_paths": duplicates,
                "note": "编号缺口只说明本地档案缺少或未完成，不等同于课程官方目录变更。",
            })
    return results


def csv_value(value: object) -> object:
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else value


def write_records_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "asset_id", "relative_path", "course_bundle", "kind", "mime_guess",
        "size_bytes", "sha256", "alias_paths", "duplicate_group", "integrity_status",
        "download_status", "evidence_status", "learning_status", "source_ids",
        "artifact_paths", "blocked_reason", "kb_revision", "course_number",
        "dedup_status",
        "container_format", "has_video", "has_audio", "document_format",
        "category", "duration", "failure_reason",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: csv_value(record.get(field, "")) for field in fields} for record in records)


def write_table_markdown(path: Path, title: str, records: list[dict[str, Any]]) -> None:
    lines = [f"# {title}", "", f"- 生成时间：{now()}", f"- 条目数：{len(records)}", ""]
    if not records:
        lines.append("无条目。")
    else:
        lines.extend(["| 资产 | 类型 | 完整性 | 学习状态 | 阻塞原因 |", "|---|---|---|---|---|"])
        for record in records:
            asset = str(record.get("relative_path") or record.get("course_bundle") or "").replace("|", "\\|")
            kind = str(record.get("kind") or "")
            integrity = str(record.get("integrity_status") or "")
            learning = str(record.get("learning_status") or "")
            block = str(record.get("blocked_reason") or "").replace("|", "\\|")
            lines.append(f"| `{asset}` | {kind} | {integrity} | {learning} | {block} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_payload_set(prefix: Path, suffix: str, title: str, payload: Any, records: list[dict[str, Any]]) -> dict[str, str]:
    base = prefix.with_name(prefix.name + suffix)
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_records_csv(csv_path, records)
    write_table_markdown(md_path, title, records)
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def load_records(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取审计 JSON：{path}（{exc}）") from exc
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("审计 JSON 缺少 records 数组")
    return payload, [dict(item) for item in records if isinstance(item, dict)]


def archive_content_count(root: Path) -> int:
    """Count auditable source files for partial-audit detection (no hashing)."""
    excluded_top = {"02-转写与OCR", "03-批次清单与校验", "04-临时处理区"}
    incomplete_suffixes = (".downloading", ".baiduyun.downloading")
    media_ext = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}
    doc_ext = {".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx"}
    image_ext = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
    html_ext = {".html", ".htm"}
    text_ext = {".txt", ".md", ".rtf"}
    exts = media_ext | doc_ext | image_ext | html_ext | text_ext
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("._") or path.name == ".DS_Store":
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or (rel.parts and rel.parts[0] in excluded_top):
            continue
        name = path.name.lower()
        # Downloader variants include ``.baiduyun.p.downloading``; strip the
        # complete marker chain so the underlying media extension is counted.
        marker = INCOMPLETE_MARKER_RE.search(name)
        if marker:
            name = name[: marker.end(1)]
        else:
            for suffix in incomplete_suffixes:
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
        if Path(name).suffix in exts:
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="从课程审计构建可恢复的统一资产账本")
    parser.add_argument("--audit-json", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, help="用于复核 MIME、容器和音轨；不写入原件")
    parser.add_argument("--transcript-root", type=Path, required=True)
    parser.add_argument("--kb-root", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument(
        "--skip-video-probe",
        action="store_true",
        help="复用审计中已有容器/音轨字段，不再次读取外置盘视频",
    )
    args = parser.parse_args()

    audit_payload, records = load_records(args.audit_json.expanduser().resolve())
    archive_root = args.archive_root.expanduser().resolve() if args.archive_root else None
    transcript_root = args.transcript_root.expanduser().resolve()
    kb_root = args.kb_root.expanduser().resolve() if args.kb_root else None
    output_prefix = args.output_prefix.expanduser().resolve()
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    kb_revision = int(audit_payload.get("kb_revision") or 0)
    if kb_root and (kb_root / "_index.md").is_file():
        match = re.search(r"(?m)^kb_revision:\s*(\d+)\s*$", (kb_root / "_index.md").read_text(encoding="utf-8"))
        if match:
            kb_revision = int(match.group(1))

    evidence_by_sha, evidence_by_path, evidence_kinds = evidence_index(transcript_root)
    source_by_sha, source_by_path = source_ids(kb_root)
    explicitly_learned_sources = source_learning_ids(kb_root)
    # ``source_ids`` intentionally indexes every citation in a stable source
    # note, including supporting transcripts/notes.  Promotion must instead
    # use the explicit primary-original mapping built by
    # ``source_learning_targets`` so a video SRC cannot accidentally mark a
    # cited DOCX or transcript as learned.
    learned_target_by_sha, learned_target_by_path, learned_target_by_asset = source_learning_targets(
        kb_root, archive_root
    )
    ffprobe = args.ffprobe.expanduser().resolve() if args.ffprobe else None

    for record in records:
        relative = str(record.get("relative_path") or "")
        digest = valid_sha(record.get("sha256"))
        category = inferred_media_category(record)
        record["category"] = category
        source_path = archive_root / relative if archive_root and relative else None
        normalized_source = normalize_path(str(source_path)) if source_path else ""
        record["relative_path"] = relative
        record["course_bundle"] = bundle_for_relative(relative)
        record["kind"] = kind_for_category(category)
        marker = INCOMPLETE_MARKER_RE.search(relative)
        if marker:
            record["content_extension"] = marker.group(1).lower()
        record["asset_id"] = asset_id(relative, digest)
        record["sha256"] = digest
        record["mime_guess"] = str(record.get("mime_guess") or record.get("mime_type") or "")
        record["kb_revision"] = kb_revision
        record["course_number"] = course_number(relative)
        # Batch manifests produced by older versions contain only a subset of
        # the audit columns.  Populate every stable ledger field explicitly so
        # downstream CSV/JSON consumers never need schema-specific branching.
        record.setdefault("size_bytes", None)
        record.setdefault("integrity_status", "unknown")
        record.setdefault("download_status", "unknown")
        record.setdefault("learning_status", "not_learned")
        record.setdefault("learning_evidence", "")
        record.setdefault("external_dependency_status", "none")
        record.setdefault("alias_paths", [relative] if relative else [])
        record.setdefault("duplicate_group", "")
        # ``dedup_status`` is deliberately separate from learning status:
        # semantic duplicates may still have distinct bytes and must retain
        # their own source/evidence records.  Hash grouping below upgrades
        # byte-identical aliases to ``byte_duplicate``.
        record.setdefault("dedup_status", "semantic_duplicate" if "semantic_duplicate" in str(record.get("learning_evidence") or "") else "unique")
        record.setdefault("artifact_paths", [])
        record.setdefault("source_ids", [])

        # Do not inherit a historical ``learned`` flag for a currently
        # damaged/incomplete original.  Its source ID remains attached, but
        # the asset must return to the repair/review queue.
        if record.get("integrity_status") != "valid" or record.get("download_status") != "complete":
            if record.get("learning_status") == "learned":
                record["learning_status"] = "not_learned"
                record["learning_evidence"] = "; ".join(filter(None, [
                    str(record.get("learning_evidence") or ""),
                    "当前原件完整性阻塞，需修复后复核",
                ]))

        if source_path and source_path.is_file() and not record["mime_guess"]:
            record["mime_guess"] = file_mime(source_path)
        if record["kind"] == "video":
            probe = (
                {}
                if args.skip_video_probe
                else probe_video(source_path, ffprobe) if source_path and source_path.is_file() else {}
            )
            record["container_format"] = str(probe.get("container_format") or record.get("container_probe") or "")
            record["has_video"] = probe.get("has_video") if probe else record.get("has_video")
            record["has_audio"] = probe.get("has_audio") if probe else record.get("has_audio")
            record["stream_types"] = probe.get("stream_types") if probe else (record.get("stream_types") or [])
            if probe.get("duration_seconds") is not None:
                record["duration_seconds"] = probe["duration_seconds"]
            if probe.get("probe_status") == "failed" and not record.get("failure_reason"):
                record["failure_reason"] = probe.get("error") or "ffprobe 失败"
        else:
            record["container_format"] = ""
            record["has_video"] = None
            record["has_audio"] = None
            record["stream_types"] = []
        record["document_format"] = document_format(record, source_path if source_path and source_path.is_file() else None) if record["kind"] == "document" else ""

        paths = set(evidence_by_sha.get(digest, set())) | set(evidence_by_path.get(normalized_source, set()))
        record["artifact_paths"] = sorted({artifact_display(Path(value), kb_root, transcript_root) for value in paths}, key=str.casefold)
        kinds = set(evidence_kinds.get(digest, set())) | set(evidence_kinds.get(normalized_source, set()))
        record["evidence_status"] = status_for_evidence(record, kinds)
        ids = set(record.get("source_ids") or [])
        ids.update(SOURCE_ID_RE.findall(str(record.get("learning_evidence") or "")))
        ids.update(source_by_sha.get(digest, set()))
        ids.update(source_by_path.get(normalized_source, set()))
        record["source_ids"] = sorted(ids)
        # A source record that explicitly carries ``learning_status: learned``
        # *and* names this exact primary original is the durable proof that
        # its cited asset passed human review.  Do not use a mere source-ID
        # citation: supporting documents, transcript paths and cross-check
        # material are deliberately excluded from the target index.
        target_ids: set[str] = set()
        target_ids.update(learned_target_by_sha.get(digest, set()))
        target_ids.update(learned_target_by_path.get(normalized_source, set()))
        # Aggregate records may name an ASSET id rather than repeating its
        # SHA.  Match case-insensitively and tolerate the historical AST-
        # prefix used by the audit script, but never match a short hash prefix
        # unless it is an exact asset id already present in this record.
        current_asset = str(record.get("asset_id") or "").upper()
        asset_variants = {current_asset}
        if current_asset.startswith("AST-"):
            asset_variants.add("ASSET-" + current_asset[4:])
        elif current_asset.startswith("ASSET-"):
            asset_variants.add("AST-" + current_asset[6:])
        for variant in asset_variants:
            target_ids.update(learned_target_by_asset.get(variant, set()))
        if (
            target_ids
            and record.get("integrity_status") == "valid"
            and record.get("download_status") == "complete"
        ):
            record["learning_status"] = "learned"
            record["learning_evidence"] = "; ".join(
                filter(None, [
                    str(record.get("learning_evidence") or ""),
                    "稳定来源档案已完成学习门槛（精确主资产匹配：" + ",".join(sorted(target_ids)) + "）",
                ])
            )
        elif record.get("learning_status") == "learned":
            # Never carry a historical/partial audit flag through a strict
            # rebuild when no exact primary target proves it.  Keep source IDs
            # and artifacts for review, but put the asset back in the queue.
            record["learning_status"] = "not_learned"
            record["learning_evidence"] = "; ".join(filter(None, [
                str(record.get("learning_evidence") or ""),
                "旧审计学习标记未找到精确主资产学习来源，待人工核读",
            ]))
        if record["source_ids"] and record.get("learning_status") != "learned":
            # Preserve the audit's strict learning rule: a stable source ID
            # alone does not upgrade an alias unless the audit already did so.
            record["evidence_status"] = record["evidence_status"] if record["evidence_status"] != "pending" else "source_record_pending_review"
        record["blocked_reason"] = blocking_reason(record)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        digest = valid_sha(record.get("sha256"))
        if digest:
            groups[digest].append(record)
    duplicate_groups: list[dict[str, Any]] = []
    unique_records: list[dict[str, Any]] = []
    for position, (digest, items) in enumerate(sorted(groups.items(), key=lambda item: min(str(row.get("relative_path") or "").casefold() for row in item[1])), start=1):
        items.sort(key=lambda item: str(item.get("relative_path") or "").casefold())
        aliases = [str(item.get("relative_path") or "") for item in items]
        representative = aliases[0]
        group_id = f"DUP-{position:04d}" if len(items) > 1 else ""
        learned = any(
            item.get("learning_status") == "learned"
            and item.get("integrity_status") == "valid"
            and item.get("download_status") == "complete"
            for item in items
        )
        inherited_ids = sorted({source for item in items for source in item.get("source_ids", [])})
        inherited_artifacts = sorted({artifact for item in items for artifact in item.get("artifact_paths", [])}, key=str.casefold)
        for item in items:
            item["alias_paths"] = aliases
            item["canonical_representative"] = representative
            item["duplicate_group"] = group_id
            item["dedup_status"] = "byte_duplicate" if group_id else str(item.get("dedup_status") or "unique")
            item["source_ids"] = inherited_ids
            item["artifact_paths"] = inherited_artifacts
            if learned and item.get("integrity_status") == "valid" and item.get("download_status") == "complete":
                item["learning_status"] = "learned"
                item["evidence_status"] = "canonical"
            elif item.get("integrity_status") != "valid" or item.get("download_status") != "complete":
                # A damaged/incomplete alias may share a historical digest,
                # but it still needs repair and re-review before it can be
                # called learned.
                item["learning_status"] = "not_learned"
        unique_records.append(items[0])
        if group_id:
            duplicate_groups.append({
                "duplicate_group": group_id,
                "asset_id": items[0]["asset_id"],
                "sha256": digest,
                "alias_paths": aliases,
                "learning_status": "learned" if learned else "not_learned",
                "source_ids": inherited_ids,
            })
    # Incomplete files have no SHA; each remains its own non-hashable asset.
    for record in records:
        if not valid_sha(record.get("sha256")):
            record["alias_paths"] = [str(record.get("relative_path") or "")]
            record["canonical_representative"] = str(record.get("relative_path") or "")
            record.setdefault("dedup_status", "unique_unhashable")
            unique_records.append(record)
    unique_records.sort(key=lambda item: str(item.get("relative_path") or "").casefold())
    # A sidecar built from a small batch must not turn unselected lessons into
    # false archive gaps.  When the archive root is available, compare the
    # audit row count with a cheap metadata-only file count; otherwise treat
    # the input as partial unless the caller explicitly supplies a full audit
    # marker in the source payload.
    expected_archive_rows = archive_content_count(archive_root) if archive_root else None
    full_coverage = (
        expected_archive_rows is not None and len(records) >= expected_archive_rows
    ) or bool(audit_payload.get("full_archive"))
    gaps = build_gaps(records) if full_coverage else []
    blocked = [record for record in records if record.get("blocked_reason")]

    complete_digests = {item.get("sha256") for item in records if item.get("download_status") == "complete" and item.get("sha256")}
    unique_kind_counts = {
        kind: sum(item.get("kind") == kind for item in unique_records)
        for kind in ("video", "document", "html", "image", "note", "other")
    }
    unique_status_counts: dict[str, int] = defaultdict(int)
    for item in unique_records:
        unique_status_counts[str(item.get("learning_status") or "not_learned")] += 1
    actionable_pending = sum(
        item.get("learning_status") not in {"learned", "semantic_duplicate", "user_excluded"}
        and not item.get("blocked_reason")
        and item.get("download_status") == "complete"
        and item.get("integrity_status") == "valid"
        for item in unique_records
    )
    summary = {
        "physical_assets": len(records),
        "unique_assets": len(unique_records),
        "unique_complete_assets": len(complete_digests),
        "unique_assets_including_incomplete": len(unique_records),
        "complete_assets": sum(item.get("download_status") == "complete" for item in records),
        # ``unique_videos`` includes the three hashless incomplete downloads,
        # matching the overall 975-content denominator.  Keep the complete
        # count separate so queue and repair reporting cannot conflate them.
        "unique_videos": unique_kind_counts["video"],
        "unique_complete_videos": len({
            item.get("sha256") for item in unique_records
            if item.get("kind") == "video"
            and item.get("sha256")
            and item.get("download_status") == "complete"
        }),
        "unique_documents": unique_kind_counts["document"],
        "unique_html": unique_kind_counts["html"],
        "unique_images": unique_kind_counts["image"],
        "unique_learning_status": dict(sorted(unique_status_counts.items())),
        "actionable_pending": actionable_pending,
        "duplicate_groups": len(duplicate_groups),
        "course_gap_groups": len(gaps),
        "blocked_assets": len(blocked),
        "audit_coverage": "full" if full_coverage else "partial",
    }
    payload = {
        "schema_version": 1,
        "type": "course_asset_ledger",
        "generated_at": now(),
        "source_audit": str(args.audit_json.expanduser().resolve()),
        "archive_root": str(archive_root) if archive_root else audit_payload.get("archive_root", ""),
        "kb_revision": kb_revision,
        "summary": summary,
        "records": records,
        "unique_assets": unique_records,
        "duplicate_groups": duplicate_groups,
        "course_number_gaps": gaps,
        "course_number_gap_note": "未对部分批次计算编号缺口；传入全盘审计 JSON 后再验收。" if not full_coverage else "",
        "blocked_records": blocked,
    }
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_records_csv(csv_path, records)
    write_table_markdown(md_path, "课程统一资产账本", records)
    sidecars = {
        "unique": write_payload_set(output_prefix, "-unique", "唯一内容清单", {"assets": unique_records}, unique_records),
        "duplicates": write_payload_set(output_prefix, "-duplicates", "字节级重复组", {"groups": duplicate_groups}, [dict(item, relative_path="；".join(item["alias_paths"]), kind="duplicate_group", integrity_status="", blocked_reason="") for item in duplicate_groups]),
        "course_gaps": write_payload_set(output_prefix, "-course-gaps", "课程编号缺口", {"gaps": gaps}, [dict(item, relative_path=item["course_bundle"], kind="course_number_gap", integrity_status="", learning_status="", blocked_reason="missing=" + ",".join(map(str, item["missing_numbers"]))) for item in gaps]),
        "blocked": write_payload_set(output_prefix, "-blocked", "阻塞清单", {"records": blocked}, blocked),
    }
    print(json.dumps({"ok": True, "summary": summary, "ledger": {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}, "sidecars": sidecars}, ensure_ascii=False))


if __name__ == "__main__":
    main()
