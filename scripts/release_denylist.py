#!/usr/bin/env python3
"""Shared tracked-artifact denylist for public-release scrub and pre-push.

Conservative path-only rules. Ordinary docs that merely mention words like
\"transcript\" in prose or filenames without artifact extensions must not match.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Ordered high-confidence path patterns (applied with re.search on repo-relative paths).
# Keep in sync with repository .gitignore families (see --expected-gitignore-needles).
DENY_PATH_REGEXES: tuple[str, ...] = (
    r"(^|/)runs/",
    r"(^|/)verdict\.json$",
    r"(^|/)__pycache__/",
    r"\.pyc$",
    r"(^|/)\.omc/",
    r"(^|/)\.omx/",
    r"(^|/)\.grokprint/",
    r"(^|/)\.buildlog/",
    # dotenv / local secrets (not .env.example)
    r"(^|/)\.env$",
    r"(^|/)\.env\.[^/]+$",
    r"(^|/)\.envrc$",
    r"(^|/)env\.local$",
    # credential / key material filenames
    r"(^|/)[^/]*credentials?[^/]*\.(json|ya?ml|toml|env)$",
    r"(^|/)[^/]*secrets?[^/]*\.(json|ya?ml|toml|env)$",
    r"(^|/)id_(rsa|ed25519|ecdsa)(?:_[^/]+)?$",
    r"\.(pem|p12|pfx)$",
    # quarantine / private dumps
    r"(^|/)[^/]*\.quarantine$",
    r"(^|/)quarantine/",
    # provider logs / transcript *artifacts* (extension or dir), not prose docs
    r"(^|/)provider[-_][^/]*\.(log|jsonl)$",
    r"(^|/)[^/]*[-_]provider\.(log|jsonl)$",
    r"(^|/)transcripts?/",
    r"(^|/)[^/]+\.transcript$",
    r"(^|/)[^/]+\.transcript\.(json|jsonl|log|txt)$",
    r"(^|/)[^/]*transcript[^/]*\.(log|json|jsonl)$",
)

DENY_PATH_RE = re.compile("|".join(f"(?:{p})" for p in DENY_PATH_REGEXES))

# Families that .gitignore must cover portably (substring needles, not full regexes).
GITIGNORE_NEEDLES: tuple[str, ...] = (
    "runs/",
    "verdict.json",
    "__pycache__/",
    "*.pyc",
    ".omc/",
    ".omx/",
    ".grokprint/",
    ".buildlog/",
    ".env",
    "*.pem",
    "id_rsa",
    "id_ed25519",
    "quarantine",
    "transcripts/",
    "*.transcript",
)


def _normalize_repo_path(path: str) -> str:
    """Normalize repo-relative paths without stripping leading-dot dirnames.

    Do not use str.lstrip('./'): that removes any mix of '.' and '/' characters
    and would turn '.omc/x' into 'omc/x' or '.env' into 'env'.
    """
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def is_forbidden_path(path: str) -> bool:
    """Return True if a repo-relative path must not be tracked."""
    normalized = _normalize_repo_path(path)
    if not normalized:
        return False
    # Never treat the checked-in example env templates as forbidden.
    base = Path(normalized).name
    if base in {".env.example", ".env.sample", ".env.template"}:
        return False
    return DENY_PATH_RE.search(normalized) is not None


def combined_grep_pattern() -> str:
    """Single extended-regex string suitable for `grep -E` over path lists."""
    return "|".join(f"({p})" for p in DENY_PATH_REGEXES)


def tracked_files(cwd: Path | None = None) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        errors="ignore",
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def forbidden_tracked_paths(cwd: Path | None = None) -> list[str]:
    return [path for path in tracked_files(cwd) if is_forbidden_path(path)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared release artifact denylist helpers.")
    parser.add_argument(
        "--check-tracked",
        action="store_true",
        help="print forbidden tracked paths (one per line); exit 1 if any",
    )
    parser.add_argument(
        "--grep-e",
        action="store_true",
        help="print combined grep -E pattern for path lists",
    )
    parser.add_argument(
        "--expected-gitignore-needles",
        action="store_true",
        help="print .gitignore needles that must stay aligned with this denylist",
    )
    args = parser.parse_args(argv)

    if args.grep_e:
        print(combined_grep_pattern())
        return 0
    if args.expected_gitignore_needles:
        for needle in GITIGNORE_NEEDLES:
            print(needle)
        return 0
    if args.check_tracked:
        bad = forbidden_tracked_paths()
        for path in bad:
            print(path)
        return 1 if bad else 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
