#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_standards.py — inline the shared rules into a repository's `AGENTS.md`.

Why this exists
---------------
A pointer (`see ~/.agents/standards/STANDARD.md`) only works for a human, or for an agent that
chooses to follow it *and* can read that path. Some sessions cannot, so the rule silently stops
binding. The rules that bind on every edit are therefore carried **in the repository**, in a
generated block:

    <!-- BEGIN standards-core ... -->
    ...
    <!-- END standards-core ... -->

The block is a copy, and a copy drifts — that is precisely what the old pointer design avoided.
It stays honest because **nobody edits it by hand**: this script renders it from the canonical
files, and `--check` fails when a checkout has drifted from the current standard.

What is copied, and what is not
-------------------------------
  * Copied (bind on every edit): `STANDARD.md` §1–§6 and the repository's own `dimensions/<x>.md`.
  * Not copied (low-frequency procedures, opened when doing the thing): `STANDARD.md` §7–§11 —
    the audit anchor, the profile listing, retiring a repository, About, citation stability. A
    repository keeps a one-line pointer for those; they are not "remembered every session".

The copied text stays in the language it is written in (English today). Translating it would
create a second source of truth per language, which is the drift this block exists to prevent.

Usage
-----
    python3 ~/.agents/scripts/render_standards.py --check .      # drift check (CI / audit)
    python3 ~/.agents/scripts/render_standards.py --write .      # render the block into AGENTS.md

Exit codes: 0 = in sync (or written), 1 = drift / missing block / unusable input.
Python 3 standard library only — offline, no dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

STANDARDS = Path(__file__).resolve().parent.parent / "standards"
STANDARD_MD = STANDARDS / "STANDARD.md"
BEGIN = "<!-- BEGIN standards-core"
END = "<!-- END standards-core"

# §1–§6 bind on every edit; §7–§11 are procedures and stay pointers.
COPIED_SECTIONS = (1, 2, 3, 4, 5, 6)

LOCK_NAME = "standards.lock"

DIMENSION_NAMES = {
    "a": "A — open source (strict)",
    "b": "B — application (ordinary)",
    "c": "C — private knowledge base & personal tools",
}


def split_sections(text: str) -> dict[int, tuple[str, str]]:
    """Map section number -> (title, body). Bodies stop at the next `## N.` heading."""
    out: dict[int, tuple[str, str]] = {}
    lines = text.splitlines()
    idx: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        if m:
            idx.append((i, int(m.group(1)), m.group(2).strip()))
    for n, (start, num, title) in enumerate(idx):
        end = idx[n + 1][0] if n + 1 < len(idx) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip("\n")
        out[num] = (title, body)
    return out


def strip_meta_blockquotes(body: str) -> str:
    """Drop a leading `> **canonical.**` note: it describes the file, not the rule."""
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith(">"):
        j = i
        while j < len(lines) and lines[j].lstrip().startswith(">"):
            j += 1
        if "canonical" in "\n".join(lines[i:j]).lower():
            rest = lines[j:]
            while rest and not rest[0].strip():
                rest.pop(0)
            return "\n".join(rest)
    return body


def demote(text: str, levels: int = 2) -> str:
    """Push every heading down `levels` so the copied text nests under `### …`.

    Standalone `---` rules are dropped: inside a generated block they add visual breakage
    (`AGENTS.md` already separates its own sections) without carrying meaning.
    """
    out = []
    for line in text.splitlines():
        if line.strip() == "---":
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)) + levels, 6)
            out.append("#" * level + " " + m.group(2))
        else:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def dirhash(root: Path) -> str:
    """sha1 over "<file hash>\\t<path>" lines, paths sorted — the same shape audit.sh uses."""
    rows = []
    for f in sorted((p for p in root.rglob("*") if p.is_file()),
                    key=lambda p: "./" + p.relative_to(root).as_posix()):
        rel = "./" + f.relative_to(root).as_posix()
        rows.append(f"{hashlib.sha1(f.read_bytes()).hexdigest()}\t{rel}\n")
    return hashlib.sha1("".join(rows).encode()).hexdigest()


def block_digest(block: str) -> str:
    return hashlib.sha256(block.encode()).hexdigest()


