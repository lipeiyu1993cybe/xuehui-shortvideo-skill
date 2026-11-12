#!/usr/bin/env python3
"""Audit a growing local course archive without changing source files.

The report separates physical files, unique byte-identical contents, completed
downloads, transcripts, and knowledge-base learning status.  It intentionally
does not infer that a discovered or downloaded file has been learned.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
HTML_EXTENSIONS = {".html", ".htm"}
TEXT_EXTENSIONS = {".txt", ".md", ".rtf"}
CONTENT_EXTENSIONS = MEDIA_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | HTML_EXTENSIONS | TEXT_EXTENSIONS
INCOMPLETE_SUFFIXES = {".downloading", ".baiduyun.downloading"}
EXCLUDED_TOP_LEVEL = {"02-转写与OCR", "03-批次清单与校验", "04-临时处理区"}
ASSET_ID_PREFIX = "ASSET-"
LEDGER_SCHEMA_VERSION = 3
SHA256_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.I)

# These are intentionally additive.  Older callers consume ``category``,
# ``mime_type`` and the original status columns; the ledger fields below give
# downstream queue builders one stable vocabulary without invalidating old
# audit JSON/CSV files.
LEDGER_FIELDS = [
    "asset_id", "relative_path", "course_bundle", "kind", "mime_guess",
    "size_bytes", "sha256", "alias_paths", "duplicate_group",
    "integrity_status", "download_status", "evidence_status",
    "learning_status", "source_ids", "artifact_paths", "blocked_reason",
    "kb_revision",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_incomplete(path: Path) -> bool:
    lower = path.name.lower()
    return any(lower.endswith(suffix) for suffix in INCOMPLETE_SUFFIXES)


def content_extension(path: Path) -> str:
    """Get the underlying extension when a downloader suffix is appended.

    ``foo.mp4.downloading`` should remain a video asset with an incomplete
    download status, rather than falling into the generic ``other`` bucket.
    """
    name = path.name.lower()
    for suffix in INCOMPLETE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Download clients may insert several state tokens (for example
    # ``.mp4.baiduyun.p.downloading``).  Prefer the last known content suffix
    # embedded in the stem instead of treating ``.p`` as the file type.
    known = sorted(CONTENT_EXTENSIONS, key=len, reverse=True)
    for extension in known:
        marker = name.rfind(extension)
        if marker >= 0 and (marker + len(extension) == len(name) or name[marker + len(extension)] == "."):
            return extension
    return Path(name).suffix.lower()


def exclusion_reason(path: Path, root: Path) -> str | None:
    """Explain why a filesystem entry is not part of the content audit.

    macOS creates AppleDouble sidecars (``._*``) and download clients may
    leave cache/state files in hidden directories.  They are useful for
    troubleshooting a download, but are not course content and must not
    silently inflate the learning denominator.
    """
    if not path.is_file():
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    if path.name.startswith("._"):
        return "appledouble_sidecar"
    if path.name == ".DS_Store":
        return "system_metadata"
    if any(part == ".accelerate" for part in relative.parts):
        return "download_cache"
    if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return "generated_audit_or_processing_output"
    if any(part.startswith(".") for part in relative.parts):
        return "hidden_system_or_download_metadata"
    return None


def source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or exclusion_reason(path, root) is not None:
            continue
        files.append(path)
    return sorted(files, key=lambda item: str(item).casefold())


def excluded_files(root: Path) -> list[dict[str, str]]:
    """Return excluded files for an auditable, non-content sidecar ledger."""
    result: list[dict[str, str]] = []
    for path in root.rglob("*"):
        reason = exclusion_reason(path, root)
        if reason is None:
            continue
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = str(path)
        result.append({"relative_path": relative, "exclusion_reason": reason})
    return sorted(result, key=lambda item: item["relative_path"].casefold())


def transcript_sources(transcript_root: Path) -> set[str]:
    sources: set[str] = set()
    if not transcript_root.is_dir():
        return sources
    for path in transcript_root.rglob("*.transcript.json"):
        if path.name.startswith("._"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        source = payload.get("source_path")
        txt_path = path.with_suffix(".txt")
        if isinstance(source, str) and txt_path.is_file():
            sources.add(str(Path(source).resolve()))
    return sources


def _artifact_bucket() -> dict[str, Any]:
    return {"paths": set(), "statuses": set(), "kinds": set(), "sources": set()}


def _add_artifact_bucket(
    index: dict[str, dict[str, dict[str, Any]]],
    *,
    digest: str = "",
    source_path: str = "",
    artifact_paths: Iterable[str] = (),
    status: str = "",
    kind: str = "",
) -> None:
    """Add evidence metadata keyed by SHA and/or exact source path."""
    digest = _valid_sha(digest)
    source_path = _normalise_path(source_path)
    paths = {str(item) for item in artifact_paths if isinstance(item, str) and item}
    if source_path and source_path in paths:
        paths.discard(source_path)
    targets: list[tuple[str, str]] = []
    if digest:
        targets.append(("by_sha", digest))
    if source_path:
        targets.append(("by_path", source_path))
    for namespace, key in targets:
        bucket = index.setdefault(namespace, {}).setdefault(key, _artifact_bucket())
        bucket["paths"].update(paths)
        if status:
            bucket["statuses"].add(status)
        if kind:
            bucket["kinds"].add(kind)
        if source_path:
            bucket["sources"].add(source_path)


def _path_values(value: object, base: Path) -> list[str]:
    """Extract likely artifact paths from manifest values."""
    result: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 4096:
            return result
        # Avoid treating free-form OCR/transcript text as a path.  Explicit
        # manifest fields generally contain an extension or an absolute path.
        if "/" not in text and "\\" not in text and not Path(text).suffix:
            return result
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        result.append(_normalise_path(str(candidate)))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_path_values(item, base))
    elif isinstance(value, dict):
        for item in value.values():
            result.extend(_path_values(item, base))
    return result


def artifact_index(transcript_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Index transcript/OCR/keyframe manifests by original SHA and path.

    Evidence files live outside the external drive under ``02-转写与OCR``.
    Most transcript JSON already carries ``source_sha256``; older extraction
    manifests carry only ``source`` and ``output``.  This index accepts both,
    so a moved or renamed archive alias can reuse evidence by SHA while a
    legacy record can still be matched by its exact source path.
    """
    index: dict[str, dict[str, dict[str, Any]]] = {"by_sha": {}, "by_path": {}}
    if not transcript_root.is_dir():
        return index
    manifest_suffixes = {".json"}
    for manifest in sorted(transcript_root.rglob("*"), key=lambda item: str(item).casefold()):
        if not manifest.is_file() or manifest.suffix.lower() not in manifest_suffixes:
            continue
        if manifest.name.startswith("._"):
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        def walk(node: object, inherited_sha: str = "", inherited_source: str = "", inherited_status: str = "") -> None:
            if isinstance(node, dict):
                own_sha = _valid_sha(node.get("source_sha256"))
                # A few PDF/page manifests use ``sha256`` for the original
                # source.  Accept it only when a source path is present; this
                # avoids indexing text/output hashes as media evidence.
                if not own_sha and (node.get("source_path") or node.get("source")):
                    own_sha = _valid_sha(node.get("sha256"))
                source_value = node.get("source_path") or node.get("source") or inherited_source
                own_source = _normalise_path(source_value)
                status = str(node.get("status") or inherited_status or "").strip()
                explicit_paths: list[str] = [str(manifest.resolve(strict=False))]
                for key in (
                    "output", "text_path", "artifact_path", "artifact", "path",
                    "file", "files", "frames", "keyframe_evidence", "outputs",
                ):
                    if key in node:
                        explicit_paths.extend(_path_values(node.get(key), manifest.parent))
                # The manifest itself and its sibling text are useful evidence
                # for transcript JSON even when no explicit output key exists.
                if manifest.name.endswith(".transcript.json"):
                    sibling = manifest.with_name(
                        manifest.name.replace(".transcript.json", ".transcript.txt")
                    )
                    if sibling.is_file():
                        explicit_paths.append(str(sibling.resolve(strict=False)))
                if own_sha or own_source:
                    # Test the filename-specific transcript marker first.
                    # The canonical evidence root itself is named
                    # ``转写与OCR``; checking the parent path before this would
                    # misclassify every transcript as OCR evidence.
                    if manifest.name.endswith(".transcript.json"):
                        kind = "transcript"
                    elif "ocr" in manifest.name.lower() or "ocr" in str(manifest.parent).lower():
                        kind = "ocr"
                    elif "keyframe" in str(manifest.parent).lower() or "frame" in manifest.name.lower():
                        kind = "keyframes"
                    elif "extract" in manifest.name.lower() or "extraction" in str(manifest.parent).lower():
                        kind = "extraction"
                    else:
                        kind = "manifest"
                    _add_artifact_bucket(
                        index,
                        digest=own_sha or inherited_sha,
                        source_path=own_source or inherited_source,
                        artifact_paths=explicit_paths,
                        status=status,
                        kind=kind,
                    )
                next_sha = own_sha or inherited_sha
                next_source = own_source or inherited_source
                for child in node.values():
                    walk(child, next_sha, next_source, status)
            elif isinstance(node, list):
                for child in node:
                    walk(child, inherited_sha, inherited_source, inherited_status)

        walk(payload)
    return index


