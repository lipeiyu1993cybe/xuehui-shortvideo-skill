#!/usr/bin/env python3
"""Build resumable, deduplicated learning queues from an asset ledger.

The queue is intentionally *non-destructive*: it creates symbolic links to
the authorised archive and writes manifests/CSV/Markdown next to the links.
No course media or documents are copied, renamed, or modified.  A queue is a
planning/evidence boundary only; it does not mark an asset as learned.

Example::

    python3 build_queue.py \
      --ledger /path/to/安先生全量资产账本-最终.json \
      --archive-root '/Volumes/T7 Shield/安先生' \
      --queue-dir /path/to/03-批次清单与校验/安先生-全量队列 \
      --max-hours 8

The default selection is one representative for each complete, not-learned,
unique SHA-256.  Incomplete/corrupt assets remain visible in the ledger but
are not placed in a learning queue.  Pass ``--include-blocked`` only when a
human explicitly wants a repair queue; those records are still labelled
blocked and are never treated as learnable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
HTML_EXTENSIONS = {".html", ".htm"}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def slug(value: str, limit: int = 80) -> str:
    """Make a stable, readable directory/file component for a queue link."""
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value).strip()
    value = re.sub(r"\s+", " ", value)
    return (value[:limit] or "asset").rstrip(" .")


def kind_of(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "").lower()
    if kind in {"video", "document", "image", "html", "note"}:
        return kind
    category = str(row.get("category") or "").lower()
    if "video" in category:
        return "video"
    if "document" in category:
        return "document"
    if "image" in category:
        return "image"
    if "html" in category:
        return "html"
    relative = str(row.get("relative_path") or "")
    suffix = Path(relative).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return "video"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in HTML_EXTENSIONS:
        return "html"
    return "other"


def priority(bundle: str, kind: str) -> tuple[int, str]:
    """Return a conservative processing priority and a human label.

    Priority is a queue hint, not a claim about course quality.  Newest
    5.0/paid materials come first, then recent paid courses, then the older
    versioned curricula and archives.  Documents are kept alongside their
    course rather than silently dropped.
    """
    text = bundle.casefold()
    if "5.0" in text or "创始人ip营销大课" in text:
        return (0, "P0-5.0")
    if any(token in text for token in ("全域增长", "内容时代", "强人设", "个人商业体", "个人品牌")):
        return (1, "P1-近期付费课")
    if "4.0" in text or "3.0" in text:
        return (2, "P2-版本课程")
    if "2.0" in text or "1.0" in text:
        return (3, "P3-旧版课程")
    if "第一人称" in text or "进阶" in text:
        return (3, "P3-社群与进阶")
    return (4, "P4-其他资料")


def duration_seconds(row: dict[str, Any]) -> float:
    for key in ("duration_seconds", "duration"):
        value = row.get(key)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return 0.0


def safe_resolve(root: Path, relative: str) -> Path:
    """Resolve a ledger path and reject path traversal outside archive root."""
    candidate = (root / relative).resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"资产路径越界：{relative}") from exc
    return candidate


def choose_unique(
    records: Iterable[dict[str, Any]],
    *,
    include: str | None,
    exclude: str | None,
    kinds: set[str],
    include_blocked: bool,
    blocked_only: bool,
    max_items: int | None,
    max_hours: float | None,
    start_index: int,
) -> list[dict[str, Any]]:
    include_re = re.compile(include) if include else None
    exclude_re = re.compile(exclude) if exclude else None

    # Keep one deterministic representative per SHA.  Hashless records (most
    # incomplete downloads) are retained only when explicitly requested and
    # are never merged with another path.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for original in records:
        row = dict(original)
        relative = str(row.get("relative_path") or "")
        kind = kind_of(row)
        if kind not in kinds:
            continue
        if include_re and not include_re.search(relative):
            continue
        if exclude_re and exclude_re.search(relative):
            continue
        learned = str(row.get("learning_status") or "not_learned").lower() == "learned"
        complete = str(row.get("download_status") or "").lower() == "complete"
        integrity = str(row.get("integrity_status") or "").lower()
        # A readable local HTML snapshot may reference images/links that we
        # intentionally did not fetch.  Keep that boundary visible as
        # ``external_unverified`` but do not treat it as an integrity blocker;
        # otherwise all historical HTML evidence disappears from the normal
        # queue.  This exception also makes queues built from older ledgers
        # (which encoded ``external_resources_not_captured`` in
        # ``blocked_reason``) behave consistently.
        external_only = (
            kind == "html"
            and complete
            and integrity not in {"incomplete", "corrupt", "unsupported_format", "partial"}
            and str(row.get("external_dependency_status") or "") == "links_only"
            and all(
                reason.strip() in {"", "external_resources_not_captured"}
                for reason in str(row.get("blocked_reason") or "").split(";")
            )
        )
        blocked = (bool(row.get("blocked_reason")) and not external_only) or not complete or integrity in {"incomplete", "corrupt", "unsupported_format", "partial"}
        if blocked_only and not blocked:
            continue
        if learned:
            continue
        if blocked and not include_blocked:
            continue
        digest = sha(row.get("sha256"))
        key = f"sha:{digest}" if digest else f"path:{relative}"
        row["kind"] = kind
        row["_blocked"] = blocked
        groups[key].append(row)

    selected: list[dict[str, Any]] = []
    for key, rows in groups.items():
        rows.sort(key=lambda item: str(item.get("relative_path") or "").casefold())
        representative = rows[0]
        aliases = [str(item.get("relative_path") or "") for item in rows if item.get("relative_path")]
        representative["alias_paths"] = sorted(set(aliases), key=str.casefold)
        representative["dedupe_key"] = key
        representative["duplicate_count"] = len(rows)
        selected.append(representative)

    selected.sort(
        key=lambda row: (
            priority(str(row.get("course_bundle") or ""), str(row.get("kind") or ""))[0],
            str(row.get("course_bundle") or "").casefold(),
            int(row.get("course_number") or 10**9),
            str(row.get("relative_path") or "").casefold(),
        )
    )
    # ``start_index`` is one-based in the CLI and applies after deterministic
    # priority/course sorting.  It makes interrupted batches resumable without
    # changing the ledger or relying on mutable queue directory order.
    offset = max(0, start_index - 1)
    selected = selected[offset:]
    bounded: list[dict[str, Any]] = []
    seconds = 0.0
    for row in selected:
        row_seconds = duration_seconds(row)
        if max_hours is not None and bounded and seconds + row_seconds > max_hours * 3600:
            break
        bounded.append(row)
        seconds += row_seconds
        if max_items is not None and len(bounded) >= max_items:
            break
    return bounded


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "queue_index", "priority", "priority_label", "asset_id", "course_bundle",
        "course_number", "kind", "relative_path", "queue_path", "sha256",
        "duplicate_count", "alias_paths", "duration_seconds", "download_status",
        "integrity_status", "learning_status", "evidence_status", "source_ids",
        "artifact_paths", "blocked_reason", "dedupe_key",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = {}
            for field in fields:
                value = row.get(field, "")
                if isinstance(value, (list, tuple, dict, set)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                out[field] = value
            writer.writerow(out)


def write_markdown(path: Path, payload: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    summary = payload["summary"]
    lines = [
        "# 安先生全量学习队列",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 状态：`{payload['status']}`（仅规划/证据队列，不代表已学习）",
        f"- 来源账本：`{payload['ledger']}`",
        f"- 原件根目录：`{payload['archive_root']}`",
        f"- 唯一队列项：{summary['selected_items']}；预计视频时长：{summary['video_hours']:.2f} 小时",
        f"- 重复别名覆盖：{summary['physical_aliases']} 个路径；阻塞修复项：{summary['blocked_items']}",
        "",
        "> 队列目录中的文件是指向外置盘原件的软链接。请在完成完整转写、原画面/原页核验、去重、应用转译和 `SRC` 入库后，另行更新审计状态；本工具不会自动标记 `learned`。",
        "",
        "| # | 优先级 | 类型 | 课程 | 资产 | 时长 | 证据状态 |",
        "|---:|---|---|---|---|---:|---|",
    ]
    for row in rows:
        p, label = priority(str(row.get("course_bundle") or ""), str(row.get("kind") or ""))
        seconds = duration_seconds(row)
        mins = f"{seconds/60:.1f}m" if seconds else "—"
        block = str(row.get("blocked_reason") or "")
        evidence = str(row.get("evidence_status") or "pending")
        if block:
            evidence += f"；阻塞：{block}"
        asset = str(row.get("relative_path") or "").replace("|", "\\|")
        bundle = str(row.get("course_bundle") or "").replace("|", "\\|")
        lines.append(f"| {row['queue_index']} | P{p} {label} | {row.get('kind','')} | `{bundle}` | `{asset}` | {mins} | {evidence} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="从课程资产账本创建去重、可恢复的学习软链接队列")
    parser.add_argument("--ledger", type=Path, required=True, help="build_asset_ledger.py 生成的 JSON")
    parser.add_argument("--archive-root", type=Path, required=True, help="授权原件根目录（只读）")
    parser.add_argument("--queue-dir", type=Path, required=True)
    parser.add_argument("--include-regex")
    parser.add_argument("--exclude-regex")
    parser.add_argument("--kind", action="append", choices=["video", "document", "image", "html", "note", "other"], help="限制媒介类型；可重复，默认全部")
    parser.add_argument("--videos-only", action="store_true", help="仅加入视频（等价于 --kind video）")
    parser.add_argument("--include-blocked", action="store_true", help="将未完成/阻塞项加入 repair 子队列；仍保留阻塞标记")
    parser.add_argument("--blocked-only", action="store_true", help="只选择阻塞/未完成项（通常用于单独的 repair 队列）")
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--max-hours", type=float)
    parser.add_argument("--start-index", type=int, default=1, help="从排序后的唯一队列第几项开始（1-based）")
    parser.add_argument("--no-links", action="store_true", help="只写清单，不创建软链接")
    args = parser.parse_args()

    ledger = args.ledger.expanduser().resolve()
    archive_root = args.archive_root.expanduser().resolve()
    queue_dir = args.queue_dir.expanduser().resolve()
    if not ledger.is_file():
        parser.error(f"账本不存在：{ledger}")
    if not archive_root.is_dir():
        parser.error(f"原件根目录不存在：{archive_root}")
    if args.max_items is not None and args.max_items <= 0:
        parser.error("--max-items 必须为正数")
    if args.start_index <= 0:
        parser.error("--start-index 必须为正数")
    if args.max_hours is not None and args.max_hours <= 0:
        parser.error("--max-hours 必须为正数")
    if args.blocked_only and not args.include_blocked:
        # Selecting blocked rows is an explicit repair operation; require the
        # caller to acknowledge that these assets are not learnable yet.
        args.include_blocked = True

    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        parser.error(f"无法读取账本：{exc}")
    records = payload.get("records")
    if not isinstance(records, list):
        parser.error("账本缺少 records 数组")
    kinds = set(args.kind or [])
    if args.videos_only:
        kinds = {"video"}
    if not kinds:
        kinds = {"video", "document", "image", "html", "note", "other"}

    selected = choose_unique(
        [item for item in records if isinstance(item, dict)],
        include=args.include_regex,
        exclude=args.exclude_regex,
        kinds=kinds,
        include_blocked=args.include_blocked,
        blocked_only=args.blocked_only,
        max_items=args.max_items,
        max_hours=args.max_hours,
        start_index=args.start_index,
    )

    queue_dir.mkdir(parents=True, exist_ok=True)
    links_root = queue_dir / ("repair" if args.include_blocked else "links")
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for index, row in enumerate(selected, start=1):
        relative = str(row.get("relative_path") or "")
        try:
            source = safe_resolve(archive_root, relative)
        except ValueError as exc:
            missing.append({"relative_path": relative, "error": str(exc)})
            continue
        if not source.is_file():
            missing.append({"relative_path": relative, "error": "原件不存在或不可读"})
            continue
        pnum, plabel = priority(str(row.get("course_bundle") or ""), str(row.get("kind") or ""))
        bundle_dir = links_root / f"P{pnum}-{slug(plabel)}" / slug(str(row.get("course_bundle") or "未归类"))
        bundle_dir.mkdir(parents=True, exist_ok=True)
        # Prefix with the queue index and asset id to avoid collisions from
        # duplicate lesson names across bundles.
        name = f"{index:04d}-{slug(source.name, 120)}"
        link = bundle_dir / name
        if not args.no_links:
            if link.exists() or link.is_symlink():
                try:
                    if link.is_symlink() and link.resolve(strict=False) == source:
                        pass
                    else:
                        link.unlink()
                except OSError:
                    missing.append({"relative_path": relative, "error": f"无法更新队列链接：{link}"})
                    continue
            try:
                link.symlink_to(source)
            except OSError as exc:
                missing.append({"relative_path": relative, "error": f"创建软链接失败：{exc}"})
                continue
        out = dict(row)
        out.update({
            "queue_index": index,
            "priority": pnum,
            "priority_label": plabel,
            "queue_path": str(link),
            "source_path": str(source),
            "exists": True,
        })
        rows.append(out)

    video_seconds = sum(duration_seconds(row) for row in rows if row.get("kind") == "video")
    aliases = sum(max(0, int(row.get("duplicate_count") or 1) - 1) for row in rows)
    blocked_items = sum(bool(row.get("_blocked")) for row in rows)
    queue_payload: dict[str, Any] = {
        "schema_version": 1,
        "type": "course_learning_queue",
        "status": "repair_queue" if args.include_blocked else "planned",
        "generated_at": now(),
        "ledger": str(ledger),
        "archive_root": str(archive_root),
        "queue_dir": str(queue_dir),
        "selection": {
            "kinds": sorted(kinds),
            "include_regex": args.include_regex or "",
            "exclude_regex": args.exclude_regex or "",
            "include_blocked": bool(args.include_blocked),
            "blocked_only": bool(args.blocked_only),
            "max_items": args.max_items,
            "max_hours": args.max_hours,
            "start_index": args.start_index,
            "links_created": not args.no_links,
        },
        "summary": {
            "selected_items": len(rows),
            "candidate_items": len(selected),
            "video_hours": video_seconds / 3600,
            "physical_aliases": aliases,
            "blocked_items": blocked_items,
            "missing_or_unlinked": len(missing),
        },
        "records": rows,
        "missing_or_unlinked": missing,
        "note": "队列只提供可恢复的证据处理顺序；完整转写/OCR、核读、去重、应用转译与 SRC 入库后，才可更新学习状态。",
    }
    (queue_dir / "queue.manifest.json").write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(queue_dir / "queue.csv", rows)
    write_markdown(queue_dir / "queue.md", queue_payload, rows)
    (queue_dir / "README.md").write_text(
        "# 可恢复课程学习队列\n\n"
        "此目录由 `build_queue.py` 生成。`links/`（或 `repair/`）下仅有指向授权原件的软链接；请勿把软链接目录当作新的课程副本。\n\n"
        "处理顺序：读取 `queue.manifest.json` → 对原件做转写/OCR/关键帧 → 回到原件逐段/逐页核验 → 写入 Obsidian `SRC/KNW/MOC` → 重新审计并更新学习状态。\n\n"
        "机器证据或队列存在不等于已学习；损坏/未完成项保留在 repair 队列，修复后应重新计算 SHA-256。\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": not missing,
        "queue": str(queue_dir),
        "manifest": str(queue_dir / "queue.manifest.json"),
        "summary": queue_payload["summary"],
        "missing_or_unlinked": missing[:20],
    }, ensure_ascii=False))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
