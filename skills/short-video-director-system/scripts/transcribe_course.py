#!/usr/bin/env python3
"""Transcribe local course videos into resumable text and JSON evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}


def source_files(source: Path) -> tuple[Path, list[Path]]:
    if source.is_file():
        if source.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError(f"不支持的媒体格式：{source}")
        return source.parent, [source]
    if not source.is_dir():
        raise FileNotFoundError(f"课程目录不存在：{source}")
    files = [
        path
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and not path.name.startswith("._")
    ]
    return source, sorted(files, key=lambda path: str(path.relative_to(source)).casefold())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def output_paths(source: Path, source_root: Path, output_root: Path) -> tuple[Path, Path]:
    relative = source.relative_to(source_root)
    base = output_root / relative.parent / relative.stem
    return (
        base.parent / f"{base.name}.transcript.txt",
        base.parent / f"{base.name}.transcript.json",
    )


def repetition_flag(text: str) -> str | None:
    compact = re.sub(r"[\s,，。.!！?？;；:'\"“”‘’、-]", "", text).casefold()
    if len(compact) < 80:
        return None
    diversity = len(set(compact)) / len(compact)
    if diversity <= 0.08:
        return "高重复片段，疑似静音区转写幻觉"
    words = re.findall(r"[a-z]+|[\u4e00-\u9fff]", compact)
    if words and max((words.count(word) for word in set(words)), default=0) >= 20:
        return "单词或单字异常重复，疑似静音区转写幻觉"
    return None


def is_complete(json_path: Path, txt_path: Path, source: Path) -> bool:
    if not json_path.is_file() or not txt_path.is_file():
        return False
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload.get("source_path") == str(source.resolve()) and payload.get("status") == "complete"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def transcribe_with_chunks(
    source: Path,
    model: str,
    language: str,
    chunk_seconds: int,
    mlx_whisper: Any,
) -> dict[str, Any]:
    """Transcribe long media in bounded audio windows to avoid huge RAM/Metal allocations."""
    if chunk_seconds <= 0:
        return jsonable(mlx_whisper.transcribe(str(source), path_or_hf_repo=model, language=language, verbose=False))

    try:
        duration_result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, check=True,
        )
        duration = float(duration_result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        # Some local runtimes bundle ffmpeg without a separate ffprobe binary.
        # Its metadata line is sufficient for bounded chunking.
        inspect = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(source)], capture_output=True, text=True)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", inspect.stderr)
        if not match:
            raise RuntimeError(f"无法读取媒体时长：{source}")
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    merged: dict[str, Any] = {"segments": [], "text": "", "language": language}
    texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="course-transcribe-") as temporary:
        chunk_path = Path(temporary) / "chunk.wav"
        offset = 0.0
        while offset < duration:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(offset), "-t", str(chunk_seconds),
                 "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(chunk_path)],
                check=True,
            )
            # Fixed-temperature decoding keeps long-course processing bounded and
            # avoids expensive fallback passes on noisy or silent windows.
            result = jsonable(
                mlx_whisper.transcribe(
                    str(chunk_path),
                    path_or_hf_repo=model,
                    language=language,
                    verbose=False,
                    temperature=0,
                    condition_on_previous_text=False,
                )
            )
            merged["language"] = result.get("language", language)
            for segment in result.get("segments") or []:
                item = dict(segment)
                item["start"] = float(item.get("start", 0)) + offset
                item["end"] = float(item.get("end", item["start"])) + offset
                merged["segments"].append(item)
            text = str(result.get("text", "")).strip()
            if text:
                texts.append(text)
            offset += chunk_seconds
            print(json.dumps({"event": "chunk", "offset": offset, "duration": duration}, ensure_ascii=False), flush=True)
    merged["text"] = "\n".join(texts)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="把本地课程视频转写为带时间轴的证据文件")
    parser.add_argument("source", type=Path, help="单个视频或课程目录")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--model",
        default="mlx-community/whisper-large-v3-turbo",
        help="本地模型目录或 Hugging Face 仓库名",
    )
    parser.add_argument("--language", default="zh")
    parser.add_argument("--ffmpeg", type=Path, help="ffmpeg 可执行文件")
    parser.add_argument("--start-index", type=int, default=1, help="从排序后的第几项开始")
    parser.add_argument("--limit", type=int, help="本次最多处理多少项")
    parser.add_argument("--chunk-seconds", type=int, default=600, help="长视频分段读取秒数；0 表示整段读取")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.ffmpeg:
        ffmpeg = args.ffmpeg.expanduser().resolve()
        if not ffmpeg.is_file():
            parser.error(f"ffmpeg 不存在：{ffmpeg}")
        os.environ["PATH"] = str(ffmpeg.parent) + os.pathsep + os.environ.get("PATH", "")
    elif shutil.which("ffmpeg") is None:
        parser.error("未找到 ffmpeg；请通过 --ffmpeg 指定")

    try:
        source_root, files = source_files(args.source.expanduser().resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    start = max(0, args.start_index - 1)
    selected = files[start : start + args.limit if args.limit else None]
    if not selected:
        parser.error("所选范围没有可处理的视频")

    try:
        import mlx_whisper
    except ImportError as exc:
        print("缺少 mlx-whisper；请在独立环境中安装后重试", file=sys.stderr)
        return 2

    output_root = args.output_dir.expanduser().resolve()
    completed = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    for position, source in enumerate(selected, start=start + 1):
        txt_path, json_path = output_paths(source, source_root, output_root)
        if not args.force and is_complete(json_path, txt_path, source):
            skipped += 1
            print(json.dumps({"event": "skip", "index": position, "source": str(source)}, ensure_ascii=False), flush=True)
            continue

        print(json.dumps({"event": "start", "index": position, "source": str(source)}, ensure_ascii=False), flush=True)
        try:
            normalized = transcribe_with_chunks(
                source, args.model, args.language, args.chunk_seconds, mlx_whisper,
            )
            segments = normalized.get("segments") or []
            lines: list[str] = []
            review_flags: list[dict[str, object]] = []
            for segment in segments:
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                start_time = float(segment.get("start", 0))
                end_time = float(segment.get("end", start_time))
                reason = repetition_flag(text)
                if reason:
                    review_flags.append(
                        {
                            "start": start_time,
                            "end": end_time,
                            "reason": reason,
                            "text_preview": text[:160],
                        }
                    )
                    continue
                lines.append(f"[{timestamp(start_time)} --> {timestamp(end_time)}] {text}")
            if not lines and str(normalized.get("text", "")).strip():
                lines.append(str(normalized["text"]).strip())
            if not lines:
                raise ValueError("模型没有返回可用文本")

            duration = max((float(segment.get("end", 0)) for segment in segments), default=0.0)
            payload = {
                "status": "complete",
                "source_path": str(source.resolve()),
                "source_sha256": sha256(source),
                "model": args.model,
                "language": normalized.get("language", args.language),
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_seconds": duration,
                "text": normalized.get("text", ""),
                "segments": segments,
                "review_flags": review_flags,
            }
            atomic_write(txt_path, "\n".join(lines) + "\n")
            atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            completed += 1
            print(json.dumps({"event": "complete", "index": position, "json": str(json_path), "txt": str(txt_path)}, ensure_ascii=False), flush=True)
        except Exception as exc:  # keep the batch resumable after one bad file
            failures.append({"source": str(source), "error": f"{type(exc).__name__}: {exc}"})
            print(json.dumps({"event": "error", "index": position, "source": str(source), "error": failures[-1]["error"]}, ensure_ascii=False), file=sys.stderr, flush=True)

    summary = {
        "ok": not failures,
        "selected": len(selected),
        "completed": completed,
        "skipped": skipped,
        "failures": failures,
        "output_dir": str(output_root),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