def evidence_for_asset(
    path: Path,
    digest: str,
    artifacts: dict[str, dict[str, dict[str, Any]]],
    transcript_root: Path,
    kb_root: Path | None,
) -> tuple[str, list[str], str]:
    """Return ``(evidence_status, artifact_paths, detail)`` for an asset."""
    buckets: list[dict[str, Any]] = []
    if digest:
        bucket = artifacts.get("by_sha", {}).get(_valid_sha(digest))
        if bucket:
            buckets.append(bucket)
    path_bucket = artifacts.get("by_path", {}).get(_normalise_path(str(path)))
    if path_bucket and path_bucket not in buckets:
        buckets.append(path_bucket)
    if not buckets:
        return "none", [], ""
    statuses: set[str] = set()
    kinds: set[str] = set()
    paths: set[str] = set()
    for bucket in buckets:
        statuses.update(str(x) for x in bucket.get("statuses", set()) if x)
        kinds.update(str(x) for x in bucket.get("kinds", set()) if x)
        paths.update(str(x) for x in bucket.get("paths", set()) if x)
    # Keep only existing evidence paths where possible; stale references are
    # retained as a detail marker instead of being silently dropped.
    rendered = sorted(
        {
            _display_artifact_path(Path(item), kb_root, transcript_root)
            for item in paths
            if item
        },
        key=str.casefold,
    )
    complete = any(status.lower() in {"complete", "completed", "ok", "learned", "canonical"} for status in statuses)
    if "transcript" in kinds and "keyframes" in kinds:
        evidence_status = "transcript_and_keyframes" if complete else "transcript_and_keyframes_pending"
    elif "transcript" in kinds:
        evidence_status = "transcript_complete" if complete else "transcript_pending"
    elif "ocr" in kinds or "extraction" in kinds:
        evidence_status = "ocr_or_extraction_complete" if complete else "ocr_or_extraction_pending"
    elif "keyframes" in kinds:
        evidence_status = "keyframes_complete" if complete else "keyframes_pending"
    else:
        evidence_status = "artifact_manifest"
    detail_parts = []
    if statuses:
        detail_parts.append("status=" + ",".join(sorted(statuses)))
    if kinds:
        detail_parts.append("kind=" + ",".join(sorted(kinds)))
    return evidence_status, rendered, "; ".join(detail_parts)


