#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_language.py — dimension A's language guard.

Implements the two A-only rules in `~/.agents/standards/dimensions/a.md`
section 1 that previously had no program check behind them:

  1. Everything outside the whitelist is English. A non-whitelisted `.md`
     carrying Chinese prose is a defect.
  2. Commit messages are English — subject *and* body.

The default mode reads only this repository: its tracked `.md` files and its own
git log. A CI runner therefore needs no `~/.agents` and no network. `--all`
additionally walks every A-tier repository from `STANDARD.md` section 0 and
checks that each guard copy is byte-identical to this canonical script.

Deliberately not judged (the standard says so):

  * Bilingual pairs — the `.zh-CN.md` side of README / MAINTENANCE /
    INVARIANTS / AGENTS.
  * Quoted literals — inline code, code fences, Markdown link targets, HTML
    tags, double-quoted strings, and switcher / anchor lines naming a
    `.zh-CN.md` page. The surrounding prose is still checked.
  * The generated `standards-core` block in AGENTS.md.
  * Third-party / machine-generated trees.

Known, accepted deviations are named explicitly below rather than hidden in
the scanner. Historic ledgers and dated audit snapshots state what was true as
of their own commit; section 1 and every repo's CHANGELOG format say old
entries are never re-verified and never rewritten. Historic Chinese commit
messages are likewise not rewritten, so the commit window defaults to the
latest commit (HEAD~1..HEAD) — the guard stops new ones, it does not litigate
old ones. Every one of these is recorded in the canonical CHANGELOG.

Exit 0 = clean, 1 = violations (each printed with file:line or commit sha).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")

# section 1: the whitelist is the bilingual pair. The English side stays
# English; its .zh-CN.md partner is the one allowed to be Chinese.
WHITELIST_NAMES = {
    "README.zh-CN.md", "MAINTENANCE.zh-CN.md",
    "INVARIANTS.zh-CN.md", "AGENTS.zh-CN.md",
}

# "Not judged": third-party / machine-generated content.
NOT_JUDGED_DIRS = (
    "po/", "locale/", "screenshots/", "thirdparty/",
    "docs/reports/", "reports/", "node_modules/",
)

# Historic, frozen by their own commit: append-only ledgers and dated audit
# snapshots. A future file that needs this treatment has to be named here.
HISTORIC_NAMES = {"CHANGELOG.md"}
HISTORIC_DIRS = ("docs/audit/",)

# section 1 names this repository as the product-content case: its product is
# bilingual by design, so product documentation and fixtures may be Chinese.
# The front door is NOT relaxed — README / MAINTENANCE / INVARIANTS pairs and
# AGENTS.md sit at the root and are still scanned.
PRODUCT_CONTENT_DOCS = {"agent-os": ("docs/", "integrations/")}

INLINE_CODE = re.compile(r"`[^`]*`")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]*>")
QUOTED = re.compile(r'"[^"]*"')


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def strip_literals(line):
    """Remove verbatim literals section 1 allows, leaving only prose."""
    line = INLINE_CODE.sub("", line)
    line = LINK_TARGET.sub("]", line)
    line = QUOTED.sub('""', line)
    line = HTML_TAG.sub("", line)
    return line


def is_skipped(rel, repo_name):
    base = rel.rsplit("/", 1)[-1]
    if base in WHITELIST_NAMES or base in HISTORIC_NAMES:
        return True
    if any(rel.startswith(p) or "/" + p in rel for p in NOT_JUDGED_DIRS):
        return True
    if any(rel.startswith(p) for p in HISTORIC_DIRS):
        return True
    if repo_name in PRODUCT_CONTENT_DOCS:
        if any(rel.startswith(p) for p in PRODUCT_CONTENT_DOCS[repo_name]):
            return True
    return False


