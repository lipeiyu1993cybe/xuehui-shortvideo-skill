#!/usr/bin/env python3
"""Extract searchable text from course documents into a derivative directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


SUPPORTED = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def detected_format(path: Path) -> tuple[str, str, str]:
    """Return effective extractor kind, integrity state and explanation.

    A number of course archives label legacy OLE ``.doc`` files as ``.docx``.
    Route by magic bytes rather than silently failing the entire batch.  OOXML
    packages are also checked for CRC errors before their text is trusted.
    """
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        return suffix, "corrupt", f"无法读取文件头：{exc}"
    is_zip = head.startswith(b"PK")
    is_ole = head.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    if suffix in {".docx", ".pptx", ".xlsx"}:
        if is_ole:
            legacy = {".docx": ".doc", ".pptx": ".ppt", ".xlsx": ".xls"}[suffix]
            return legacy, "unsupported_format", f"扩展名 {suffix} 实际为旧式 OLE {legacy}"
        if not is_zip:
            return suffix, "corrupt", f"{suffix} 不是 OOXML ZIP 容器"
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
            if bad:
                return suffix, "partial", f"OOXML 包存在 CRC 错误：{bad}"
        except (OSError, zipfile.BadZipFile) as exc:
            return suffix, "corrupt", f"OOXML 包无法校验：{exc}"
    if suffix == ".pdf" and not head.startswith(b"%PDF"):
        return suffix, "corrupt", "扩展名为 PDF 但文件头不是 %PDF"
    return suffix, "valid", ""


def extract_pdf(path: Path) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for index, page in enumerate(reader.pages, 1):
        parts.extend((f"## 第 {index} 页", "", page.extract_text() or "", ""))
    return "\n".join(parts).strip(), len(reader.pages)


def extract_docx(path: Path) -> tuple[str, int]:
    from docx import Document

    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table_index, table in enumerate(document.tables, 1):
        parts.extend(("", f"## 表格 {table_index}", ""))
        for row in table.rows:
            parts.append(" | ".join(cell.text.replace("\n", " ").strip() for cell in row.cells))
    return "\n".join(parts).strip(), len(document.paragraphs)


def extract_docx_xml(path: Path) -> tuple[str, int]:
    """Best-effort text extraction when embedded media has a bad CRC.

    Reading the XML parts directly avoids opening damaged image members.  The
    returned text is explicitly marked partial by the caller; visual assets
    still require repair and manual review before the source can be learned.
    """
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    parts: list[str] = []
    units = 0
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist()
            if name == "word/document.xml"
            or name.startswith("word/header") and name.endswith(".xml")
            or name.startswith("word/footer") and name.endswith(".xml")
        ]
        for name in sorted(names):
            root = ET.fromstring(archive.read(name))
            for paragraph in root.findall(".//w:p", ns):
                text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                if text:
                    parts.append(text)
                    units += 1
    return "\n".join(parts).strip(), units


def extract_doc(path: Path) -> tuple[str, int]:
    result = subprocess.run(
        ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
        capture_output=True,
        check=False,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 and not text:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return text, 0


def extract_legacy_office(path: Path, label: str) -> tuple[str, int]:
    """Use macOS' read-only converter for legacy Office containers."""
    result = subprocess.run(
        ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
        capture_output=True,
        check=False,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 and not text:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"textutil 无法转换 {label}")
    return text, 0


def extract_pptx(path: Path) -> tuple[str, int]:
    from pptx import Presentation

    deck = Presentation(str(path))
    parts: list[str] = []
    for slide_index, slide in enumerate(deck.slides, 1):
        parts.extend((f"## 幻灯片 {slide_index}", ""))
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and text.strip():
                parts.append(text.strip())
            if getattr(shape, "has_table", False):
                parts.append("")
                parts.append("### 表格")
                for row in shape.table.rows:
                    parts.append(" | ".join(cell.text.replace("\n", " ").strip() for cell in row.cells))
        try:
            notes = slide.notes_slide
            note_text = [
                getattr(shape, "text", "").strip()
                for shape in notes.shapes
                if getattr(shape, "text", "").strip()
            ]
            if note_text:
                parts.extend(("", "### 备注", *note_text))
        except Exception:
            # Some presentations omit a notes master.  That is not an
            # extraction failure and is recorded by the output units instead.
            pass
        parts.append("")
    return "\n".join(parts).strip(), len(deck.slides)


