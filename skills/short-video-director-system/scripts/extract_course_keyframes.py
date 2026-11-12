#!/usr/bin/env python3
"""Extract reproducible course-video keyframes from an audit manifest."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="按课程审计清单批量抽取关键画面")
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--positions", default="0.25,0.5,0.75")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        payload = json.loads(args.audit_json.expanduser().read_text(encoding="utf-8"))
        archive_root = Path(str(payload["archive_root"]))
        videos = [record for record in payload["records"] if record.get("category") == "course_video"]
        positions = [float(item) for item in args.positions.split(",")]
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if not positions or any(position <= 0 or position >= 1 for position in positions):
        parser.error("positions 必须是 0 到 1 之间的逗号分隔小数")

    start = max(0, args.start_index - 1)
    selected = videos[start : start + args.limit if args.limit else None]
    if not selected:
        parser.error("所选范围没有视频")
    ffmpeg = args.ffmpeg.expanduser().resolve()
    if not ffmpeg.is_file():
        parser.error(f"ffmpeg 不存在：{ffmpeg}")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for index, record in enumerate(selected, start=start + 1):
        source = archive_root / str(record["relative_path"])
        duration = float(record.get("duration_seconds") or 0)
        if duration <= 0 or not source.is_file():
            failures.append({"source": str(source), "error": "原件或有效时长缺失"})
            continue
        for position in positions:
            seconds = duration * position
            label = f"p{int(round(position * 100)):02d}"
            target = output_dir / f"{index:02d}_{source.stem}__{label}.jpg"
            if not target.is_file() or args.force:
                completed = subprocess.run(
                    [
                        str(ffmpeg),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{seconds:.3f}",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        "-y",
                        str(target),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode:
                    failures.append({"source": str(source), "error": completed.stderr.strip()})
                    continue
            manifest.append(
                {
                    "source_path": str(source.resolve()),
                    "source_sha256": record.get("sha256", ""),
                    "time_seconds": round(seconds, 3),
                    "position": position,
                    "frame_path": str(target),
                }
            )

    manifest_path = output_dir / "keyframes.manifest.json"
    manifest_payload = {
        "status": "complete" if not failures else "partial",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audit_json": str(args.audit_json.expanduser().resolve()),
        "frames": manifest,
        "failures": failures,
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": not failures,
                "videos": len(selected),
                "frames": len(manifest),
                "failures": failures,
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