def learned_sources(kb_root: Path | None, archive_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return exact archive files cited by stable source records.

    A transcript proves transcription only.  Learning requires a source record
    created after transcript review, visual verification, deduplication and
    application translation.  Only explicit primary-original SHA/path fields
    are considered; transcript/OCR/keyframe and supporting-material paths are
    evidence-only and cannot accidentally mark an entire course as learned.
    """
    learned: dict[str, str] = {}
    learned_hashes: dict[str, str] = {}
    if kb_root is None or not kb_root.is_dir():
        return learned, learned_hashes
    source_root = kb_root / "02-来源档案"
    if not source_root.is_dir():
        return learned, learned_hashes
    archive_prefix = str(archive_root.resolve()) + os.sep
    auditable_files = source_files(archive_root)
    by_name: dict[str, list[Path]] = defaultdict(list)
    for item in auditable_files:
        by_name[item.name].append(item)
    for note in source_root.glob("SRC-*.md"):
        try:
            content = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Only a source record that explicitly passed the human learning gate
        # can prove an asset was learned.  ``status: evidence`` or even a
        # canonical citation may still be a transcript/OCR-only source, so it
        # must not advance the queue without ``learning_status: learned``.
        status_match = re.search(r"(?m)^status:\s*([^\n#]+)", content)
        source_status = status_match.group(1).strip().strip('"\'').casefold() if status_match else ""
        learning_match = re.search(r"(?m)^learning_status:\s*([^\n#]+)", content)
        learning_status = learning_match.group(1).strip().strip('"\'').casefold() if learning_match else ""
        if source_status not in {"learned", "canonical"} or learning_status != "learned":
            continue
        source_id = note.stem.split("-", 2)[0] + "-" + note.stem.split("-", 2)[1]
        primary_hashes, primary_paths = _primary_source_references(content, archive_root)
        for digest in primary_hashes:
            learned_hashes[digest] = source_id
        for raw in primary_paths:
            raw_path = Path(raw)
            if raw_path.suffix.lower() not in CONTENT_EXTENSIONS:
                continue
            candidates: list[Path] = []
            if raw_path.is_absolute():
                candidates.append(raw_path)
            else:
                candidates.append(archive_root / "01-百度网盘原始课程" / raw_path)
                if len(by_name.get(raw_path.name, [])) == 1:
                    candidates.extend(by_name[raw_path.name])
            for candidate in candidates:
                try:
                    resolved = str(candidate.resolve())
                except OSError:
                    continue
                if resolved.startswith(archive_prefix) and candidate.is_file():
                    learned[resolved] = source_id
                    break
    return learned, learned_hashes


def _tool_path(explicit: Path | None, name: str) -> Path | None:
    """Resolve an explicitly supplied binary, then a safe PATH fallback."""
    if explicit:
        return explicit if explicit.is_file() else None
    found = shutil.which(name)
    return Path(found) if found else None


def file_probe(path: Path) -> tuple[str, str]:
    """Return ``(mime_type, file_description)`` without invoking a shell."""
    file_bin = shutil.which("file") or "/usr/bin/file"
    mime_result = subprocess.run(
        [file_bin, "--brief", "--mime-type", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    desc_result = subprocess.run(
        [file_bin, "--brief", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    mime = mime_result.stdout.strip() or mimetypes.guess_type(path.name)[0] or ""
    description = desc_result.stdout.strip()
    return mime, description


def ffprobe_details(path: Path, ffprobe: Path | None) -> dict[str, Any]:
    """Probe container, duration and stream presence in one ffprobe call.

    A few archive files carry an ``.mp4`` suffix while their actual container
    is MPEG-TS.  We trust the probed format instead of the suffix and expose
    audio/video presence so a video-only interview is not silently treated as
    a normal audiovisual evidence source.
    """
    binary = _tool_path(ffprobe, "ffprobe")
    if binary is None:
        return {
            "duration": None, "format_name": "", "status": "unavailable",
            "error": "ffprobe 不可用", "has_video": None, "has_audio": None,
            "stream_types": [],
        }
    result = subprocess.run(
        [
            str(binary), "-v", "error", "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        return {
            "duration": None, "format_name": "", "status": "failed",
            "error": detail[:500] or "ffprobe 返回非零状态",
            "has_video": None, "has_audio": None, "stream_types": [],
        }
    try:
        payload = json.loads(result.stdout)
        fmt = payload.get("format") or {}
        names = str(fmt.get("format_name") or "")
        raw_duration = fmt.get("duration")
        duration = float(raw_duration) if raw_duration not in (None, "", "N/A") else None
        streams = payload.get("streams") or []
        stream_types = [
            str(item.get("codec_type"))
            for item in streams
            if isinstance(item, dict) and item.get("codec_type")
        ]
        has_video = "video" in stream_types
        has_audio = "audio" in stream_types
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "duration": None, "format_name": "", "status": "failed",
            "error": f"ffprobe JSON 无法解析：{exc}",
            "has_video": None, "has_audio": None, "stream_types": [],
        }
    if duration is not None and duration >= 0:
        return {
            "duration": duration, "format_name": names, "status": "ok",
            "error": "", "has_video": has_video, "has_audio": has_audio,
            "stream_types": stream_types,
        }
    return {
        "duration": None, "format_name": names, "status": "partial",
        "error": "容器可识别但未提供时长", "has_video": has_video,
        "has_audio": has_audio, "stream_types": stream_types,
    }


def ffprobe_video(path: Path, ffprobe: Path | None) -> tuple[float | None, str, str, str]:
    """Backward-compatible duration/container probe tuple.

    The tuple is ``(duration, format_name, probe_status, failure_reason)``.
    """
    details = ffprobe_details(path, ffprobe)
    return (
        details.get("duration"),
        str(details.get("format_name") or ""),
        str(details.get("status") or ""),
        str(details.get("error") or ""),
    )


def duration_probe(
    path: Path,
    ffmpeg: Path | None,
    ffprobe: Path | None,
    *,
    use_ffprobe: bool = True,
) -> tuple[float | None, str, str]:
    """Find duration with ffprobe → ffmpeg → macOS metadata fallbacks."""
    probe_status = ""
    probe_error = ""
    if use_ffprobe:
        probed, _format_name, probe_status, probe_error = ffprobe_video(path, ffprobe)
        if probed is not None:
            return probed, "ffprobe", ""

    ffmpeg_bin = _tool_path(ffmpeg, "ffmpeg")
    if ffmpeg_bin is not None:
        result = subprocess.run(
            [str(ffmpeg_bin), "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds), "ffmpeg", ""

    # macOS ships mdls even when a separate FFmpeg binary is unavailable.
    # It preserves duration coverage for archive audits without making media
    # processing a hidden prerequisite.
    mdls = "/usr/bin/mdls"
    if Path(mdls).is_file():
        result = subprocess.run(
            [mdls, "-name", "kMDItemDurationSeconds", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"=\s*([0-9]+(?:\.[0-9]+)?)\s*$", result.stdout)
        if match:
            return float(match.group(1)), "mdls", ""
    return None, "none", probe_error or ("ffprobe " + probe_status if probe_status else "未找到时长")


def duration_seconds(path: Path, ffmpeg: Path | None, ffprobe: Path | None = None) -> float | None:
    """Backward-compatible duration helper used by external callers."""
    return duration_probe(path, ffmpeg, ffprobe)[0]


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def top_level(relative_path: Path) -> str:
    parts = relative_path.parts
    if parts and parts[0] == "01-百度网盘原始课程" and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else ""


def course_bundle(relative_path: Path) -> str:
    """Return a deterministic human-readable course bundle name.

    The archive has two layouts: standalone bundles at its root and a large
    ``安先生往期所有课`` container whose next directory is the actual course.
    Keeping the latter component (rather than the generic container name)
    makes numbering-gap reports and cross-run asset IDs useful.  No file is
    moved or renamed by this normalization.
    """
    parts = list(relative_path.parts)
    if parts and parts[0] == "01-百度网盘原始课程":
        parts = parts[1:]
    if not parts:
        return ""
    if parts[0] == "安先生往期所有课" and len(parts) > 1:
        return parts[1]
    if parts[0] == "安先生2026" and len(parts) > 1:
        return parts[1]
    return parts[0]


def kind_for_category(category: str) -> str:
    """Map legacy categories to a small, stable asset-kind vocabulary."""
    if category == "course_video":
        return "video"
    if category in {"course_document", "ancillary_document"}:
        return "document"
    if category in {"course_image", "ancillary_image"}:
        return "image"
    if category in {"course_html", "reference_html"}:
        return "html"
    if category == "course_note":
        return "note"
    return "other"


def asset_id_for(relative_path: str, digest: str) -> str:
    """Build a stable ID from content, with a path fallback for incomplete files."""
    if digest and SHA256_RE.fullmatch(digest):
        return f"{ASSET_ID_PREFIX}{digest[:16].lower()}"
    path_digest = hashlib.sha256(relative_path.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{ASSET_ID_PREFIX}PATH-{path_digest}"


def _normalise_path(raw: object) -> str:
    """Normalize a path-like value without resolving non-existent files."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(Path(raw).expanduser())


def _display_artifact_path(path: Path, kb_root: Path | None, transcript_root: Path) -> str:
    """Prefer KB-relative artifact links while retaining outside paths."""
    candidates: list[tuple[Path, str]] = [(transcript_root, "02-转写与OCR")]
    if kb_root is not None:
        candidates.insert(0, (kb_root, ""))
    for base, prefix in candidates:
        try:
            rel = path.resolve(strict=False).relative_to(base.resolve(strict=False))
        except (ValueError, OSError, RuntimeError):
            continue
        value = str(rel)
        return f"{prefix}/{value}" if prefix else value
    return str(path)


def _valid_sha(value: object) -> str:
    if isinstance(value, str) and SHA256_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return ""


_PRIMARY_SOURCE_MARKERS = (
    "source_sha256",
    "source_path",
    "原始文件",
    "t7",
    "完整本地原件",
    "复用既有完整",
    "复用的本地完整",
    "原件：",
    "原件:",
    "原件为",
    "原件大小",
    "文件大小",
)
_EVIDENCE_ONLY_MARKERS = (
    "转写",
    "文本 sha",
    "txt sha",
    "json sha",
    "ocr",
    "关键帧",
    "抽取",
    "提取",
    "manifest",
    "工件",
    "缓存",
    "支持材料",
    "辅助",
    "交叉核对",
    "仅作",
    "重复副本",
    "字节级重复",
)


def _frontmatter(content: str) -> str:
    """Return the YAML-like frontmatter block, if one is present."""
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", content, re.S)
    return match.group(1) if match else ""


def _primary_source_references(
    content: str,
    archive_root: Path,
) -> tuple[set[str], set[str]]:
    """Extract only primary original-file SHA/path references from a SRC.

    Source notes commonly mention transcript, OCR, keyframe and supporting
    document paths alongside the original.  Treating every hash or backtick
    path as an original makes a supporting note (for example a ``赠2`` DOCX)
    appear learned merely because a video SRC cites it.  This parser limits
    promotion candidates to explicit frontmatter source fields and lines that
    identify the original/T7 file.  Evidence-only paths remain available to
    the general artifact index, but cannot advance ``learning_status``.
    """
    hashes: set[str] = set()
    paths: set[str] = set()
    frontmatter = _frontmatter(content)

    # Explicit metadata is the strongest mapping.  Newer notes generally use
    # ``source_sha256``/``source_path`` while the asset-ledger migration also
    # emits ``source_asset_*`` and ``primary_*`` aliases.  Accept all of these
    # equivalent spellings here so an already-reviewed canonical SRC is not
    # silently downgraded merely because it predates the current schema.
    for line in frontmatter.splitlines():
        key_match = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)\s*$", line)
        if not key_match:
            continue
        key, value = key_match.group(1).casefold(), key_match.group(2)
        if key in {
            "source_sha256",
            "source_asset_sha256",
            "source_asset_sha",
            "primary_sha256",
            "original_sha256",
            "source_file_sha256",
        }:
            hashes.update(SHA256_RE.findall(value.lower()))
        elif key in {
            "source_path",
            "source_asset_path",
            "primary_asset_path",
            "original_path",
            "source_file",
        }:
            candidate = value.strip().strip("\"'")
            if Path(candidate).suffix.lower() in CONTENT_EXTENSIONS:
                paths.add(candidate)

    # A source bundle may declare multiple exact assets with an asset-id table
    # rather than one frontmatter SHA.  The full SHA on each row is still the
    # authoritative mapping; do not treat the short asset-id prefix as a hash.
    table_row = re.compile(
        r"\|\s*ASSET-[0-9a-f]{8,32}\s*\|[^|]+\|\s*(`?[0-9a-f]{64}`?)\s*\|",
        re.I,
    )
    archive_marker = str(archive_root.resolve(strict=False)).casefold().rstrip("/")
    in_original_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Keep the common ``原件与学习证据``/``原件与证据`` section active;
            # reset it when the note moves into course analysis or relations.
            heading = line.lstrip("#").strip().casefold()
            in_original_section = "原件" in heading or "证据" in heading
            continue

        row_match = table_row.search(line)
        if row_match:
            hashes.add(row_match.group(1).strip("`").lower())
            # The row's filename is useful only as a fallback when the exact
            # SHA cannot be found in a current ledger; hash matching remains
            # the learning proof, so no basename path is promoted here.
            continue

        lowered = line.casefold()
        has_primary_marker = any(marker in lowered for marker in _PRIMARY_SOURCE_MARKERS)
        has_archive_path = archive_marker in lowered and any(
            extension in lowered for extension in CONTENT_EXTENSIONS
        )
        # Some older document records list the absolute T7 path and SHA on a
        # bullet without the word ``原件``.  Accept that shape only inside the
        # original/evidence section, never arbitrary prose later in the note.
        if not (in_original_section and (has_primary_marker or has_archive_path)):
            continue
        # Evidence lines can contain the word ``完整`` while describing a
        # transcript/OCR artifact.  Exclusions win unless the line explicitly
        # names a T7/original file and also carries an exact source SHA.
        evidence_only = any(marker in lowered for marker in _EVIDENCE_ONLY_MARKERS)
        explicit_original = any(marker in lowered for marker in (
            "source_sha256", "source_path", "原始文件", "t7", "完整本地原件",
            "复用既有完整", "复用的本地完整", "原件：", "原件:", "原件为",
            "原件大小",
        ))
        line_hashes = SHA256_RE.findall(line.lower())
        if evidence_only and not (explicit_original and line_hashes):
            continue
        hashes.update(line_hashes)
        supporting_line = any(
            marker in lowered
            for marker in ("支持材料", "辅助", "交叉核对", "仅作", "重复副本", "字节级重复")
        )
        for tick in re.findall(r"`([^`\n]+)`", line):
            candidate = tick.strip()
            if Path(candidate).suffix.lower() not in CONTENT_EXTENSIONS:
                continue
            # Supporting/duplicate paths are useful evidence but are not a
            # primary mapping; path promotion would otherwise match an
            # unrelated basename.  A source hash is accepted only when the
            # same line also contains an explicit primary declaration.
            if supporting_line:
                continue
            # Never promote paths that clearly point at generated evidence.
            candidate_lower = candidate.casefold()
            if any(
                token in candidate_lower
                for token in ("02-转写与ocr", "ocr/", "关键帧", "keyframe", ".transcript.", ".extract.")
            ):
                continue
            paths.add(candidate)
    return hashes, paths


