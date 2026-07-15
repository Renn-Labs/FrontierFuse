#!/usr/bin/env python3
"""Public-release scrub scanner.

The scanner is intentionally conservative and non-leaky: it reports only scope, path,
line, and finding type. It never prints the matched value.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import release_denylist as denylist  # noqa: E402


@dataclass(frozen=True)
class Finding:
    scope: str
    path: str
    line: int
    kind: str


# High-confidence content detectors. Patterns must not require printing the match.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GCP_API_KEY", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    (
        "OPENAI_ANTHROPIC_OR_OPENROUTER_KEY",
        re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9\-_]{20,}\b"),
    ),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    # Additional common provider token prefixes (high-confidence shape only).
    ("HF_TOKEN", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("NPM_TOKEN", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    ("PYPI_TOKEN", re.compile(r"\bpypi-[A-Za-z0-9_\-]{20,}\b")),
    ("XAI_TOKEN", re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b")),
    # dotenv / export style: OPENAI_API_KEY=..., export ANTHROPIC_API_KEY='...', TOKEN=...
    # Value charset is secret-like (no '.' / '(') so code such as TOKEN = re.compile(...) is ignored.
    (
        "DOTENV_SECRET_ASSIGNMENT",
        re.compile(
            r"(?i)^\s*(?:export\s+)?"
            r"[A-Za-z_]*"
            r"(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE[_-]?KEY)"
            r"[A-Za-z0-9_]*\s*=\s*['\"]?"
            r"[A-Za-z0-9+_/=-]{8,}"
        ),
    ),
    # Inline assignments, including names like MY_API_KEY / service_token (not bare prose).
    (
        "SECRET_ASSIGNMENT",
        re.compile(
            r"(?i)(?:^|[\s,;{])(?:export\s+)?"
            r"(?:[A-Za-z][A-Za-z0-9_]*_)?"
            r"(?:api[_-]?key|secret|passwd|password|token|access[_-]?key|credentials?)\b"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_=\-]{12,}"
        ),
    ),
    ("URL_CREDENTIALS", re.compile(r"https?://[^/\s:@]+:[^@\s]+@")),
    ("PRIVATE_ABSOLUTE_PATH", re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+")),
)

OPAQUE_TOKEN = re.compile(r"\b[A-Za-z0-9+/_\-]{32,}={0,2}\b")
SECRET_WORD = re.compile(r"(?i)(secret|token|api[_-]?key|password|passwd|credential|private)")


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="ignore", check=check)


def git_text(args: list[str], *, check: bool = True) -> str:
    return run_git(args, check=check).stdout


def shannon(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    size = len(value)
    return -sum((count / size) * math.log2(count / size) for count in counts.values())


def is_binaryish(text: str) -> bool:
    if "\0" in text:
        return True
    if not text:
        return False
    sample = text[:4096]
    control = sum(1 for char in sample if ord(char) < 32 and char not in "\n\r\t\f\b")
    return control > max(20, len(sample) // 20)


def format_finding(finding: Finding) -> str:
    """Non-leaky single-line finding (scope/path/line/kind only)."""
    line = f":{finding.line}" if finding.line else ""
    return f"- {finding.scope}:{finding.path}{line}: {finding.kind}"


def scan_text(scope: str, path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    if is_binaryish(text):
        return findings
    for line_no, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(Finding(scope, path, line_no, kind))
        if SECRET_WORD.search(line):
            for match in OPAQUE_TOKEN.finditer(line):
                token = match.group(0)
                # Python/shell identifiers in test names and code are not opaque values.
                # Prefix detectors and assignment detectors still cover named provider secrets.
                if token.isidentifier():
                    continue
                if shannon(token) >= 3.6:
                    findings.append(Finding(scope, path, line_no, "HIGH_ENTROPY_SECRET_CONTEXT"))
                    break
    return findings


def tracked_files() -> list[str]:
    return [line for line in git_text(["ls-files"]).splitlines() if line]


def scan_worktree() -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_files():
        if denylist.is_forbidden_path(path):
            findings.append(Finding("worktree", path, 0, "TRACKED_PRIVATE_OR_GENERATED_PATH"))
            continue
        try:
            text = Path(path).read_text(errors="ignore")
        except OSError:
            findings.append(Finding("worktree", path, 0, "WORKTREE_FILE_READ_ERROR"))
            continue
        findings.extend(scan_text("worktree", path, text))
    return findings


def upstream_ref() -> str | None:
    proc = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False)
    if proc.returncode != 0:
        return None
    ref = proc.stdout.strip()
    return ref or None


def commits_for_push_range() -> list[str]:
    upstream = upstream_ref()
    if upstream:
        spec = f"{upstream}..HEAD"
    else:
        spec = "HEAD"
    return [line for line in git_text(["rev-list", spec]).splitlines() if line]


def commits_for_all_history() -> list[str]:
    return [line for line in git_text(["rev-list", "--all"]).splitlines() if line]


def show_file(commit: str, path: str) -> str | None:
    proc = run_git(["show", f"{commit}:{path}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def commit_message(commit: str) -> str | None:
    proc = run_git(["log", "-1", "--format=%B", commit], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def tree_paths(commit: str) -> list[str] | None:
    proc = run_git(["ls-tree", "-r", "--name-only", commit], check=False)
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def scan_commits(commits: list[str], scope_name: str) -> list[Finding]:
    findings: list[Finding] = []
    for commit in commits:
        short = commit[:12]
        scope = f"{scope_name}:{short}"
        # Commit subjects/bodies can carry leaked tokens; scan without printing values.
        msg = commit_message(commit)
        if msg is None:
            findings.append(Finding(scope, "COMMIT_MESSAGE", 0, "HISTORY_COMMIT_MESSAGE_READ_ERROR"))
        elif msg:
            findings.extend(scan_text(scope, "COMMIT_MESSAGE", msg))
        files = tree_paths(commit)
        if files is None:
            findings.append(Finding(scope, "TREE", 0, "HISTORY_TREE_READ_ERROR"))
            continue
        for path in files:
            if denylist.is_forbidden_path(path):
                findings.append(Finding(scope, path, 0, "TRACKED_PRIVATE_OR_GENERATED_PATH"))
                continue
            text = show_file(commit, path)
            if text is None:
                findings.append(Finding(scope, path, 0, "HISTORY_FILE_READ_ERROR"))
            elif text:
                findings.extend(scan_text(scope, path, text))
    return findings


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[Finding] = set()
    out: list[Finding] = []
    for finding in findings:
        if finding in seen:
            continue
        seen.add(finding)
        out.append(finding)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan public-release files/history for scrub candidates.")
    parser.add_argument("--push-range", action="store_true", help="scan commits that would be pushed upstream")
    parser.add_argument("--all-history", action="store_true", help="scan every commit reachable from refs")
    args = parser.parse_args()

    try:
        findings = scan_worktree()
        if args.push_range:
            findings.extend(scan_commits(commits_for_push_range(), "push"))
        if args.all_history:
            findings.extend(scan_commits(commits_for_all_history(), "history"))
    except (OSError, subprocess.SubprocessError):
        print("public-release-scrub: FAIL", file=sys.stderr)
        print("Git inspection failed; release is blocked until the scrub can complete.", file=sys.stderr)
        return 2
    findings = dedupe(findings)

    if findings:
        print("public-release-scrub: FAIL", file=sys.stderr)
        print("Matched values are intentionally not printed.", file=sys.stderr)
        for finding in findings:
            print(format_finding(finding), file=sys.stderr)
        print("Scrub the file/history, rotate any real exposed secret, then rerun this scanner.", file=sys.stderr)
        return 1

    print("public-release-scrub: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
