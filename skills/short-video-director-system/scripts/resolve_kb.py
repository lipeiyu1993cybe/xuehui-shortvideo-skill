#!/usr/bin/env python3
"""Resolve and validate the Obsidian knowledge source for this skill."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


COLLECTION_ID = "short-video-director-system"
DEFAULT_LOCATOR = (
    Path.home()
    / ".codex"
    / "knowledge-sources"
    / "short-video-director-system.json"
)


def read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"知识库入口缺少 YAML frontmatter：{path}")

    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")

    raise ValueError(f"知识库入口的 YAML frontmatter 未闭合：{path}")


def ensure_inside(child: Path, parent: Path) -> None:
    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise ValueError("知识库入口不能位于所配置的 vault 之外") from exc


def validate_entry_links(entry: Path, kb_root: Path, kb_revision: int) -> list[str]:
    """Validate the first-level task routes declared by the knowledge entry."""

    targets: list[str] = []
    for raw in re.findall(r"\[\[([^\]]+)\]\]", entry.read_text(encoding="utf-8")):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in targets:
            targets.append(target)

    validated: list[str] = []
    for target in targets:
        name = Path(target).name
        candidates = list(kb_root.rglob(f"{name}.md"))
        if not candidates:
            raise FileNotFoundError(f"知识库入口路由目标不存在：{target}")
        if len(candidates) > 1:
            matches = "、".join(str(path.relative_to(kb_root)) for path in candidates)
            raise ValueError(f"知识库入口路由目标不唯一：{target} -> {matches}")

        routed = candidates[0]
        routed_meta = read_frontmatter(routed)
        status = routed_meta.get("status")
        if status not in {"canonical", "evidence"}:
            raise ValueError(
                f"知识库入口路由目标状态不可用：{routed}，status={status!r}"
            )

        try:
            routed_revision = int(str(routed_meta["kb_revision"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"知识库入口路由目标缺少有效 kb_revision：{routed}") from exc
        if routed_revision > kb_revision:
            raise ValueError(
                f"知识库入口路由目标版本超前：{routed}，"
                f"入口 {kb_revision}，目标 {routed_revision}"
            )

        validated.append(str(routed.relative_to(kb_root)))

    return validated


def resolve(locator_path: Path) -> dict[str, object]:
    if not locator_path.is_file():
        raise FileNotFoundError(f"知识源定位文件不存在：{locator_path}")

    locator = json.loads(locator_path.read_text(encoding="utf-8"))
    required = {
        "collection_id",
        "vault_id",
        "vault_path",
        "entry",
        "schema_version",
    }
    missing = sorted(required - locator.keys())
    if missing:
        raise ValueError(f"知识源定位文件缺少字段：{', '.join(missing)}")
    if locator["collection_id"] != COLLECTION_ID:
        raise ValueError("定位文件的 collection_id 与技能不匹配")

    vault_path = Path(str(locator["vault_path"])).expanduser().resolve()
    if not (vault_path / ".obsidian").is_dir():
        raise ValueError(f"配置路径不是可识别的 Obsidian vault：{vault_path}")

    entry = (vault_path / str(locator["entry"])).resolve()
    ensure_inside(entry, vault_path)
    if not entry.is_file():
        raise FileNotFoundError(f"知识库入口不存在：{entry}")

    metadata = read_frontmatter(entry)
    checks = {
        "collection_id": str(locator["collection_id"]),
        "vault_id": str(locator["vault_id"]),
        "schema_version": str(locator["schema_version"]),
        "status": "canonical",
    }
    for field, expected in checks.items():
        actual = metadata.get(field)
        if actual != expected:
            raise ValueError(
                f"入口字段 {field} 不匹配：期望 {expected!r}，实际 {actual!r}"
            )

    kb_revision = int(str(metadata.get("kb_revision", "0")))
    validated_entry_links = validate_entry_links(entry, entry.parent, kb_revision)

    return {
        "ok": True,
        "collection_id": COLLECTION_ID,
        "vault_id": locator["vault_id"],
        "vault_path": str(vault_path),
        "kb_root": str(entry.parent),
        "entry": str(entry),
        "schema_version": int(str(metadata["schema_version"])),
        "kb_revision": kb_revision,
        "status": metadata["status"],
        "validated_entry_links": validated_entry_links,
        "locator": str(locator_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="定位并校验短视频编导 Obsidian 知识库"
    )
    parser.add_argument(
        "--locator",
        type=Path,
        default=DEFAULT_LOCATOR,
        help="知识源定位 JSON；默认读取用户级固定位置",
    )
    args = parser.parse_args()

    try:
        result = resolve(args.locator.expanduser())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
