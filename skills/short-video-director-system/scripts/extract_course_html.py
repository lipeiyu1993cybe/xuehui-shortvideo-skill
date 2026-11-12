#!/usr/bin/env python3
"""Extract visible text and dependency metadata from local HTML snapshots.

No external URL is fetched.  The output is evidence for human review and is
deliberately marked historical/unverified so it cannot be mistaken for a live
platform rule or current trend report.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self._in_title = False
        self._skip = 0
        self._link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._link = {"href": attrs_map.get("href", ""), "text": ""}
        if tag == "img":
            self.images.append({
                "src": attrs_map.get("src") or attrs_map.get("data-src", ""),
                "alt": attrs_map.get("alt", ""),
            })

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link is not None:
            self.links.append(self._link)
            self._link = None
        if tag in {"script", "style", "noscript", "template", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        self.text_parts.append(value)
        if self._link is not None:
            self._link["text"] = (self._link.get("text", "") + " " + value).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    parser = VisibleTextParser()
    parser.feed(raw)
    parser.close()
    # Keep links/images in their original form but expose absolute URLs when
    # the snapshot declares a base URL; this is metadata only, never a fetch.
    base_match = re.search(r"<base[^>]+href=[\"']([^\"']+)", raw, re.I)
    base = base_match.group(1) if base_match else ""
    for item in parser.links:
        item["resolved_href"] = urljoin(base, item.get("href", "")) if base else item.get("href", "")
    for item in parser.images:
        item["resolved_src"] = urljoin(base, item.get("src", "")) if base else item.get("src", "")
    dates = sorted(set(re.findall(r"(?:20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?|20\d{2}-\d{2}-\d{2})", raw)))
    external = [
        item.get("resolved_href", "") for item in parser.links
        if str(item.get("resolved_href", "")).lower().startswith(("http://", "https://"))
    ]
    external += [
        item.get("resolved_src", "") for item in parser.images
        if str(item.get("resolved_src", "")).lower().startswith(("http://", "https://"))
    ]
    return {
        "source_path": str(path),
        "source_sha256": sha256(path),
        "extracted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "title": " ".join(parser.title_parts).strip(),
        "dates": dates,
        "visible_text": "\n".join(parser.text_parts),
        "links": parser.links,
        "images": parser.images,
        "external_resources": sorted(set(external)),
        "external_dependency_status": "external_unverified" if external else "none",
        "historical_fact_boundary": "页面快照/历史资料；平台、热度、案例与日期需按当期权威来源复核",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    paths = [source] if source.is_file() else sorted(
        p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in {".html", ".htm"} and not p.name.startswith("._")
    )
    if not paths:
        raise SystemExit("未找到 HTML 文件")
    base = source.parent if source.is_file() else source
    output_dir = args.output_dir.expanduser().resolve()
    rows: list[dict[str, object]] = []
    for path in paths:
        relative = path.relative_to(base)
        target = output_dir / relative.parent / f"{path.name}.html.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            item = extract(path)
            target.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            text = str(item.get("visible_text") or "")
            rows.append({"source": str(path), "output": str(target), "status": "complete" if len(text) >= 20 else "needs_review", "characters": len(text), "links": len(item.get("links") or []), "images": len(item.get("images") or [])})
        except Exception as exc:
            rows.append({"source": str(path), "status": "error", "error": str(exc)})
    manifest = output_dir / "html-extraction-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "files": len(paths), "manifest": str(manifest), "counts": {status: sum(1 for row in rows if row.get("status") == status) for status in sorted({str(row.get("status")) for row in rows})}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
