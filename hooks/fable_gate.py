#!/usr/bin/env python3
"""fable_gate.py — PreToolUse hard gate for FableFuse orchestrator mode.

Inert unless the session is armed (`fable-dispatch arm`) and guards are on. When armed, the brain
must not execute/mutate directly — it delegates to the body. Blocks file-mutation tools and
non-allowlisted Bash, steering to `fable-dispatch`. Read-only inspection stays allowed (the brain
reads and reasons). Narrowed per council review: mutation tools + a Bash allowlist, not a blanket
"heavy Bash" block. Denies via the Claude Code JSON permission decision.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fable_common as fc

BLOCK_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
DEFAULT_ALLOW = ("fable-dispatch,fable_dispatch.py,ask-fable,fable_advisor.py,fable_verify.py,"
                 "codex,git status,git diff,git log,git show,ls,cat,rg,"
                 "grep,find,head,tail,wc,pwd,echo")
# Shell metacharacters that would let an allowlisted prefix chain into an arbitrary command
# (e.g. "git status && rm -rf /"). Bash tool calls run the full string through a shell, so a
# prefix check alone is not enough — reject anything that isn't a single simple command.
# Checked against the ORIGINAL (un-normalized) string — normalization below never widens this.
_DANGEROUS_SHELL_TOKENS = (";", "&&", "||", "|", "`", "$(", "\n", "\r", ">", "<", "&")

# Real dogfooding (not synthetic tests) showed agents naturally invoke allowlisted scripts as
# `python3 fable_dispatch.py ...` and sometimes prefix a scoped env var (`FOO=bar cmd`) — neither
# form matched a literal prefix check, so a just-armed session immediately fought its own brain on
# the most natural invocations. Strip a leading interpreter and simple env-var assignments before
# the allowlist comparison (metacharacter check above still runs on the untouched original).
_LEADING_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
_LEADING_INTERPRETER_RE = re.compile(r"^(python3?)\s+")


def _normalized_for_allowlist(cmd: str) -> str:
    prev = None
    while prev != cmd:
        prev = cmd
        cmd = _LEADING_ENV_RE.sub("", cmd)
    return _LEADING_INTERPRETER_RE.sub("", cmd)
MSG = ("FableFuse hard gate: the brain does not execute/mutate directly. Delegate to the body via "
       "`fable-dispatch \"<spec: goal, paths, constraints, non-goals, proof command>\"` (or "
       "--parallel), then verify with `fable-dispatch verify --gate \"<tests/build/lint>\"`. "
       "Tiny (<~20-line) obvious edits: set FABLE_GATE_ALLOW_TRIVIAL=1. Kill-switch: FABLE_GUARDS_OFF=1.")


def _allow() -> None:
    sys.exit(0)


def _is_simple_command(cmd: str) -> bool:
    """True only if `cmd` has no shell metacharacters that could chain/redirect/substitute past
    an allowlisted prefix. Read-only allowlisted commands never legitimately need these."""
    return bool(cmd) and not any(tok in cmd for tok in _DANGEROUS_SHELL_TOKENS)


def _deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}))
    sys.exit(0)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if fc.guards_off():
        _allow()
    sid = data.get("session_id") or "default"
    if not fc.read_state(sid).get("armed"):
        _allow()
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool in BLOCK_TOOLS:
        if fc._as_bool(os.environ.get("FABLE_GATE_ALLOW_TRIVIAL")):
            _allow()
        _deny(f"{tool} blocked. {MSG}")
    if tool == "Bash":
        cmd = (ti.get("command") or "").strip()
        allow = [a.strip() for a in os.environ.get("FABLE_BASH_ALLOW", DEFAULT_ALLOW).split(",") if a.strip()]
        # metacharacter check ALWAYS runs on the raw string; normalization only strips a leading
        # interpreter/env-var prefix for the allowlist comparison, never widens what's dangerous.
        normalized = _normalized_for_allowlist(cmd)
        if _is_simple_command(cmd) and any(normalized == a or normalized.startswith(a + " ") for a in allow):
            _allow()
        _deny(f"Bash blocked: {cmd[:60]!r}. {MSG}")
    _allow()  # Read / Grep / Glob / Task / MCP reads / etc. — the brain may inspect freely


if __name__ == "__main__":
    main()
