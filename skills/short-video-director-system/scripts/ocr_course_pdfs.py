#!/usr/bin/env python3
"""OCR image-only course PDFs with macOS Vision without changing originals."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def extract_page_images(pdf: Path, temporary: Path) -> list[Path]:
    reader = PdfReader(str(pdf))
    outputs: list[Path] = []
    for page_index, page in enumerate(reader.pages, 1):
        images = sorted(page.images, key=lambda item: len(item.data), reverse=True)
        if not images:
            continue
        image = images[0]
        extension = Path(image.name).suffix or ".png"
        target = temporary / f"{pdf.stem}-p{page_index:03d}{extension}"
        target.write_bytes(image.data)
        outputs.append(target)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--vision-script", type=Path, required=True)
    parser.add_argument("--only-if-text-below", type=int, default=20)
    args = parser.parse_args()

    source = args.source.resolve()
    paths = [source] if source.is_file() else sorted(
        (path for path in source.rglob("*.pdf") if not path.name.startswith("._")),
        key=lambda item: str(item).casefold(),
    )
    base = source.parent if source.is_file() else source
    output_dir = args.output_dir.resolve()
    results: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="course-ocr-") as tmp:
        temporary = Path(tmp)
        image_to_pdf: dict[str, tuple[Path, int]] = {}
        for pdf in paths:
            reader = PdfReader(str(pdf))
            extracted = "".join((page.extract_text() or "") for page in reader.pages).strip()
            if len(extracted) >= args.only_if_text_below:
                results.append({"source": str(pdf), "status": "text_already_available"})
                continue
            images = extract_page_images(pdf, temporary)
            if not images:
                results.append({"source": str(pdf), "status": "no_page_image"})
                continue
            for page_number, image in enumerate(images, 1):
                image_to_pdf[str(image)] = (pdf, page_number)

        if image_to_pdf:
            command = ["/usr/bin/swift", str(args.vision_script.resolve()), *image_to_pdf]
            process = subprocess.run(command, capture_output=True, text=True, check=False)
            if process.returncode != 0:
                raise SystemExit(process.stderr.strip() or "Vision OCR failed")
            ocr_results = json.loads(process.stdout)
            grouped: dict[Path, list[tuple[int, str, str | None]]] = {}
            for item in ocr_results:
                pdf, page_number = image_to_pdf[item["path"]]
                grouped.setdefault(pdf, []).append((page_number, item.get("text", ""), item.get("error")))
            for pdf, pages in grouped.items():
                relative = pdf.relative_to(base)
                target = output_dir / relative.parent / f"{pdf.name}.ocr.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                lines = [
                    f"# {pdf.name}", "",
                    f"- source_path: `{pdf}`",
                    f"- source_sha256: `{sha256(pdf)}`",
                    f"- ocr_engine: `macOS Vision accurate zh-Hans,en-US`",
                    f"- ocr_at: `{datetime.now().astimezone().isoformat(timespec='seconds')}`", "",
                ]
                errors: list[str] = []
                character_count = 0
                for page_number, text, error in sorted(pages):
                    lines.extend((f"## 第 {page_number} 页", "", text.strip(), ""))
                    character_count += len(text.strip())
                    if error:
                        errors.append(f"page {page_number}: {error}")
                target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
                results.append({
                    "source": str(pdf), "output": str(target),
                    "status": "complete" if character_count >= 20 and not errors else "needs_review",
                    "characters": character_count, "errors": errors,
                })

    manifest = output_dir / "ocr-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for item in results:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"ok": True, "counts": counts, "manifest": str(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
