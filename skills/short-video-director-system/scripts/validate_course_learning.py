#!/usr/bin/env python3
"""Validate an evidence-aware course learning ledger without mutating files.

The validator deliberately separates three outcomes:

* ``learned``/``semantic_duplicate``/``user_excluded`` are terminal learning
  states (the latter two are not claimed as learned content).
* complete, valid assets still marked ``not_learned`` are actionable queue
  items.
* incomplete, corrupt, or externally dependent assets are reported as
  blockers and never silently converted to learned.

It is intentionally independent of the ledger builder so that a malformed or
stale builder output cannot validate itself.  Exit status is zero for a
structurally valid snapshot unless ``--strict`` is supplied and actionable
pending items, blockers, or version mismatches remain.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


LEARNED = "learned"
TERMINAL = {"learned", "semantic_duplicate", "user_excluded"}
ALLOWED_BATCH_STATES = {
    "completed_learning",
    "evidence_preparation",
    "blocked",
    "draft",
    "pending_evidence",
    # Queue snapshots are intentionally not learning manifests.  They are
    # allowed here so the validator can inspect a whole batch directory while
    # still treating their pending rows as non-terminal.
    "planned",
    "repair_queue",
    "superseded",
}
SHA_RE = re.compile(r"\b[0-9a-f]{64}\b", re.I)
SRC_RE = re.compile(r"\bSRC-\d+\b")
REV_RE = re.compile(r"(?m)^kb_revision:\s*(\d+)\s*$")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON：{path}（{exc}）") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象：{path}")
    return value


def frontmatter(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def source_catalog(kb_root: Path) -> dict[str, tuple[Path, dict[str, str]]]:
    """Return one authoritative note for each source id.

    The vault intentionally keeps superseded source notes as historical
    evidence.  A simple ``dict`` assignment over ``glob()`` results is not
    safe here: filesystem traversal order is unspecified and can make a
    superseded duplicate win over the canonical note (or make validation
    change from run to run).  Collect all candidates first, then choose by a
    stable status/revision ranking.  Canonical/learned records always outrank
    evidence and superseded records; the filename is only the final
    deterministic tie-breaker.  Historical files therefore remain on disk,
    while every validator lookup has a single, predictable canonical target.
    """
    candidates: dict[str, list[tuple[Path, dict[str, str]]]] = {}
    source_root = kb_root / "02-来源档案"
    if not source_root.is_dir():
        return {}
    for note in sorted(source_root.glob("SRC-*.md"), key=lambda item: item.name.casefold()):
        fields = frontmatter(note)
        match = SRC_RE.search(note.name)
        if not match:
            continue
        candidates.setdefault(match.group(0).upper(), []).append((note, fields))

    def rank(item: tuple[Path, dict[str, str]]) -> tuple[int, int, int, str]:
        note, fields = item
        status = fields.get("status", "").strip().casefold()
        learning = fields.get("learning_status", "").strip().casefold()
        # An active canonical record is the sole authority when a historical
        # duplicate carries the same SRC id.  ``learned`` is accepted as an
        # older active spelling; evidence/draft records remain fallback only
        # when no active record exists.
        status_rank = {
            "canonical": 0,
            "learned": 1,
            "evidence": 2,
            "draft": 3,
            "superseded": 9,
        }.get(status, 8)
        learning_rank = {"learned": 0, "semantic_duplicate": 5}.get(learning, 4)
        # Prefer the newest revision within the same lifecycle state, while
        # retaining a stable lexical tie-breaker for equal metadata.
        def integer_field(name: str) -> int:
            try:
                return int(fields.get(name, "0").strip())
            except (TypeError, ValueError):
                return 0

        revision = max(integer_field("revision"), integer_field("kb_revision"))
        return (status_rank, learning_rank, -revision, note.name.casefold())

    catalog: dict[str, tuple[Path, dict[str, str]]] = {}
    for source_id, entries in candidates.items():
        catalog[source_id] = min(entries, key=rank)
    return catalog


def source_learning_quality(
    note: Path,
    asset: dict[str, Any],
    fields: dict[str, str],
) -> list[str]:
    """Return human-learning-gate gaps for one source/asset pair.

    A source citation or an extraction manifest is not enough to prove that a
    course asset was learned.  The strict final gate therefore checks the
    explicit frontmatter state, an exact SHA/path citation, the review and
    application sections, and at least one material evidence artifact.  The
    default validator reports these as warnings so an in-progress ledger can
    still be inspected; ``--strict`` promotes them to failures.
    """
    problems: list[str] = []
    status = fields.get("status", "").strip().lower()
    learning = fields.get("learning_status", "").strip().lower()
    expected_learning = str(asset.get("learning_status") or LEARNED).strip().lower()
    if status not in {"canonical", "learned"}:
        problems.append(f"{note.name}: status 不是 canonical/learned")
    allowed_learning = {"learned"}
    if expected_learning == "semantic_duplicate":
        allowed_learning.add("semantic_duplicate")
    if learning not in allowed_learning:
        problems.append(f"{note.name}: 缺少匹配的 learning_status")
    try:
        content = note.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return problems + [f"{note.name}: 来源档案不可读"]
    digest = str(asset.get("sha256") or "").strip().lower()
    relative = str(asset.get("relative_path") or "")
    # Keep this check independent from the ledger builder, but use the same
    # primary-original boundary.  A SHA mentioned only in a transcript/OCR,
    # supporting-note or cache line must not validate the corresponding
    # course asset as learned.
    explicit_hashes: set[str] = set()
    for key in (
        "source_asset_sha256", "source_asset_sha", "primary_sha256",
        "source_sha256", "original_sha256", "source_file_sha256",
    ):
        explicit_hashes.update(
            re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", fields.get(key, "").lower())
        )
    primary_hashes = set(explicit_hashes)
    in_original_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().casefold()
            in_original_section = any(token in heading for token in ("原件", "来源与完整性", "原件核验"))
            continue
        if not in_original_section:
            continue
        lowered = line.casefold()
        line_hashes = set(SHA_RE.findall(lowered))
        if not line_hashes:
            continue
        # An aggregate original table that identifies an ASSET and carries a
        # full SHA is an exact primary mapping.  Otherwise require an explicit
        # original/T7/file-size marker and exclude generated-evidence lines.
        aggregate_row = "asset-" in lowered and "|" in line
        primary_marker = any(token in lowered for token in (
            "t7", "原件", "完整原件", "本地完整原件", "来源文件",
            "文件大小", "大小", "字节", "sha-256", "sha256",
        ))
        evidence_only = any(token in lowered for token in (
            "转写", "ocr", "关键帧", "抽取", "提取", "缓存", "工件",
            "支持材料", "辅助", "交叉核对", "仅作",
        ))
        if aggregate_row or (primary_marker and not evidence_only):
            primary_hashes.update(value.lower() for value in line_hashes)
    hash_match = bool(digest and digest in primary_hashes)
    if digest and not hash_match:
        problems.append(f"{note.name}: 未精确引用资产 SHA-256")
    elif not digest and relative and relative not in content and relative != fields.get("source_asset_path", ""):
        problems.append(f"{note.name}: 未精确引用资产路径")
    if not any(heading in content for heading in ("原件与学习证据", "原件与证据", "来源与完整性", "原件核验")):
        problems.append(f"{note.name}: 缺少原件/证据段落")
    if "应用转译" not in content:
        problems.append(f"{note.name}: 缺少“应用转译”段落")
    artifacts = asset.get("artifact_paths") or []
    if not artifacts:
        problems.append(f"{note.name}: 账本没有 artifact_paths")
    return problems


def artifact_path(raw: object, kb_root: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = Path(raw.strip()).expanduser()
    if value.is_absolute():
        return value
    # Ledger displays paths beneath the vault as-is.  A few older artifacts
    # omit the ``02-转写与OCR`` prefix, so try both the literal path and that
    # evidence root without guessing outside the vault.
    candidates = [kb_root / value]
    if not str(value).startswith("02-转写与OCR"):
        candidates.append(kb_root / "02-转写与OCR" / value)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def revision_from_index(kb_root: Path) -> int | None:
    path = kb_root / "_index.md"
    if not path.is_file():
        return None
    match = REV_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def revision_from_skill(skill_path: Path | None) -> int | None:
    if skill_path is None or not skill_path.is_file():
        return None
    content = skill_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"last_synced_kb_revision:\s*`?(\d+)`?", content)
    return int(match.group(1)) if match else None


def manifest_checks(manifest_dir: Path, catalog: dict[str, tuple[Path, dict[str, str]]]) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0
    state_counts: Counter[str] = Counter()
    stale_completed: list[str] = []
    for path in sorted(manifest_dir.rglob("*.json")) if manifest_dir.is_dir() else []:
        # Audit/ledger sidecars are not batch manifests; only inspect files
        # whose name explicitly identifies a batch or queue manifest.
        if not (path.name.startswith("批次-") or path.name.endswith(".manifest.json") or path.name.startswith("queue")):
            continue
        try:
            payload = read_json(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        status = str(payload.get("status") or "").strip()
        if not status:
            continue
        checked += 1
        state_counts[status] += 1
        if status not in ALLOWED_BATCH_STATES:
            errors.append(f"{path.name}: 未知批次状态 {status!r}")
        base = payload.get("base_kb_revision")
        completed = payload.get("completed_kb_revision")
        if status == "completed_learning":
            if not isinstance(completed, int):
                errors.append(f"{path.name}: completed_learning 缺少 completed_kb_revision")
            if isinstance(base, int) and isinstance(completed, int) and completed < base:
                errors.append(f"{path.name}: completed_kb_revision 小于 base_kb_revision")
            records = payload.get("records")
            if not isinstance(records, list) or not records:
                errors.append(f"{path.name}: completed_learning 没有 records")
            else:
                bad = 0
                for item in records:
                    if not isinstance(item, dict):
                        bad += 1
                        continue
                    learning = str(item.get("learning_status") or "")
                    if learning not in TERMINAL:
                        bad += 1
                    source_ids = set(SRC_RE.findall(json.dumps(item, ensure_ascii=False)))
                    source_id = str(item.get("source_id") or "")
                    if source_id:
                        source_ids.add(source_id)
                    for source_id in source_ids:
                        if source_id not in catalog:
                            warnings.append(f"{path.name}: {source_id} 未找到对应 SRC 文件")
                if bad:
                    stale_completed.append(path.name)
                    warnings.append(f"{path.name}: {bad} 条 records 仍不是终态，需更新 manifest 或降级状态")
    return errors, warnings, {
        "checked": checked,
        "status_counts": dict(state_counts),
        "stale_completed": stale_completed,
    }


def validate(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    ledger_path = args.ledger.expanduser().resolve()
    kb_root = args.kb_root.expanduser().resolve()
    try:
        ledger = read_json(ledger_path)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}

    assets = ledger.get("unique_assets")
    if not isinstance(assets, list):
        assets = ledger.get("records")
    if not isinstance(assets, list):
        errors.append("账本缺少 unique_assets/records 数组")
        assets = []

    catalog = source_catalog(kb_root)
    status_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    pending: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    learned_without_source: list[str] = []
    learned_blocked: list[str] = []
    missing_artifacts: list[str] = []
    learning_gate_gaps: list[str] = []
    for item in assets:
        if not isinstance(item, dict):
            errors.append("账本含非对象资产条目")
            continue
        asset_id = str(item.get("asset_id") or item.get("relative_path") or "<unknown>")
        status = str(item.get("learning_status") or "not_learned")
        kind = str(item.get("kind") or item.get("category") or "unknown")
        status_counts[status] += 1
        kind_counts[kind] += 1
        integrity = str(item.get("integrity_status") or "")
        download = str(item.get("download_status") or "")
        blocked_reason = str(item.get("blocked_reason") or "")
        source_ids = {str(value) for value in (item.get("source_ids") or []) if value}
        source_ids.update(SRC_RE.findall(str(item.get("learning_evidence") or "")))
        unknown_sources = sorted(source_ids - set(catalog))
        if unknown_sources:
            warnings.append(f"{asset_id}: 未找到来源档案 {', '.join(unknown_sources)}")
        if status == LEARNED and not source_ids:
            learned_without_source.append(asset_id)
        if status in {LEARNED, "semantic_duplicate"}:
            # Every terminal content state must be backed by a source note
            # that explicitly passed the human review gate.  Keep the check
            # independent from the ledger builder so stale citations cannot
            # validate themselves.
            if source_ids:
                source_quality_ok = False
                candidate_gaps: list[str] = []
                for source_id in sorted(source_ids):
                    source_entry = catalog.get(source_id)
                    if not source_entry:
                        continue
                    note, fields = source_entry
                    gaps = source_learning_quality(note, item, fields)
                    if not gaps:
                        source_quality_ok = True
                    else:
                        candidate_gaps.extend(f"{asset_id}: {gap}" for gap in gaps)
                if not source_quality_ok and source_ids:
                    # Keep the detailed reasons above; this marker makes the
                    # summary easy to search in compact output.
                    learning_gate_gaps.extend(candidate_gaps)
                    learning_gate_gaps.append(
                        f"{asset_id}: 没有通过完整学习门槛的来源档案"
                    )
        # ``links_only`` means that a local HTML snapshot has outbound
        # dependencies we intentionally did not fetch.  The local title/body
        # can still be reviewed; keep the dependency visible without turning
        # it into an integrity blocker.  This also lets the validator inspect
        # older ledgers that encoded the boundary in ``blocked_reason``.
        external_only = (
            kind == "html"
            and download in {"complete", ""}
            and integrity not in {"corrupt", "incomplete", "unsupported_format", "partial"}
            and str(item.get("external_dependency_status") or "") in {"links_only", "external_unverified"}
            and all(reason.strip() in {"", "external_resources_not_captured"} for reason in blocked_reason.split(";"))
        )
        is_blocked = ((bool(blocked_reason) and not external_only)
                      or download not in {"complete", ""}
                      or integrity in {"corrupt", "incomplete", "unsupported_format", "partial"})
        if status == LEARNED and is_blocked:
            learned_blocked.append(asset_id)
        if status == LEARNED:
            paths = item.get("artifact_paths") or []
            if isinstance(paths, str):
                paths = [paths]
            existing = [artifact_path(path, kb_root) for path in paths if artifact_path(path, kb_root) is not None]
            if paths and not any(path and path.exists() for path in existing):
                missing_artifacts.append(asset_id)
        if status not in TERMINAL:
            row = {
                "asset_id": asset_id,
                "kind": kind,
                "relative_path": str(item.get("relative_path") or ""),
                "integrity_status": integrity,
                "download_status": download,
                "blocked_reason": blocked_reason,
                "external_dependency_status": str(item.get("external_dependency_status") or "none"),
            }
            if is_blocked:
                blockers.append(row)
            else:
                pending.append(row)

    if learned_without_source:
        errors.append(f"{len(learned_without_source)} 个 learned 资产没有 source_ids")
    if learned_blocked:
        errors.append(f"{len(learned_blocked)} 个 learned 资产仍有完整性/下载阻塞")
    if missing_artifacts:
        warnings.append(f"{len(missing_artifacts)} 个 learned 资产的 artifact_paths 在知识库中不存在")
    if learning_gate_gaps:
        warnings.append(f"{len(learning_gate_gaps)} 个终态资产的来源/学习门槛需要复核")

    index_revision = revision_from_index(kb_root)
    ledger_revision = ledger.get("kb_revision")
    if index_revision is not None and ledger_revision != index_revision:
        errors.append(f"账本 kb_revision={ledger_revision!r} 与 _index.md={index_revision} 不一致")
    skill_revision = revision_from_skill(args.skill.expanduser().resolve() if args.skill else None)
    if index_revision is not None and skill_revision is not None and skill_revision != index_revision:
        errors.append(f"Skill 检查点={skill_revision} 与 _index.md={index_revision} 不一致")

    manifest_dir = args.manifest_dir.expanduser().resolve() if args.manifest_dir else kb_root / "03-批次清单与校验"
    manifest_errors, manifest_warnings, manifest_summary = manifest_checks(manifest_dir, catalog)
    errors.extend(manifest_errors)
    warnings.extend(manifest_warnings)

    summary = ledger.get("summary") if isinstance(ledger.get("summary"), dict) else {}
    reported_unique = summary.get("unique_assets")
    if isinstance(reported_unique, int) and reported_unique != len(assets):
        errors.append(f"账本 summary.unique_assets={reported_unique} 与条目数={len(assets)} 不一致")

    counts = {
        "unique_assets": len(assets),
        "status": dict(status_counts),
        "kind": dict(kind_counts),
        "actionable_pending": len(pending),
        "blocked": len(blockers),
        "external_unverified_pending": sum(
            row["external_dependency_status"] in {"links_only", "external_unverified"}
            for row in pending
        ),
    }
    strict_failures = []
    if args.strict:
        if pending:
            strict_failures.append(f"仍有 {len(pending)} 个可学习完整资产待核读")
        if blockers:
            strict_failures.append(f"仍有 {len(blockers)} 个阻塞资产")
        if manifest_summary["stale_completed"]:
            strict_failures.append("存在 completed_learning manifest 的记录未达终态")
        if learning_gate_gaps:
            strict_failures.append(
                f"存在 {len(learning_gate_gaps)} 个终态资产未通过来源/学习门槛"
            )
    errors.extend(strict_failures)
    return {
        "ok": not errors,
        "ledger": str(ledger_path),
        "kb_root": str(kb_root),
        "revisions": {
            "ledger": ledger_revision,
            "index": index_revision,
            "skill": skill_revision,
        },
        "counts": counts,
        "manifest_checks": manifest_summary,
        "learned_without_source": learned_without_source,
        "learned_blocked": learned_blocked,
        "missing_artifacts": missing_artifacts,
        "learning_gate_gaps": learning_gate_gaps,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读验证课程学习账本、来源、证据和版本一致性")
    parser.add_argument("--ledger", type=Path, required=True, help="统一资产账本 JSON")
    parser.add_argument("--kb-root", type=Path, required=True, help="Obsidian 知识库根目录")
    parser.add_argument("--skill", type=Path, help="可选：short-video-director-system/SKILL.md")
    parser.add_argument("--manifest-dir", type=Path, help="可选：批次 manifest 根目录")
    parser.add_argument("--strict", action="store_true", help="待学习或阻塞项存在时以失败退出")
    parser.add_argument("--compact", action="store_true", help="单行 JSON 输出")
    args = parser.parse_args()
    result = validate(args)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