def learned_by_existing_evidence(
    path: Path,
    file_digest: str,
    learned_paths: dict[str, str],
    learned_hashes: dict[str, str],
) -> tuple[bool, str]:
    """Return learning status proved by an explicit stable source record.

    ``learned_paths`` and ``learned_hashes`` are built only from source notes
    that explicitly declare ``learning_status: learned``.  Do not add course
    directory, filename or media-type fallbacks here: finding a familiar
    bundle (or previously generated evidence) does not prove that this exact
    asset passed the human review and application-translation gates.
    """
    resolved = str(path.resolve())
    if resolved in learned_paths:
        return True, f"稳定来源档案 {learned_paths[resolved]} 已覆盖"
    if file_digest and file_digest in learned_hashes:
        return True, f"文件 SHA-256 由稳定来源档案 {learned_hashes[file_digest]} 覆盖"
    return False, ""


def classify(path: Path) -> str:
    extension = content_extension(path)
    text = str(path)
    if extension in MEDIA_EXTENSIONS:
        return "course_video"
    if extension in DOCUMENT_EXTENSIONS:
        if any(token in text for token in ("简历", "工具包", "礼包", "运营表", "SOP", "sop")):
            return "ancillary_document"
        return "course_document"
    if extension in IMAGE_EXTENSIONS:
        course_image_tokens = ("学员高清手册", "AI智能体", "课程笔记", "电子书", "思维导图", "线下课")
        return "course_image" if any(token in text for token in course_image_tokens) else "ancillary_image"
    if extension in HTML_EXTENSIONS:
        # The archive contains both lesson-page snapshots and a dated hot-topic
        # reference set.  Keep both in the content denominator, while exposing
        # the latter as reference material for freshness safeguards.
        reference_tokens = ("今日热门选题", "热门选题", "选题参考", "热点", "直播资料")
        return "reference_html" if any(token in text for token in reference_tokens) else "course_html"
    if extension in TEXT_EXTENSIONS:
        return "course_note"
    return "other"