def read_lock(root: Path) -> dict[str, str]:
    path = root / LOCK_NAME
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def write_lock(root: Path, updates: dict[str, str]) -> None:
    """Merge `updates` into the lock; `source` is refreshed from the standards tree.

    Only written where the standards tree is readable (this machine). A checkout without
    `~/.agents` never has a lock to write, and `--verify-lock` treats that as skipped.
    """
    current = read_lock(root)
    current.update(updates)
    lines = [
        "# standards.lock — generated by render_standards.py; do not edit by hand.",
        "# source: sha1 of the standards/ directory this block was rendered from.",
        "# One line per rules file: its name and the sha256 of its standards-core block.",
        f"source  {dirhash(STANDARDS)}",
    ]
    for name in sorted(k for k in current if k != "source"):
        lines.append(f"{name}  {current[name]}")
    (root / LOCK_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def desection(text: str) -> str:
    """Render `§3` / `§1–§6` as `section 3` / `sections 1–6`.

    Two guards in the fleet (`copyous`, `burn-my-windows`) forbid the section sign outright, or
    resolve every `§N` to a file that must exist in the checkout — and the copied rules cite a
    file that lives outside it. Spelling the reference out keeps the block self-contained and
    keeps those guards meaningful instead of exempting the block.
    """
    text = re.sub(r"§(\d+)\s*[–—-]\s*§(\d+)", r"sections \1–\2", text)
    return re.sub(r"§(\d+|N)\b", r"section \1", text)


def detect_dimension(agents: str) -> str | None:
    """Read the dimension from either pointer form: `**Dimension.** A` or `**档位。** … **B 档**`."""
    m = re.search(r"\*\*Dimension\.\*\*\s*\*{0,2}([ABC])\b", agents)
    if not m:
        m = re.search(r"\*\*档位。\*\*[\s\S]{0,80}?\*{0,2}([ABC])\s*档", agents)
    return m.group(1).lower() if m else None


def render(dimension: str) -> str:
    std = STANDARD_MD.read_text(encoding="utf-8")
    sections = split_sections(std)
    missing = [n for n in COPIED_SECTIONS if n not in sections]
    if missing:
        raise SystemExit(f"render_standards: STANDARD.md is missing section(s) {missing}")

    dim_file = STANDARDS / "dimensions" / f"{dimension}.md"
    if not dim_file.is_file():
        raise SystemExit(f"render_standards: no dimension card at {dim_file}")
    dim_text = dim_file.read_text(encoding="utf-8")
    # Drop the dimension page's own title line; the heading below names it instead.
    dim_body = re.sub(r"^#\s+.*\n", "", dim_text, count=1)
    # Drop the `> **canonical.** …` note that follows the title.
    dim_body = strip_meta_blockquotes(dim_body.strip("\n"))

    parts = [
        "<!-- BEGIN standards-core",
        "     generated from ~/.agents/standards/ by render_standards.py; do not edit by hand.",
        f"     sources: STANDARD.md §1–§6 + dimensions/{dimension}.md",
        "     regenerate: python3 ~/.agents/scripts/render_standards.py --write . -->",
        "",
        f"### Shared hard rules — from `STANDARD.md` §1–§6",
        "",
        "> These bind on every edit in this repository. They are carried here rather than pointed at,",
        "> because a session may not be able to read `~/.agents`. The canonical text is",
        "> `~/.agents/standards/STANDARD.md`; this is a generated copy — change it there, never here.",
        "",
    ]
    for n in COPIED_SECTIONS:
        title, body = sections[n]
        body = strip_meta_blockquotes(body)
        parts.append(f"#### {n}. {title}")
        parts.append("")
        parts.append(demote(body, 2))
        parts.append("")

    parts += [
        f"### Dimension {dimension.upper()} additions — beyond the shared rules above",
        "",
        "> Generated from `~/.agents/standards/dimensions/"
        f"{dimension}.md`. Same rule: edit the source, never this copy.",
        "",
        demote(dim_body, 2),
        "",
        "### Procedures (pointers — open when doing the thing)",
        "",
        "> Copied rules above are what every edit obeys. The steps for a rare operation stay in the",
        "> standard and are read when that operation happens: retiring a repository",
        "> (`STANDARD.md` §9), About (`§10`), citation stability (`§11`), the audit anchor (`§7`),",
        "> and the profile listing triggers (`§8`).",
        "",
        "<!-- END standards-core -->",
    ]
    return desection("\n".join(parts))


def extract_block(text: str) -> str | None:
    """The generated block, ending at the END marker's line."""
    if BEGIN not in text or END not in text:
        return None
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    while end < len(text) and text[end] != "\n":
        end += 1
    return text[start:end].rstrip("\n")


def insert_block(agents: str, block: str) -> str:
    """Place the block right after the `> **Dimension.**` pointer block."""
    lines = agents.splitlines()
    start = next(
        (i for i, l in enumerate(lines)
         if l.startswith(">") and ("**Dimension.**" in l or "**档位。**" in l)),
        None,
    )
    if start is None:
        raise SystemExit(
            "render_standards: no `> **Dimension.**` / `> **档位。**` block in the rules file — "
            "add the one-line pointer first (CONVENTIONS.md §4)"
        )
    end = start
    while end < len(lines) and lines[end].startswith(">"):
        end += 1
    rest = lines[end:]
    while rest and not rest[0].strip():
        rest.pop(0)
    new = lines[:end] + ["", block, ""] + rest
    return "\n".join(new).rstrip("\n") + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=".", help="repository root (default: .)")
    ap.add_argument("--file", default="AGENTS.md",
                    help="rules file to render into (default: AGENTS.md; bilingual pair: AGENTS.zh-CN.md)")
    ap.add_argument("--write", action="store_true", help="write the block into the rules file")
    ap.add_argument("--check", action="store_true", help="fail if the block is stale (default)")
    ap.add_argument("--verify-lock", action="store_true",
                    help="compare the block with standards.lock (CI; needs no ~/.agents); "
                         "a missing lock is skipped, not failed")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    agents_path = root / args.file
    if not agents_path.is_file():
        print(f"[render_standards] {root}: no {args.file}")
        return 1

    agents = agents_path.read_text(encoding="utf-8")
    dimension = detect_dimension(agents)
    if dimension is None:
        print(f"[render_standards] {root}: {args.file} has no `> **Dimension.** A|B|C` block")
        return 1

    # CI path: compare the block with the lock only. No ~/.agents needed, so a runner can do it;
    # a checkout without a lock is skipped rather than failed (not every repository has migrated).
    if args.verify_lock:
        lock = read_lock(root)
        if not lock:
            print(f"[render_standards] {root}: no {LOCK_NAME} — skipped")
            return 0
        want = lock.get(args.file)
        if not want:
            print(f"[render_standards] {root}: {args.file} not in {LOCK_NAME} — skipped")
            return 0
        block = extract_block(agents)
        if block is None:
            print(f"[render_standards] {root}: {args.file} has no standards-core block")
            return 1
        if block_digest(block) != want:
            print(f"[render_standards] {root}: {args.file} block differs from {LOCK_NAME}.")
            print("  Re-render it on the machine that has the standard:"
                  " python3 ~/.agents/scripts/render_standards.py --write .")
            return 1
        if STANDARDS.is_dir():
            src = lock.get("source")
            if src and src != dirhash(STANDARDS):
                print(f"[render_standards] {root}: block matches the lock, but the lock predates"
                      " the current standard — re-render to pick up the change")
                return 1
        print(f"[render_standards] {root}: block matches {LOCK_NAME}")
        return 0

    expected = render(dimension)

    if BEGIN in agents:
        start = agents.index(BEGIN)
        end = agents.index(END, start) + len(END)
        while end < len(agents) and agents[end] != "\n":
            end += 1
        current = agents[start:end].rstrip("\n")
        if current == expected:
            if args.write:
                write_lock(root, {args.file: block_digest(expected)})
            print(f"[render_standards] {root}: in sync (dimension {dimension.upper()})")
            return 0
        if args.write:
            agents_path.write_text(agents[:start] + expected + agents[end:], encoding="utf-8")
            write_lock(root, {args.file: block_digest(expected)})
            print(f"[render_standards] {root}: block rewritten (dimension {dimension.upper()})")
            return 0
        print(f"[render_standards] {root}: STALE — the block differs from the standard.")
        print("  regenerate: python3 ~/.agents/scripts/render_standards.py --write .")
        return 1

    if args.write:
        agents_path.write_text(insert_block(agents, expected), encoding="utf-8")
        write_lock(root, {args.file: block_digest(expected)})
        print(f"[render_standards] {root}: block inserted (dimension {dimension.upper()})")
        return 0
    print(f"[render_standards] {root}: no standards-core block — insert it with --write")
    return 1


if __name__ == "__main__":
    sys.exit(main())