def extract_xlsx(path: Path) -> tuple[str, int]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), data_only=False, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.extend((f"## 工作表：{sheet.title}", ""))
        for row in sheet.iter_rows(values_only=True):
            values = ["" if value is None else str(value).replace("\n", " ") for value in row]
            if any(value.strip() for value in values):
                parts.append(" | ".join(values).rstrip(" |"))
        parts.append("")
    return "\n".join(parts).strip(), len(workbook.worksheets)


def extract_ppt(path: Path) -> tuple[str, int]:
    return extract_legacy_office(path, "PPT")


def extract_xls(path: Path) -> tuple[str, int]:
    return extract_legacy_office(path, "XLS")


def extract(path: Path, effective_suffix: str | None = None) -> tuple[str, int]:
    handlers = {
        ".pdf": extract_pdf,
        ".docx": extract_docx,
        ".doc": extract_doc,
        ".pptx": extract_pptx,
        ".ppt": extract_ppt,
        ".xlsx": extract_xlsx,
        ".xls": extract_xls,
    }
    suffix = effective_suffix or path.suffix.lower()
    return handlers[suffix](path)


def meaningful_characters(text: str) -> int:
    """Count extracted source text while ignoring generated page/slide headings."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return len("".join(lines).strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--exclude-token", action="append", default=[])
    args = parser.parse_args()

    source = args.source.resolve()
    if source.is_file():
        paths = [source]
        base = source.parent
    elif source.is_dir():
        paths = sorted(
            (
                path for path in source.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED and not path.name.startswith("._")
            ),
            key=lambda item: str(item).casefold(),
        )
        base = source
    else:
        raise SystemExit(f"Source not found: {source}")

    output_dir = args.output_dir.resolve()
    results: list[dict[str, object]] = []
    for path in paths:
        relative = path.relative_to(base)
        if any(token in str(relative) for token in args.exclude_token):
            results.append({"source": str(path), "status": "excluded"})
            continue
        target = output_dir / relative.parent / f"{path.name}.extract.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        effective_suffix = path.suffix.lower()
        integrity_status = "valid"
        integrity_reason = ""
        try:
            effective_suffix, integrity_status, integrity_reason = detected_format(path)
            if integrity_status == "corrupt":
                raise RuntimeError(integrity_reason)
            # Handlers route by an effective suffix while still receiving the
            # original path.  No renamed or copied source is created.
            try:
                text, units = extract(path, effective_suffix)
            except Exception as exc:
                if effective_suffix == ".docx":
                    text, units = extract_docx_xml(path)
                    integrity_status = "partial"
                    integrity_reason = f"正文 XML 可读，但嵌入媒体/CRC 损坏：{exc}"
                else:
                    raise
            header = [
                f"# {path.name}",
                "",
                f"- source_path: `{path}`",
                f"- source_sha256: `{digest(path)}`",
                f"- extracted_at: `{datetime.now().astimezone().isoformat(timespec='seconds')}`",
                f"- detected_format: `{effective_suffix}`",
                f"- integrity_status: `{integrity_status}`",
                f"- integrity_note: {integrity_reason or '无'}",
                f"- source_units: `{units}`",
                "",
            ]
            target.write_text("\n".join(header) + text + "\n", encoding="utf-8")
            meaningful = meaningful_characters(text)
            results.append({
                "source": str(path),
                "output": str(target),
                "status": "complete" if meaningful >= 20 and integrity_status == "valid" else (
                    "partial" if meaningful >= 20 else "needs_ocr"
                ),
                "detected_format": effective_suffix,
                "integrity_status": integrity_status,
                "integrity_note": integrity_reason,
                "source_sha256": digest(path),
                "characters": len(text),
                "meaningful_characters": meaningful,
                "units": units,
            })
        except Exception as exc:  # report per-file errors without losing successful work
            results.append({
                "source": str(path),
                "status": "error",
                "error": str(exc),
                "source_sha256": digest(path),
                "detected_format": effective_suffix,
                "integrity_status": integrity_status if integrity_status != "valid" else "corrupt",
                "integrity_note": integrity_reason or str(exc),
            })

    manifest = output_dir / "extraction-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"ok": True, "counts": counts, "manifest": str(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