def integrity_probe(
    path: Path,
    category: str,
    mime_type: str,
    file_description: str,
    ffprobe_status: str = "",
) -> tuple[str, str]:
    """Classify basic file/container integrity without altering source bytes.

    This is deliberately conservative: a valid probe is enough for ``valid``;
    a malformed or mislabeled container is surfaced as ``corrupt`` or
    ``unsupported_format`` and remains in the queue for manual handling.
    """
    extension = path.suffix.lower()
    if category == "course_video":
        if ffprobe_status == "ok":
            return "valid", ""
        # A successful ffmpeg duration parse is a useful fallback even when a
        # nonstandard stream does not expose ffprobe's format JSON.
        if ffprobe_status in {"partial", "unavailable"}:
            return "partial", "视频容器未完成机器探测"
        return "corrupt", "视频容器无法由 ffprobe/ffmpeg 读取"

    if category in {"course_html", "reference_html"}:
        try:
            head = path.read_bytes()[:8192].lstrip().lower()
        except OSError as exc:
            return "corrupt", f"HTML 无法读取：{exc}"
        if b"<html" in head or b"<!doctype html" in head:
            return "valid", ""
        return "partial", "扩展名为 HTML 但未检测到 HTML 标记"

    if category in {"course_document", "ancillary_document"}:
        try:
            head = path.read_bytes()[:16]
        except OSError as exc:
            return "corrupt", f"文档无法读取：{exc}"
        is_zip = head.startswith(b"PK")
        is_ole = head.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
        if extension in {".docx", ".pptx", ".xlsx"} and not is_zip:
            if is_ole:
                return "unsupported_format", f"{extension} 扩展名实际为旧式 OLE 文档"
            return "corrupt", f"{extension} 不是可读取的 OOXML ZIP 容器"
        if extension == ".pdf" and not head.startswith(b"%PDF"):
            return "corrupt", "扩展名为 PDF 但文件头不是 %PDF"
        if extension == ".doc" and not is_ole and not is_zip:
            return "partial", "旧式 DOC 文件头无法确认"
        if extension == ".ppt" and not is_ole and not is_zip:
            return "partial", "旧式 PPT 文件头无法确认"
        # zipfile.testzip() catches CRC errors in OOXML packages while keeping
        # the source untouched.  It is intentionally limited to the package
        # header; large media payloads are not extracted during the audit.
        if is_zip and extension in {".docx", ".pptx", ".xlsx"}:
            try:
                with zipfile.ZipFile(path) as archive:
                    bad = archive.testzip()
                if bad:
                    return "partial", f"OOXML 包存在 CRC 错误：{bad}"
            except (OSError, zipfile.BadZipFile) as exc:
                return "corrupt", f"OOXML 包无法校验：{exc}"
        return "valid", ""

    if category in {"course_image", "ancillary_image"}:
        if mime_type.startswith("image/") or "image" in file_description.lower():
            return "valid", ""
        return "partial", "扩展名为图片但 MIME/文件探测不一致"

    if category == "course_note":
        return "valid", ""
    return "unsupported_format", "当前管道未定义该文件格式"


def html_dependency_status(path: Path, category: str) -> str:
    """Report whether an HTML snapshot depends on external resources.

    We do not fetch those resources during an audit.  A ``links_only`` marker
    makes that boundary explicit so historical snapshots are not mistaken for
    a self-contained course package or current platform facts.
    """
    if category not in {"course_html", "reference_html"}:
        return "none"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "missing_resources"
    external = re.findall(r"(?:src|href|data-src)\s*=\s*[\"'](https?://[^\"']+)", content, re.I)
    return "links_only" if external else "none"


def read_kb_revision(kb_root: Path | None) -> int:
    """Read the current canonical KB revision without requiring the KB."""
    if kb_root is None:
        return 0
    index = kb_root.resolve() / "_index.md"
    if not index.is_file():
        return 0
    try:
        content = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    match = re.search(r"(?m)^kb_revision:\s*(\d+)\s*$", content)
    return int(match.group(1)) if match else 0


