#!/usr/bin/env python3
"""Upgrade an existing audit snapshot without rereading large source bytes.

The full archive audit can take several minutes because it probes hundreds of
videos on an external disk.  This migration enriches a freshly generated
schema-v2 snapshot with deterministic asset IDs, aliases, queue sidecars and
underlying extensions for ``*.downloading`` files.  It intentionally verifies
only current size/mtime metadata; use ``audit_course_archive.py`` for a fresh
SHA/container scan when a source changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from audit_course_archive import (
    CONTENT_EXTENSIONS,
    classify,
    content_extension,
    evidence_status,
    html_dependency_status,
    integrity_probe,
    read_kb_revision,
    write_auxiliary_reports,
    write_csv,
    write_markdown,
)


def source_signature(root: Path, relative: str, expected_size: object) -> str:
    """Return a non-destructive signature warning for a changed path."""
    path = root / relative
    try:
        size = path.stat().st_size
    except OSError:
        return "source_missing_since_audit"
    try:
        expected = int(expected_size)
    except (TypeError, ValueError):
        return "size_unknown"
    return "" if size == expected else f"size_changed_since_audit:{expected}->{size}"


def course_number_gaps(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Report only explicit numbered 2.0/3.0/5.0 bundles; never infer others."""
    grouped: dict[str, set[int]] = defaultdict(set)
    incomplete_numbers: dict[str, set[int]] = defaultdict(set)
    maxima: dict[str, int] = {}
    for record in records:
        if record.get("category") != "course_video":
            # HTML reference pages often have their own day/entry numbers;
            # they are not lesson numbers and must not create false gaps.
            continue
        relative = str(record.get("relative_path") or "")
        top = str(record.get("top_level") or "")
        bundle_match = re.search(r"学院\s*([235])\.0[^/]*?(\d+)节", relative)
        filename = Path(relative).name
        number_match = re.match(r"(?:第\s*)?(\d{1,3})[_ .-]", filename)
        if not bundle_match:
            if "5.0" in top:
                if number_match:
                    version, limit = "5.0", 39
                else:
                    continue
            else:
                continue
        else:
            version, limit = f"{bundle_match.group(1)}.0", int(bundle_match.group(2))
        if number_match:
            number = int(number_match.group(1))
            grouped[version].add(number)
            if record.get("download_status") != "complete":
                incomplete_numbers[version].add(number)
            maxima[version] = limit
    result: list[dict[str, object]] = []
    for version, values in sorted(grouped.items()):
        limit = maxima.get(version, max(values) if values else 0)
        if limit <= 0:
            continue
        missing = sorted(set(range(1, limit + 1)) - values)
        result.append({
            "course_version": version,
            "expected_range": [1, limit],
            "observed_numbers": sorted(values),
            "missing_numbers": missing,
            "incomplete_numbers": sorted(incomplete_numbers.get(version, set())),
            "status": (
                "source_gap" if missing else
                "observed_range_with_incomplete_assets" if incomplete_numbers.get(version) else
                "observed_complete_range"
            ),
            "note": "编号缺口仅反映当前文件名，不推断云端/未下载内容。",
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--kb-root", type=Path)
    parser.add_argument("--verify-root", type=Path, help="仅核对当前 size，不重新计算 SHA")
    args = parser.parse_args()
    source = args.audit_json.expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload.get("records"), list):
        raise SystemExit("审计 JSON 缺少 records")
    root = Path(str(payload.get("archive_root") or "")).expanduser().resolve()
    records: list[dict[str, object]] = [dict(item) for item in payload["records"] if isinstance(item, dict)]
    kb_revision = read_kb_revision(args.kb_root.expanduser().resolve() if args.kb_root else None)

    for record in records:
        relative = str(record.get("relative_path") or "")
        path = Path(relative)
        # Preserve the physical suffix while exposing the content suffix for
        # incomplete downloader names such as ``.mp4.baiduyun.p.downloading``.
        record["content_extension"] = content_extension(path)
        if record.get("category") == "other" and content_extension(path) in CONTENT_EXTENSIONS:
            record["category"] = classify(path)
        record["course_bundle"] = record.get("top_level") or (path.parts[0] if path.parts else "")
        record["kind"] = record.get("category")
        record["mime_guess"] = record.get("mime_type") or ""
        record.setdefault("canonical_representative", "")
        record.setdefault("alias_paths", [])
        record.setdefault("source_ids", sorted(set(re.findall(r"SRC-\d+", str(record.get("learning_evidence") or "")))))
        record.setdefault("artifact_paths", [])
        record["kb_revision"] = kb_revision
        # Stable source records do not override a newly detected damaged
        # original.  Keep the citation as evidence, but force a re-review of
        # partial/corrupt/OLE and incomplete files.
        if record.get("integrity_status") != "valid" or record.get("download_status") != "complete":
            if record.get("learning_status") == "learned":
                record["learning_status"] = "not_learned"
                record["learning_evidence"] = "; ".join(filter(None, [
                    str(record.get("learning_evidence") or ""),
                    "当前原件完整性阻塞，需修复后复核",
                ]))
        if args.verify_root:
            warning = source_signature(args.verify_root.expanduser().resolve(), relative, record.get("size_bytes"))
            if warning:
                record["failure_reason"] = "; ".join(filter(None, [str(record.get("failure_reason") or ""), warning]))
                if record.get("download_status") == "complete":
                    record["integrity_status"] = "partial"
        if str(record.get("category")) in {"course_html", "reference_html"} and args.verify_root:
            try:
                record["external_dependency_status"] = html_dependency_status(args.verify_root / relative, str(record.get("category")))
            except OSError:
                record["external_dependency_status"] = "missing_resources"

    by_hash: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        digest = str(record.get("sha256") or "")
        if len(digest) == 64:
            by_hash[digest].append(record)
    duplicate_groups: dict[str, list[dict[str, object]]] = {}
    for index, (digest, items) in enumerate(sorted(by_hash.items(), key=lambda item: (-len(item[1]), item[0])), 1):
        aliases = sorted({str(item.get("relative_path") or "") for item in items}, key=str.casefold)
        asset_id = f"AST-{digest[:16].upper()}"
        group_id = f"DUP-{index:04d}" if len(items) > 1 else ""
        if group_id:
            duplicate_groups[group_id] = items
        for item in items:
            item["asset_id"] = asset_id
            item["canonical_representative"] = aliases[0] if aliases else ""
            item["alias_paths"] = aliases
            if group_id:
                item["duplicate_group"] = group_id
    for record in records:
        if not record.get("asset_id"):
            relative = str(record.get("relative_path") or "")
            record["asset_id"] = "AST-PATH-" + hashlib.sha1(relative.encode("utf-8")).hexdigest()[:16].upper()
            record["canonical_representative"] = relative
            record["alias_paths"] = [relative]
        record["evidence_status"] = evidence_status(record)
        record["blocked_reason"] = (
            str(record.get("failure_reason") or "")
            if record.get("integrity_status") != "valid" or record.get("download_status") != "complete"
            else ""
        )

    prefix = args.output_prefix.expanduser().resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    auxiliary = write_auxiliary_reports(prefix, records, duplicate_groups)
    upgraded = {
        "schema_version": 3,
        "upgraded_from": str(source),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "archive_root": str(root),
        "kb_revision": kb_revision,
        "excluded_files": payload.get("excluded_files", []),
        "auxiliary_reports": auxiliary,
        "records": records,
    }
    json_path = prefix.with_suffix(".json")
    csv_path = prefix.with_suffix(".csv")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(upgraded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, records)
    write_markdown(md_path, root, records, duplicate_groups, kb_revision, payload.get("excluded_files", []))
    gap_path = prefix.with_name(prefix.name + "-课程编号缺口.json")
    gap_path.write_text(json.dumps({"schema_version": 1, "records": course_number_gaps(records)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "records": len(records), "json": str(json_path), "csv": str(csv_path), "markdown": str(md_path), "course_gaps": str(gap_path), **auxiliary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
