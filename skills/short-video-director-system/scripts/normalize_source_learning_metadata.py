#!/usr/bin/env python3
"""Add explicit learning metadata to an allow-listed set of reviewed sources.

This migration is intentionally conservative: it only touches the source IDs
passed with ``--source-id`` (or the documented 5.0/community allow-list), only
when the note is already ``status: canonical`` and contains an original-file
and application-translation section.  Evidence-only notes are left untouched.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ID_RE = re.compile(r"SRC-\d{4}")


def update_note(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^status:\s*canonical\s*$", text):
        return False
    if re.search(r"(?m)^learning_status:\s*learned\s*$", text):
        return False
    # Require the two human-review signals used by this vault.  A citation or
    # an extraction manifest alone must not be promoted by this migration.
    if "原件与学习证据" not in text or "应用转译" not in text:
        return False
    lines = text.splitlines()
    status_index = next(i for i, line in enumerate(lines) if line.startswith("status:"))
    lines[status_index + 1:status_index + 1] = [
        "learning_status: learned",
        "evidence_status: complete",
    ]
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="为已核读 canonical 来源补齐显式学习状态")
    parser.add_argument("--kb-root", type=Path, required=True)
    parser.add_argument("--source-id", action="append", dest="source_ids")
    args = parser.parse_args()
    source_root = args.kb_root.expanduser().resolve() / "02-来源档案"
    if not source_root.is_dir():
        raise SystemExit(f"来源目录不存在：{source_root}")
    requested = {value.upper() for value in (args.source_ids or []) if ID_RE.fullmatch(value.upper())}
    if not requested:
        # These are the reviewed 5.0 lessons and the reviewed first-person
        # camera bundle.  0216 is deliberately excluded: it is evidence-only.
        requested = {f"SRC-{n:04d}" for n in range(189, 216)} | {f"SRC-{n:04d}" for n in range(217, 233)}
    changed = []
    skipped = []
    for note in sorted(source_root.glob("SRC-*.md")):
        match = ID_RE.search(note.name.upper())
        if not match or match.group(0) not in requested:
            continue
        if update_note(note):
            changed.append(match.group(0))
        else:
            skipped.append(match.group(0))
    print({"updated": changed, "skipped_or_not_eligible": skipped})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