def evidence_status(record: dict[str, object]) -> str:
    """Summarise what evidence exists without conflating it with learning."""
    if record.get("learning_status") == "learned":
        return "canonical"
    if record.get("integrity_status") in {"incomplete", "corrupt", "unsupported_format"}:
        return "blocked_integrity"
    if record.get("external_dependency_status") == "links_only":
        return "external_unverified"
    if record.get("transcript_status") == "complete":
        return "transcribed_pending_review"
    if record.get("category") in {"course_document", "ancillary_document", "course_image", "course_html", "reference_html", "course_note"}:
        return "extraction_pending_review"
    return "pending"


def write_csv(path: Path, records: list[dict[str, object]]) -> None:
    fields = [
        "asset_id", "relative_path", "top_level", "course_bundle", "kind",
        "category", "extension", "content_extension", "size_bytes",
        "mime_type", "mime_guess", "file_description", "container_probe",
        "duration_probe", "duration", "sha256", "duplicate_group",
        "canonical_representative", "alias_paths", "download_status",
        "integrity_status", "failure_reason", "transcript_status",
        "evidence_status", "learning_status", "learning_evidence", "source_ids",
        "artifact_paths", "artifact_detail", "blocked_reason", "external_dependency_status",
        "audio_status", "stream_types", "kb_revision",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        def csv_value(value: object) -> object:
            if isinstance(value, (list, tuple, set)):
                return ";".join(str(item) for item in value)
            return value
        writer.writerows(
            {key: csv_value(record.get(key, "")) for key in fields} for record in records
        )


def write_markdown(
    path: Path,
    root: Path,
    records: list[dict[str, object]],
    duplicate_groups: dict[str, list[dict[str, object]]],
    kb_revision: int,
    excluded: list[dict[str, str]] | None = None,
) -> None:
    complete = [record for record in records if record["download_status"] == "complete"]
    videos = [record for record in complete if record["category"] == "course_video"]
    unique_hashes = {str(record["sha256"]) for record in complete}
    unique_video_hashes = {str(record["sha256"]) for record in videos}
    learned_hashes = {
        str(record["sha256"]) for record in complete if record["learning_status"] == "learned"
    }
    learned_video_hashes = {
        str(record["sha256"]) for record in videos if record["learning_status"] == "learned"
    }
    category_counts = Counter(str(record.get("category") or "other") for record in records)
    integrity_counts = Counter(str(record.get("integrity_status") or "unknown") for record in records)
    top_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        group = top_counts[str(record["top_level"])]
        group["physical"] += 1
        if record["download_status"] == "complete":
            group["complete"] += 1
        if record["category"] == "course_video" and record["download_status"] == "complete":
            group["videos"] += 1
        if record["learning_status"] == "learned":
            group["learned_physical"] += 1
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "---",
        "type: course_asset_audit",
        "status: evidence",
        f"kb_revision: {kb_revision}",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# {root.name}｜课程资产审计",
        "",
        f"- 扫描时间：{generated_at}",
        f"- 课程根目录：`{root}`",
        f"- 完整物理文件：{len(complete)}",
        f"- 唯一内容：{len(unique_hashes)}",
        f"- 完整视频：{len(videos)} 个物理文件 / {len(unique_video_hashes)} 个唯一内容",
        f"- 当前盘内已学习匹配：{sum(1 for record in complete if record['learning_status'] == 'learned')} 个物理文件 / {len(learned_hashes)} 个唯一内容",
        f"- 已学习视频唯一内容：{len(learned_video_hashes)}",
        f"- 下载未完成文件：{sum(1 for r in records if r['download_status'] == 'incomplete')}",
        f"- 精确重复组：{len(duplicate_groups)}",
        f"- 技术元数据排除：{len(excluded or [])} 个（单列，不计入内容完成率）",
        "",
        "> 已下载、已转写、已学习、已入库是不同状态；本报告不从文件名推断已学习。",
        "",
        "## 内容类型与完整性",
        "",
        "| 内容类型 | 文件数 |",
        "|---|---:|",
    ]
    for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {category} | {count} |")
    lines.extend([
        "",
        "| 完整性状态 | 文件数 |",
        "|---|---:|",
    ])
    for status, count in sorted(integrity_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {status} | {count} |")
    lines.extend([
        "",
        "## 技术元数据排除",
        "",
    ])
    if not excluded:
        lines.append("未发现。")
    else:
        exclusion_counts = Counter(item["exclusion_reason"] for item in excluded)
        for reason, count in sorted(exclusion_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{reason}`：{count} 个")
        lines.extend(["", "抽样路径（最多 20 条）："])
        for item in excluded[:20]:
            lines.append(f"- `{item['relative_path']}`（{item['exclusion_reason']}）")
    lines.extend([
        "",
        "## 顶层课程覆盖",
        "",
        "| 顶层目录 | 物理文件 | 完整文件 | 完整视频 | 已学习物理文件 |",
        "|---|---:|---:|---:|---:|",
    ])
    for name in sorted(top_counts, key=str.casefold):
        count = top_counts[name]
        lines.append(
            f"| {name.replace('|', '\\|')} | {count['physical']} | {count['complete']} | "
            f"{count['videos']} | {count['learned_physical']} |"
        )
    lines.extend(["", "## 下载未完成文件", ""])
    incomplete_records = [
        record for record in records if record["download_status"] != "complete"
    ]
    if not incomplete_records:
        lines.append("未发现。")
    else:
        for record in incomplete_records:
            size = record["size_bytes"]
            size_text = f"{size} B" if isinstance(size, int) else "大小未知"
            lines.append(
                f"- `{record['relative_path']}`（{record['download_status']}，{size_text}）"
            )
    lines.extend(["", "## 字节级完全重复", ""])
    if not duplicate_groups:
        lines.append("未发现。")
    else:
        for group_id, items in duplicate_groups.items():
            lines.append(f"### {group_id} · {items[0]['sha256']}")
            lines.append("")
            for item in items:
                lines.append(f"- `{item['relative_path']}`")
            lines.append("")
    lines.extend([
        "## 完整视频学习缺口",
        "",
        "| 状态 | 时长 | 文件 |",
        "|---|---:|---|",
    ])
    canonical_seen: set[str] = set()
    for record in videos:
        digest = str(record["sha256"])
        if digest in canonical_seen:
            continue
        canonical_seen.add(digest)
        status = "已学习" if digest in learned_video_hashes else "待学习"
        relative = str(record["relative_path"]).replace("|", "\\|")
        lines.append(f"| {status} | {record['duration']} | `{relative}` |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_auxiliary_reports(
    prefix: Path,
    records: list[dict[str, object]],
    duplicate_groups: dict[str, list[dict[str, object]]],
) -> dict[str, str]:
    """Write machine-readable unique, duplicate and blocker ledgers.

    These sidecars keep the human-readable audit compact while giving batch
    workers deterministic inputs.  They are derived from the same snapshot and
    never change the source archive.
    """
    unique: dict[str, dict[str, object]] = {}
    for record in records:
        digest = str(record.get("sha256") or "")
        key = digest or str(record.get("asset_id") or record.get("relative_path") or "")
        if key in unique:
            continue
        unique[key] = {
            "asset_id": record.get("asset_id"),
            "sha256": digest or None,
            "canonical_representative": record.get("canonical_representative"),
            "alias_paths": record.get("alias_paths") or [record.get("relative_path")],
            "category": record.get("category"),
            "content_extension": record.get("content_extension") or record.get("extension"),
            "size_bytes": record.get("size_bytes"),
            "duration_seconds": record.get("duration_seconds"),
            "integrity_status": record.get("integrity_status"),
            "download_status": record.get("download_status"),
            "evidence_status": record.get("evidence_status"),
            "learning_status": record.get("learning_status"),
            "blocked_reason": record.get("blocked_reason") or record.get("failure_reason") or "",
            "kb_revision": record.get("kb_revision", 0),
        }
    unique_payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(unique),
        "records": sorted(unique.values(), key=lambda item: str(item.get("canonical_representative") or "").casefold()),
    }
    duplicates_payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(duplicate_groups),
        "groups": [
            {
                "duplicate_group": group_id,
                "sha256": str(items[0].get("sha256") or "") if items else "",
                "asset_id": items[0].get("asset_id") if items else None,
                "paths": [str(item.get("relative_path") or "") for item in items],
            }
            for group_id, items in duplicate_groups.items()
        ],
    }
    blockers = [
        {
            "asset_id": record.get("asset_id"),
            "relative_path": record.get("relative_path"),
            "sha256": record.get("sha256") or None,
            "category": record.get("category"),
            "download_status": record.get("download_status"),
            "integrity_status": record.get("integrity_status"),
            "evidence_status": record.get("evidence_status"),
            "external_dependency_status": record.get("external_dependency_status"),
            "blocked_reason": record.get("blocked_reason") or record.get("failure_reason") or "",
            "next_action": (
                "补齐下载后重新审计" if record.get("download_status") != "complete" else
                "修复/转换原件后重新提取证据" if record.get("integrity_status") != "valid" else
                "补正文/原页核读与应用转译"
            ),
        }
        for record in records
        if record.get("download_status") != "complete"
        or record.get("integrity_status") != "valid"
        or record.get("evidence_status") == "blocked_integrity"
    ]
    blockers_payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(blockers),
        "records": blockers,
    }
    outputs = {
        "unique": prefix.with_name(prefix.name + "-唯一内容.json"),
        "duplicates": prefix.with_name(prefix.name + "-重复组.json"),
        "blockers": prefix.with_name(prefix.name + "-阻塞清单.json"),
    }
    for key, payload in (
        ("unique", unique_payload),
        ("duplicates", duplicates_payload),
        ("blockers", blockers_payload),
    ):
        outputs[key].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: str(value) for key, value in outputs.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--transcript-root", type=Path, required=True)
    parser.add_argument(
        "--kb-root",
        type=Path,
        help="Obsidian knowledge-base root; exact files cited by SRC records count as learned",
    )
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument(
        "--ffprobe",
        type=Path,
        help="ffprobe binary; defaults to a sibling of --ffmpeg or PATH",
    )
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()

    root = args.archive_root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Archive root not found: {root}")
    paths = source_files(root)
    excluded = excluded_files(root)
    kb_revision = read_kb_revision(args.kb_root.resolve() if args.kb_root else None)
    ffprobe = args.ffprobe
    if ffprobe is None and args.ffmpeg:
        sibling = args.ffmpeg.with_name("ffprobe")
        if sibling.is_file():
            ffprobe = sibling
    transcript_root = args.transcript_root.resolve()
    transcripts = transcript_sources(transcript_root)
    # Index all existing extraction/transcript/keyframe manifests so a renamed
    # or moved alias can inherit evidence by SHA.  This is intentionally
    # separate from learning status: artifacts remain review inputs only.
    artifacts = artifact_index(transcript_root)
    learned_paths, learned_hashes = learned_sources(
        args.kb_root.resolve() if args.kb_root else None,
        root,
    )
    records: list[dict[str, object]] = []
    for path in paths:
        relative = path.relative_to(root)
        try:
            snapshot = path.stat()
        except FileNotFoundError:
            records.append({
                "relative_path": str(relative), "top_level": top_level(relative),
                "category": classify(path), "extension": path.suffix.lower(),
                "content_extension": content_extension(path),
                "asset_id": "",
                "course_bundle": top_level(relative), "kind": classify(path),
                "mime_guess": "", "canonical_representative": "", "alias_paths": [],
                "mime_type": "", "file_description": "", "container_probe": "",
                "duration_probe": "", "integrity_status": "corrupt",
                "failure_reason": "扫描期间下载客户端移除或改名",
                "size_bytes": "", "duration_seconds": None, "duration": "", "sha256": "",
                "duplicate_group": "", "download_status": "vanished_during_scan",
                "transcript_status": "missing", "learning_status": "not_learned",
                "learning_evidence": "扫描期间下载客户端移除或改名",
                "evidence_status": "blocked_integrity", "source_ids": [], "artifact_paths": [],
                "blocked_reason": "扫描期间下载客户端移除或改名",
                "external_dependency_status": "none",
                "audio_status": "not_applicable", "stream_types": [],
                "artifact_detail": "",
                "kb_revision": kb_revision,
            })
            continue
        incomplete = is_incomplete(path)
        try:
            file_digest = "" if incomplete else sha256(path)
        except FileNotFoundError:
            file_digest = ""
            incomplete = True
        learned, evidence = learned_by_existing_evidence(
            path,
            file_digest,
            learned_paths,
            learned_hashes,
        )
        category = classify(path)
        try:
            mime_type, file_description = file_probe(path)
        except (OSError, subprocess.SubprocessError):
            mime_type, file_description = "", ""
        container_name = ""
        duration_method = ""
        probe_error = ""
        ffprobe_status = ""
        duration = None
        audio_status = "not_applicable"
        stream_types: list[str] = []
        probe_details: dict[str, Any] = {}
        if category == "course_video" and not incomplete:
            probe_details = ffprobe_details(path, ffprobe)
            duration = probe_details.get("duration")
            container_name = str(probe_details.get("format_name") or "")
            ffprobe_status = str(probe_details.get("status") or "")
            probe_error = str(probe_details.get("error") or "")
            stream_types = [str(item) for item in (probe_details.get("stream_types") or [])]
            has_audio = probe_details.get("has_audio")
            audio_status = "present" if has_audio is True else "none" if has_audio is False else "unknown"
            if duration is not None:
                duration_method = "ffprobe"
            else:
                duration, duration_method, fallback_error = duration_probe(
                    path, args.ffmpeg, ffprobe, use_ffprobe=False,
                )
                probe_error = probe_error or fallback_error
        integrity_status, integrity_reason = integrity_probe(
            path,
            category,
            mime_type,
            file_description,
            ffprobe_status,
        )
        if incomplete:
            integrity_status, integrity_reason = "incomplete", "下载文件仍带有未完成后缀"
        failure_reason = integrity_reason or probe_error
        # A historical SRC can cover a byte-identical path while the current
        # integrity probe finds damaged embedded media or a mislabeled OLE
        # container.  Preserve that source evidence for auditability, but do
        # not let it promote a currently unreadable original to ``learned``.
        # The asset remains in the blocked queue until the original is repaired
        # and visual/page evidence is rechecked.
        if integrity_status != "valid" or incomplete:
            learned = False
            if evidence:
                evidence = f"{evidence}；当前原件完整性阻塞，需修复后复核"
        artifact_status, artifact_paths, artifact_detail = evidence_for_asset(
            path, file_digest, artifacts, transcript_root, args.kb_root.resolve() if args.kb_root else None,
        )
        source_ids = set(re.findall(r"SRC-\d+", evidence or ""))
        resolved_path = str(path.resolve())
        if resolved_path in learned_paths:
            source_ids.add(learned_paths[resolved_path])
        if file_digest and file_digest in learned_hashes:
            source_ids.add(learned_hashes[file_digest])
        external_status = html_dependency_status(path, category)
        if external_status == "links_only":
            # Keep the dependency boundary in the evidence field.  An
            # intentionally un-fetched external link is not an integrity or
            # download failure and therefore must not populate blocked_reason.
            artifact_status = "external_unverified"
        if learned:
            artifact_status = "canonical"
        elif integrity_status in {"incomplete", "corrupt", "unsupported_format"}:
            artifact_status = "blocked_integrity"
        records.append({
            "asset_id": "",
            "relative_path": str(relative),
            "top_level": top_level(relative),
            "course_bundle": top_level(relative),
            "kind": category,
            "category": category,
            "extension": path.suffix.lower(),
            "content_extension": content_extension(path),
            "mime_type": mime_type,
            "mime_guess": mime_type,
            "file_description": file_description,
            "container_probe": container_name,
            "duration_probe": duration_method,
            "size_bytes": snapshot.st_size,
            "duration_seconds": duration,
            "duration": format_duration(duration),
            "sha256": file_digest,
            "duplicate_group": "",
            "download_status": "incomplete" if incomplete else "complete",
            "integrity_status": integrity_status,
            "failure_reason": failure_reason,
            "canonical_representative": "",
            "alias_paths": [],
            "transcript_status": (
                "complete" if str(path.resolve()) in transcripts or "transcript" in artifact_detail else "missing"
            ) if category == "course_video" else "not_applicable",
            "evidence_status": artifact_status,
            "learning_status": "learned" if learned else "not_learned",
            "learning_evidence": evidence,
            "source_ids": sorted(source_ids),
            "artifact_paths": artifact_paths,
            "artifact_detail": artifact_detail,
            "blocked_reason": (
                failure_reason if integrity_status != "valid" else
                ""
            ),
            "external_dependency_status": external_status,
            "audio_status": audio_status,
            "stream_types": stream_types,
            "kb_revision": kb_revision,
        })

    by_hash: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        if record["sha256"]:
            by_hash[str(record["sha256"])].append(record)
    duplicate_groups: dict[str, list[dict[str, object]]] = {}
    duplicate_index = 0
    for digest, items in sorted(by_hash.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        if len(items) < 2:
            continue
        duplicate_index += 1
        group_id = f"DUP-{duplicate_index:04d}"
        duplicate_groups[group_id] = items
        inherited_learning = any(item["learning_status"] == "learned" for item in items)
        inherited_evidence = next(
            (str(item["learning_evidence"]) for item in items if item["learning_status"] == "learned"),
            "",
        )
        for item in items:
            item["duplicate_group"] = group_id
            # A valid learned representative may share bytes with an alias
            # whose current path is incomplete/corrupt.  Keep the alias
            # visible in the duplicate group, but never let inheritance
            # override that alias's integrity/download block.
            if inherited_learning and (
                item.get("download_status") == "complete"
                and item.get("integrity_status") == "valid"
            ):
                item["learning_status"] = "learned"
                if not item["learning_evidence"]:
                    item["learning_evidence"] = f"与已学习来源字节相同；{inherited_evidence}"

    # Attach deterministic asset IDs and alias paths after duplicate inheritance
    # so every physical file can be traced back to one byte-level content unit.
    for digest, items in by_hash.items():
        aliases = sorted({str(item["relative_path"]) for item in items}, key=str.casefold)
        representative = aliases[0] if aliases else ""
        asset_id = f"AST-{digest[:16].upper()}"
        for item in items:
            item["asset_id"] = asset_id
            item["canonical_representative"] = representative
            item["alias_paths"] = aliases
    for item in records:
        if not item.get("asset_id"):
            relative = str(item.get("relative_path") or "")
            # Incomplete/vanished files have no trustworthy content hash; the
            # path-derived ID is stable for queue bookkeeping but never treated
            # as a deduplication claim.
            item["asset_id"] = "AST-PATH-" + hashlib.sha1(relative.encode("utf-8")).hexdigest()[:16].upper()
            item["canonical_representative"] = relative
            item["alias_paths"] = [relative]
        item["evidence_status"] = evidence_status(item)
        if item.get("learning_status") == "learned" and not item.get("source_ids"):
            item["source_ids"] = sorted(set(re.findall(r"SRC-\d+", str(item.get("learning_evidence") or ""))))

    prefix = args.output_prefix.resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    csv_path = prefix.with_suffix(".csv")
    markdown_path = prefix.with_suffix(".md")
    auxiliary_paths = write_auxiliary_reports(prefix, records, duplicate_groups)
    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "archive_root": str(root),
        "kb_revision": kb_revision,
        "excluded_files": excluded,
        "auxiliary_reports": auxiliary_paths,
        "records": records,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, records)
    write_markdown(markdown_path, root, records, duplicate_groups, kb_revision, excluded)
    print(json.dumps({
        "ok": True,
        "files": len(paths),
        "excluded_files": len(excluded),
        "complete_files": sum(1 for record in records if record["download_status"] == "complete"),
        "duplicate_groups": len(duplicate_groups),
        "json": str(json_path),
        "csv": str(csv_path),
        "markdown": str(markdown_path),
        **auxiliary_paths,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
