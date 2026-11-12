#!/usr/bin/env python3
"""Batch OCR course-note images with macOS Vision."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def natural_key(path: Path) -> list[object]:
    import re

    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--vision-script", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    if source.is_file():
        paths = [source]
        base = source.parent
    else:
        paths = sorted(
            (
                path for path in source.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and not path.name.startswith("._")
            ),
            key=natural_key,
        )
        base = source
    if not paths:
        raise SystemExit("No supported images found")

    process = subprocess.run(
        ["/usr/bin/swift", str(args.vision_script.resolve()), *(str(path) for path in paths)],
        capture_output=True, text=True, check=False,
    )
    if process.returncode != 0:
        raise SystemExit(process.stderr.strip() or "Vision OCR failed")
    by_path = {item["path"]: item for item in json.loads(process.stdout)}
    output_dir = args.output_dir.resolve()
    results: list[dict[str, object]] = []
    for path in paths:
        item = by_path.get(str(path), {})
        text = str(item.get("text", "")).strip()
        error = item.get("error")
        relative = path.relative_to(base)
        target = output_dir / relative.parent / f"{path.name}.ocr.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join([
            f"# {path.name}", "",
            f"- source_path: `{path}`",
            f"- source_sha256: `{sha256(path)}`",
            f"- ocr_engine: `macOS Vision accurate zh-Hans,en-US`",
            f"- ocr_at: `{datetime.now().astimezone().isoformat(timespec='seconds')}`", "",
            text, "",
        ]), encoding="utf-8")
        status = "complete" if len(text) >= 20 and not error else "needs_review"
        results.append({
            "source": str(path), "output": str(target), "status": status,
            "characters": len(text), "error": error,
        })

    manifest = output_dir / "image-ocr-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for item in results:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"ok": True, "counts": counts, "manifest": str(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
