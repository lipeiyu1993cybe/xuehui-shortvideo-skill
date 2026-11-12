#!/usr/bin/env python3
"""Skill 与 Obsidian 知识库的双向同步辅助。

三个子命令：
  check   比对 Skill 检查点与知识库 kb_revision，只报告不改文件
  sync    把 Skill 检查点升到知识库当前 kb_revision（需要显式调用）
  index   扫描知识库 canonical 笔记，生成只含标题与路径的路由索引

设计约束（见 references/course-distillation-supplement.md）：
Skill 是执行入口，Obsidian 是唯一可编辑真源。同步的是「同一真源、同一版本检查、
同一执行规则」，不是把整库复制成第二份静态文本。索引只记录去哪里读，不记录正文。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"
KB_ROOT = Path("/Users/lipeiyu/Documents/Codex/仓库/短视频编导知识库")
INDEX_MD = KB_ROOT / "_index.md"
ROUTE_INDEX = Path(__file__).resolve().parent.parent / "references" / "kb-route-index.md"

FRONTMATTER = re.compile(r"^---\s*$(.*?)^---\s*$", re.MULTILINE | re.DOTALL)


def read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def kb_revision() -> int:
    fields = read_frontmatter(INDEX_MD)
    try:
        return int(fields.get("kb_revision", "-1"))
    except ValueError:
        return -1


def skill_checkpoint() -> int:
    try:
        text = SKILL_MD.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return -1
    match = re.search(r"last_synced_kb_revision:\s*(\d+)", text)
    return int(match.group(1)) if match else -1


def cmd_check(_args: argparse.Namespace) -> int:
    kb = kb_revision()
    sk = skill_checkpoint()
    payload = {
        "ok": kb == sk,
        "kb_revision": kb,
        "skill_checkpoint": sk,
        "kb_root": str(KB_ROOT),
        "skill_md": str(SKILL_MD),
    }
    if kb != sk:
        payload["action"] = "运行 sync 子命令把 Skill 检查点升至知识库版本"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if kb == sk else 1


def cmd_sync(_args: argparse.Namespace) -> int:
    kb = kb_revision()
    sk = skill_checkpoint()
    if kb < 0:
        print(json.dumps({"ok": False, "error": "无法读取知识库 kb_revision"},
                         ensure_ascii=False))
        return 2
    if kb == sk:
        print(json.dumps({"ok": True, "unchanged": True, "kb_revision": kb},
                         ensure_ascii=False))
        return 0

    text = SKILL_MD.read_text(encoding="utf-8", errors="ignore")
    today = date.today().isoformat()
    new_text, n1 = re.subn(r"last_synced_kb_revision:\s*\d+",
                           f"last_synced_kb_revision: {kb}", text)
    new_text, n2 = re.subn(r"`checked_at:\s*[\d-]+`", f"`checked_at: {today}`", new_text)
    if not n1:
        print(json.dumps({"ok": False, "error": "SKILL.md 未找到 last_synced_kb_revision 字段"},
                         ensure_ascii=False))
        return 2
    SKILL_MD.write_text(new_text, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "kb_revision": kb,
        "previous_checkpoint": sk,
        "checked_at": today,
        "updated_fields": {"checkpoint": n1, "checked_at": n2},
    }, ensure_ascii=False, indent=2))
    return 0


def collect_notes(pattern: str, kind: str) -> list[dict]:
    notes = []
    for path in sorted(KB_ROOT.glob(pattern)):
        fields = read_frontmatter(path)
        if fields.get("status") != "canonical":
            continue
        title = fields.get("title") or path.stem
        rel = path.relative_to(KB_ROOT)
        notes.append({"kind": kind, "title": title, "path": str(rel)})
    return notes


def cmd_index(_args: argparse.Namespace) -> int:
    kb = kb_revision()
    maps = collect_notes("03-方法地图/MOC-*.md", "方法地图")
    cards = collect_notes("04-知识卡片/KNW-*.md", "知识卡片")
    templates = collect_notes("06-模板/TPL-*.md", "模板")

    lines = [
        "---",
        "type: route_index",
        "status: canonical",
        f"kb_revision: {kb}",
        f"generated_at: {date.today().isoformat()}",
        "---",
        "",
        "# 知识库路由索引",
        "",
        "本文件由 `scripts/sync_skill_kb.py index` 生成，**只记录去哪里读，不复制正文**。",
        "需要完整内容时按路径到 Obsidian 知识库读取；本索引不替代动态真源。",
        "",
        f"知识库根：`{KB_ROOT}`",
        f"当前 `kb_revision`：{kb}",
        "",
    ]

    for label, items in (("方法地图", maps), ("知识卡片", cards), ("模板", templates)):
        if not items:
            continue
        lines.append(f"## {label}（{len(items)}）")
        lines.append("")
        for item in items:
            lines.append(f"- {item['title']} — `{item['path']}`")
        lines.append("")

    ROUTE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    ROUTE_INDEX.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "kb_revision": kb,
        "maps": len(maps),
        "cards": len(cards),
        "templates": len(templates),
        "output": str(ROUTE_INDEX),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="比对版本，只报告")
    sub.add_parser("sync", help="把 Skill 检查点升到知识库版本")
    sub.add_parser("index", help="生成知识库路由索引")
    args = ap.parse_args()
    return {
        "check": cmd_check,
        "sync": cmd_sync,
        "index": cmd_index,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