def md_problems(root):
    repo_name = root.name
    problems = []
    files = [f for f in git(root, "ls-files", "*.md").stdout.splitlines() if f]
    for rel in files:
        if is_skipped(rel, repo_name):
            continue
        try:
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append("%s: cannot read (%s)" % (rel, exc))
            continue
        in_block = in_code = False
        for number, line in enumerate(lines, 1):
            if "BEGIN standards-core" in line:
                in_block = True
                continue
            if "END standards-core" in line:
                in_block = False
                continue
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_block or in_code:
                continue
            # A language switcher or an anchor into the Chinese page is a
            # quoted literal, not prose.
            if ".zh-CN.md" in line:
                continue
            prose = strip_literals(line)
            if CJK.search(prose):
                problems.append(
                    "%s:%d carries Chinese prose outside a quoted literal: %s"
                    % (rel, number, prose.strip()[:90])
                )
    return problems, len(files)


def commit_problems(root, since):
    problems = []
    result = git(root, "log", "--format=%H%x00%B%x00", "%s..HEAD" % since)
    if result.returncode != 0:
        # A repository with a single commit has no HEAD~1.
        result = git(root, "log", "-1", "--format=%H%x00%B%x00")
    parts = result.stdout.split("\0")
    count = 0
    for index in range(0, len(parts) - 1, 2):
        sha, body = parts[index].strip(), parts[index + 1]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        count += 1
        if CJK.search(body):
            first = body.strip().splitlines()[0] if body.strip() else ""
            problems.append(
                "%s: commit message is not English: %s" % (sha[:7], first[:90])
            )
    return problems, count


def run_one(root, since):
    md, file_count = md_problems(root)
    commits, commit_count = commit_problems(root, since)
    return md + commits, file_count, commit_count


def a_tier_repositories():
    """Parse the first (A-tier) table in STANDARD.md section 0."""
    std = Path.home() / ".agents" / "standards" / "STANDARD.md"
    if not std.is_file():
        return None
    text = std.read_text(encoding="utf-8")
    if "## 0." not in text:
        return None
    section = text.split("## 0.", 1)[1].split("## 1.", 1)[0]
    paths, seen_header = [], False
    for line in section.splitlines():
        if line.startswith("| Repo | Kind |"):
            if seen_header:
                break
            seen_header = True
            continue
        match = re.match(r"^\| `([^`]+)` \|", line)
        if match and seen_header:
            paths.append(Path(os.path.expanduser(match.group(1))))
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=None,
                        help="repository root (default: current directory)")
    parser.add_argument("--since", default="HEAD~1",
                        help="commit-message window start (default: HEAD~1)")
    parser.add_argument("--all", action="store_true",
                        help="walk every A-tier repo and check the guard copies")
    args = parser.parse_args()

    if args.all:
        repos = a_tier_repositories()
        if not repos:
            print("[check_language] FAIL: cannot read the A-tier table from "
                  "~/.agents/standards/STANDARD.md section 0")
            return 1
        canonical = Path(__file__).resolve().read_bytes()
        all_problems = []
        print("[check_language] %d A-tier repositories" % len(repos))
        for repo in repos:
            label = repo.name
            if not repo.is_dir():
                all_problems.append("%s: repository directory is missing" % label)
                continue
            copy = repo / "scripts" / "check_language.py"
            if not copy.is_file():
                all_problems.append("%s: scripts/check_language.py is missing" % label)
            elif copy.read_bytes() != canonical:
                all_problems.append(
                    "%s: scripts/check_language.py differs from the canonical script" % label
                )
            problems, nf, nc = run_one(repo, args.since)
            for problem in problems:
                all_problems.append("%s: %s" % (label, problem))
            if not problems:
                print("  OK  %-24s %d .md, %d commit(s)" % (label, nf, nc))
        if all_problems:
            print("[check_language] FAIL (%d problem(s)):" % len(all_problems))
            for problem in all_problems:
                print("  - %s" % problem)
            return 1
        print("[check_language] PASS — %d repositories clean" % len(repos))
        return 0

    root = Path(args.root).resolve() if args.root else Path.cwd()
    problems, nf, nc = run_one(root, args.since)
    print("[check_language] %s" % root)
    if problems:
        print("[check_language] FAIL (%d problem(s)):" % len(problems))
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print("[check_language] PASS — %d .md, %d commit(s) in %s..HEAD"
          % (nf, nc, args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
