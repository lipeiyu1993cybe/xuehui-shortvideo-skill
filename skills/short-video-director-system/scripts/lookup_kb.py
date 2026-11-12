#!/usr/bin/env python3
"""Rank exact course-knowledge matches from the configured Obsidian source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from resolve_kb import DEFAULT_LOCATOR, read_frontmatter, resolve


TYPE_BONUS = {
    "method_map": 35,
    "knowledge_card": 30,
    "decision_log": 25,
    "source_record": 5,
}

QUERY_ALIASES = {
    "ip影响力": ["故事母题", "平凡英雄", "帮助弱者", "跨文化事实展示"],
    "提升影响力": ["故事母题", "平凡英雄", "帮助弱者", "跨文化事实展示"],
    "文案写作技巧": ["画面感", "描述法", "类比法", "力量感"],
    "文案技巧": ["画面感", "描述法", "类比法", "力量感"],
}


def expand_terms(terms: list[str]) -> list[str]:
    expanded = list(terms)
    normalized_query = " ".join(terms).casefold().replace(" ", "")
    for alias, additions in QUERY_ALIASES.items():
        if alias in normalized_query:
            expanded.extend(additions)
    return list(dict.fromkeys(expanded))


def title_from(path: Path, metadata: dict[str, str]) -> str:
    return metadata.get("title") or path.stem


def matching_lines(lines: list[str], terms: list[str]) -> list[int]:
    indexes: list[int] = []
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if any(term.casefold() in lowered for term in terms):
            indexes.append(index)
    return indexes


def nearest_heading(lines: list[str], index: int) -> str:
    for cursor in range(index, -1, -1):
        if lines[cursor].startswith("#"):
            return lines[cursor].strip()
    return ""


def excerpts(lines: list[str], indexes: list[int], context: int) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    used_headings: set[str] = set()
    for index in indexes:
        heading = nearest_heading(lines, index)
        key = heading or str(index)
        if key in used_headings:
            continue
        used_headings.add(key)
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        selected.append(
            {
                "line": index + 1,
                "heading": heading,
                "text": "\n".join(lines[start:end]).strip(),
            }
        )
        if len(selected) == 3:
            break
    return selected


def score_document(
    text: str,
    lines: list[str],
    metadata: dict[str, str],
    terms: list[str],
) -> tuple[int, dict[str, int]]:
    title = title_from(Path(""), metadata).casefold()
    headings = "\n".join(line for line in lines if line.startswith("#")).casefold()
    lowered = text.casefold()
    counts: dict[str, int] = {}
    score = TYPE_BONUS.get(metadata.get("type", ""), 0)
    if metadata.get("status") == "canonical":
        score += 45
    elif metadata.get("status") == "evidence":
        score += 10

    for term in terms:
        needle = term.casefold()
        count = lowered.count(needle)
        counts[term] = count
        if not count:
            continue
        score += min(count, 12) * 3
        if needle in title:
            score += 35
        if needle in headings:
            score += 22

    if all(counts.values()):
        score += 30
    return score, counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按课程词检索并排序短视频编导知识库"
    )
    parser.add_argument("terms", nargs="+", help="2～5 个课程词或用户点名的例子")
    parser.add_argument("--limit", type=int, default=8, help="最多返回多少个文件")
    parser.add_argument("--context", type=int, default=2, help="命中行前后的上下文行数")
    parser.add_argument(
        "--locator", type=Path, default=DEFAULT_LOCATOR, help="知识源定位文件"
    )
    args = parser.parse_args()

    requested_terms = list(dict.fromkeys(term.strip() for term in args.terms if term.strip()))
    if not requested_terms:
        parser.error("至少提供一个非空课程词")
    terms = expand_terms(requested_terms)

    try:
        resolved = resolve(args.locator.expanduser())
        kb_root = Path(str(resolved["kb_root"]))
        matches: list[dict[str, object]] = []
        for path in kb_root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            indexes = matching_lines(lines, terms)
            if not indexes:
                continue
            try:
                metadata = read_frontmatter(path)
            except ValueError:
                metadata = {}
            score, counts = score_document(text, lines, metadata, terms)
            matches.append(
                {
                    "path": str(path.relative_to(kb_root)),
                    "title": title_from(path, metadata),
                    "type": metadata.get("type", ""),
                    "status": metadata.get("status", ""),
                    "score": score,
                    "term_counts": counts,
                    "excerpts": excerpts(lines, indexes, max(0, args.context)),
                }
            )

        matches.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        output = {
            "ok": True,
            "kb_revision": resolved["kb_revision"],
            "requested_terms": requested_terms,
            "expanded_terms": terms,
            "matches": matches[: max(1, args.limit)],
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
