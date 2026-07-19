#!/usr/bin/env python3
"""frontier_gate.py - PreToolUse workflow guardrail for FrontierFuse orchestrator mode.

Inert unless the session is armed (`frontier-dispatch arm`) and guards are on. When armed, the brain
must not execute/mutate directly — it delegates to the body. Blocks file-mutation tools and
non-allowlisted Bash, steering to `frontier-dispatch`. Read-only inspection stays allowed (the brain
reads and reasons). Narrowed per council review: mutation tools + a Bash command policy, not a
blanket "heavy Bash" block. Denies via the Claude Code JSON permission decision.

Direct codex/grok/claude/gemini body CLIs are denied while armed, as is
`frontier-dispatch disarm`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frontier_common as fc

BLOCK_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Narrow explicit allowlist of read-only / inspection tools the controller may use while armed.
# Names match official Claude Code tools (https://code.claude.com/docs/en/tools). Everything else
# fails closed (workflow guardrail, not a sandbox — host can kill-switch/disarm).
# Do NOT allow broad dynamic MCP tool prefixes (mcp__server__*); those tools may mutate. Only the
# dedicated MCP resource list/read tools are permitted. Obsolete names (LS, TodoRead,
# ListMcpResources, ReadMcpResource) are intentionally omitted — not present in current docs.
ALLOW_READONLY_TOOLS = frozenset({
    "Read",
    "Grep",
    "Glob",
    "WebSearch",
    "WebFetch",
    "LSP",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "ToolSearch",
})

# Shell metacharacters that would let an allowlisted command chain into an arbitrary one
# (e.g. "git status && rm -rf /"). Bash tool calls run the full string through a shell, so
# argv validation alone is not enough — reject anything that isn't a single simple command.
# Checked against the ORIGINAL (un-normalized) string.
_DANGEROUS_SHELL_TOKENS = (";", "&&", "||", "|", "`", "$(", "\n", "\r", ">", "<", "&")

# Real dogfooding showed agents invoke scripts as `python3 frontier_dispatch.py ...`. Python script
# invocation remains supported, but leading environment assignments are denied while armed because
# variables such as GIT_EXTERNAL_DIFF and provider-specific overrides can execute arbitrary code.
_LEADING_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")

# Direct body/host CLIs and common wrapper/mutator binaries — never allowed while armed.
_DENIED_BINARIES = frozenset({
    "codex", "grok", "claude", "gemini",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
    "sudo", "doas", "su",
    "env", "xargs", "nohup", "nice", "timeout", "stdbuf", "script",
    "eval", "exec", "source",
    "perl", "ruby", "node", "nodejs", "php", "lua", "deno", "bun",
    "curl", "wget", "nc", "ncat", "netcat",
    "chmod", "chown", "rm", "rmdir", "mv", "cp", "dd", "truncate", "tee", "install",
    "kill", "pkill", "killall",
    "python2", "ipython", "pip", "pip3",
})

_READONLY_BINARIES = frozenset({
    "ls", "cat", "rg", "grep", "head", "tail", "wc", "pwd", "echo",
})

_GIT_READONLY_SUBS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "describe", "ls-files", "ls-tree",
    "cat-file", "blame", "show-ref",
})

_FIND_DANGEROUS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
})

# Loop-safe frontier-dispatch subcommands while the controller is armed.
# disarm is an explicit host-side escape — never from the model tool path.
# arm is unnecessary once armed and is denied so the model cannot re-toggle gate state.
# install-hooks / uninstall-hooks mutate host settings — denied.
_DISPATCH_ALLOWED_SUBS = frozenset({
    "dispatch", "verify", "done", "config", "models", "doctor",
})
_DISPATCH_DENIED_SUBS = frozenset({
    "arm", "disarm", "update", "install-hooks", "uninstall-hooks",
})
_DISPATCH_ALL_SUBS = _DISPATCH_ALLOWED_SUBS | _DISPATCH_DENIED_SUBS

_HELP_FLAGS = frozenset({"-h", "--help"})
_PROJECT_DISPATCH_NAMES = frozenset({"frontier-dispatch", "frontier_dispatch.py"})
_PROJECT_VERIFY_NAMES = frozenset({"frontier_verify.py"})
_PYTHON_INTERPRETERS = frozenset({"python", "python3"})

# Flags accepted on the shared frontier-dispatch argparse surface for allowed subcommands.
_DISPATCH_FLAGS_WITH_VALUE = frozenset({"--timeout", "--budget-usd", "--fanout"})
_DISPATCH_FLAGS_BOOL = frozenset({"--parallel", "-p", "--dry-run"})

MSG = ("FrontierFuse workflow guardrail: the controller does not execute/mutate directly. Delegate to the body via "
       "`frontier-dispatch \"<spec: goal, paths, constraints, non-goals, proof command>\"` (or "
       "--parallel), then run the verification command frozen by the host with `frontier-dispatch verify`. "
       "Tiny (<~20-line) obvious edits: set FRONTIER_GATE_ALLOW_TRIVIAL=1. Kill-switch: FRONTIER_GUARDS_OFF=1.")


def _allow() -> None:
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}))
    sys.exit(0)


def _has_dangerous_shell(cmd: str) -> bool:
    """True if the raw command string can chain, substitute, or redirect past argv policy."""
    if not cmd:
        return True
    if any(tok in cmd for tok in _DANGEROUS_SHELL_TOKENS):
        return True
    # Control characters other than tab/space (newlines already covered above).
    if any(ord(c) < 32 and c != "\t" for c in cmd):
        return True
    return False


def _strip_leading_env(cmd: str) -> str:
    prev = None
    while prev != cmd:
        prev = cmd
        cmd = _LEADING_ENV_RE.sub("", cmd)
    return cmd


def _basename(token: str) -> str:
    return token.rstrip("/").replace("\\", "/").split("/")[-1]


def _has_traversal(token: str) -> bool:
    if not token:
        return False
    normalized = token.replace("\\", "/")
    if normalized.startswith("~"):
        return True
    return ".." in normalized.split("/")


def _is_safe_project_script(token: str, allowed_basenames: frozenset[str]) -> bool:
    """Allow bare or relative project script paths; reject absolute paths and traversal."""
    if not token or _has_traversal(token):
        return False
    if token.startswith("/") or (len(token) > 1 and token[1] == ":"):
        return False
    return _basename(token) in allowed_basenames


def _parse_argv(cmd: str) -> list[str] | None:
    stripped = _strip_leading_env(cmd.strip())
    if not stripped:
        return None
    # Reject a leading bare `source`/`.` that shlex would treat as argv0.
    try:
        argv = shlex.split(stripped, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    if argv[0] in (".", "source"):
        return None
    return argv


def _extra_allow_basenames() -> frozenset[str]:
    """Optional FRONTIER_BASH_ALLOW: cautious exact-basename extensions only (not prefixes).

    Entries with spaces or path separators are ignored — whole-command / prefix overrides are
    no longer honored. Denied binaries can never be re-enabled through this env var.
    """
    raw = os.environ.get("FRONTIER_BASH_ALLOW")
    if raw is None or not raw.strip():
        return frozenset()
    out: set[str] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p or " " in p or "/" in p or "\\" in p or p in _DENIED_BINARIES:
            continue
        base = _basename(p)
        if base and base not in _DENIED_BINARIES:
            out.add(base)
    return frozenset(out)


def _flags_with_values_ok(
    args: list[str],
    *,
    with_value: frozenset[str],
    boolean: frozenset[str],
    allow_positionals: bool,
    required_value_flags: frozenset[str] | None = None,
) -> bool:
    seen_required: set[str] = set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in _HELP_FLAGS or a in boolean:
            i += 1
            continue
        if a in with_value:
            if i + 1 >= len(args):
                return False
            if a in ("--cwd", "--fanout", "--session") and _has_traversal(args[i + 1]):
                return False
            if required_value_flags and a in required_value_flags:
                seen_required.add(a)
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            flag, _, val = a.partition("=")
            if flag not in with_value:
                return False
            if flag in ("--cwd", "--fanout", "--session") and _has_traversal(val):
                return False
            if required_value_flags and flag in required_value_flags:
                seen_required.add(flag)
            i += 1
            continue
        if a.startswith("-"):
            return False
        if not allow_positionals:
            return False
        i += 1
    if required_value_flags:
        # --help alone is fine without required flags
        if args and all(a in _HELP_FLAGS for a in args):
            return True
        return required_value_flags <= seen_required
    return True


def _dispatch_argv_allowed(args: list[str], approved_gate: dict | None = None) -> bool:
    if not args:
        return False
    if args[0] in _HELP_FLAGS or args[0] == "help":
        return True

    if args[0] in _DISPATCH_ALL_SUBS:
        sub = args[0]
        rest = args[1:]
    else:
        sub = "dispatch"
        rest = args

    if sub in _DISPATCH_DENIED_SUBS:
        return False
    if sub not in _DISPATCH_ALLOWED_SUBS:
        return False

    if sub == "done":
        return all(a in _HELP_FLAGS for a in rest)

    if sub == "doctor":
        return all(a in {*_HELP_FLAGS, "--json"} for a in rest)

    if sub == "verify":
        # The host freezes the verifier argv/workspace at arm time. The model may only invoke that
        # frozen command; it cannot supply a replacement through --gate or --cwd.
        if not isinstance(approved_gate, dict) or not isinstance(approved_gate.get("argv"), list):
            return False
        return not rest or all(a in _HELP_FLAGS for a in rest)

    if sub == "config":
        # Read-only effective-config inspection is allowed. Reconfiguration must happen before arm
        # through the explicit config skill or host CLI.
        return not rest or all(a in _HELP_FLAGS for a in rest)

    if sub == "models":
        return _flags_with_values_ok(
            rest,
            with_value=frozenset({"--provider"}),
            boolean=frozenset({"--no-discover", "--json", *_HELP_FLAGS}),
            allow_positionals=False,
        )

    # dispatch (explicit or default)
    return _flags_with_values_ok(
        rest,
        with_value=_DISPATCH_FLAGS_WITH_VALUE,
        boolean=_DISPATCH_FLAGS_BOOL,
        allow_positionals=True,
    )


def _python_invocation_allowed(argv: list[str], approved_gate: dict | None = None) -> bool:
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-c", "-m", "-"):
            return False
        if a.startswith("-c") or a.startswith("-m"):
            return False
        if a in ("-u", "-O", "-OO", "-B", "-I", "-S", "-s", "-E", "-P", "-q", "-v", "-VV"):
            i += 1
            continue
        if a in ("-W", "-X") and i + 1 < len(rest):
            i += 2
            continue
        if a.startswith("-W") or a.startswith("-X"):
            i += 1
            continue
        if a.startswith("-"):
            return False
        break
    if i >= len(rest):
        return False
    script = rest[i]
    script_args = rest[i + 1:]
    if _is_safe_project_script(script, _PROJECT_DISPATCH_NAMES):
        return _dispatch_argv_allowed(script_args, approved_gate)
    # Direct verifier execution would let the controller replace the host-frozen gate command.
    return False


# Global git options that cannot smuggle config/exec/path overrides.
_GIT_SAFE_GLOBAL_FLAGS = frozenset({
    "--no-pager", "--no-optional-locks", "--version", "-h", "--help",
})
# Subcommand flags that re-enable external programs or write output.
_GIT_DENIED_FLAGS = frozenset({
    "--output", "-o", "--ext-diff", "--textconv", "--external-diff",
    "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--upload-pack", "--receive-pack",
})


def _git_allowed(argv: list[str]) -> bool:
    """Read-only git only. Deny -c/config overrides, external diff drivers, and path escapes."""
    i = 1
    while i < len(argv):
        a = argv[i]
        # -c key=value (and --config) can set diff.external / core.editor → arbitrary exec.
        if a == "-c" or a == "--config" or a.startswith("-c") or a.startswith("--config="):
            return False
        if a in ("--git-dir", "--work-tree", "--namespace", "--exec-path"):
            return False
        if a.startswith("--git-dir=") or a.startswith("--work-tree=") or a.startswith("--namespace="):
            return False
        if a.startswith("--exec-path"):
            return False
        # -C <path> can redirect into unexpected trees; deny to keep inspection local.
        if a == "-C" or a.startswith("-C"):
            return False
        if a in _HELP_FLAGS or a == "--version":
            return True
        if a in _GIT_SAFE_GLOBAL_FLAGS:
            i += 1
            continue
        if a.startswith("-"):
            # Unknown global option — fail closed (no open-ended pass-through).
            return False
        break
    if i >= len(argv):
        return True
    sub = argv[i]
    if sub in _HELP_FLAGS:
        return True
    if sub not in _GIT_READONLY_SUBS:
        return False
    for a in argv[i + 1:]:
        if a in _GIT_DENIED_FLAGS:
            return False
        if any(a.startswith(p) for p in (
            "--output=", "--git-dir=", "--work-tree=", "--namespace=",
            "--exec-path=", "--upload-pack=", "--receive-pack=",
        )):
            return False
        # Reject embedded -c even after the subcommand (unusual but possible).
        if a == "-c" or a == "--config" or a.startswith("--config="):
            return False
    return True


def _find_allowed(argv: list[str]) -> bool:
    for a in argv[1:]:
        if a in _FIND_DANGEROUS:
            return False
        if a.startswith("-exec") or a.startswith("-ok"):
            return False
        # GNU find action predicates that write.
        if a in ("-fprint0", "-fprintf"):
            return False
    return True


def _argv_allowed(argv: list[str], approved_gate: dict | None = None) -> bool:
    head = argv[0]
    if _has_traversal(head):
        return False
    # No absolute executable paths — prevents /usr/bin/codex-style bypasses.
    if head.startswith("/") or (len(head) > 1 and head[1] == ":"):
        return False

    base = _basename(head)
    if base in _DENIED_BINARIES:
        return False

    if base in _PYTHON_INTERPRETERS:
        if head != base and not head.endswith(base):
            # relative path to a python binary is still the interpreter; allow by basename only
            pass
        return _python_invocation_allowed(argv, approved_gate)

    if base in _PROJECT_DISPATCH_NAMES:
        if not _is_safe_project_script(head, _PROJECT_DISPATCH_NAMES):
            return False
        return _dispatch_argv_allowed(argv[1:], approved_gate)

    if base in _PROJECT_VERIFY_NAMES:
        return False

    if base == "git":
        return _git_allowed(argv)

    if base == "find":
        return _find_allowed(argv)

    if base in _READONLY_BINARIES:
        return _readonly_tool_allowed(base, argv)

    extra = _extra_allow_basenames()
    if base in extra:
        # Extra allowlist entries are exact basenames only; refuse eval-style flags.
        if any(a in ("-c", "--command", "-e", "--eval") or a.startswith("-c") for a in argv[1:]):
            return False
        return True

    return False


def _readonly_tool_allowed(base: str, argv: list[str]) -> bool:
    """Extra flag denials for tools that can still exec or write via options."""
    # ripgrep/grep can run a preprocessor (--pre) or invoke PAGER-like helpers.
    if base in ("rg", "grep"):
        for a in argv[1:]:
            if a in ("--pre", "--pre-glob", "--hostname-bin") or a.startswith("--pre="):
                return False
            if a.startswith("--pre-glob="):
                return False
    return True


def bash_command_allowed(cmd: str, approved_gate: dict | None = None) -> bool:
    """Return True iff `cmd` is permitted under the armed-controller Bash policy.

    Pure function of the command string (+ optional FRONTIER_BASH_ALLOW). Does not consult
    session armed state or kill switches — callers apply those separately.
    """
    if not cmd or not str(cmd).strip():
        return False
    raw = str(cmd)
    if _has_dangerous_shell(raw):
        return False
    if _LEADING_ENV_RE.match(raw.strip()):
        return False
    argv = _parse_argv(raw)
    if argv is None:
        return False
    return _argv_allowed(argv, approved_gate)


# Back-compat name used by older contract imports / mental model.
def _is_simple_command(cmd: str) -> bool:
    return not _has_dangerous_shell(cmd)


def _normalized_for_allowlist(cmd: str) -> str:
    """Strip leading env assignments (legacy helper; policy now uses bash_command_allowed)."""
    return _strip_leading_env(cmd.strip())


def main() -> None:
    if fc.guards_off():
        _allow()
    try:
        data = json.load(sys.stdin)
    except Exception:
        _deny("FrontierFuse workflow guardrail: invalid hook input; denying safely.")
    if not isinstance(data, dict):
        _deny("FrontierFuse workflow guardrail: invalid hook input; expected a JSON object.")
    raw_sid = data.get("session_id")
    if raw_sid is not None and not fc.session_id_is_valid(raw_sid):
        _deny("FrontierFuse workflow guardrail: invalid hook input; session_id must be a string.")
    sid = raw_sid or "default"
    tool = data.get("tool_name", "")
    if not isinstance(tool, str):
        _deny("FrontierFuse workflow guardrail: invalid hook input; tool_name must be a string.")
    ti = data.get("tool_input", {})
    if ti is None:
        ti = {}
    if not isinstance(ti, dict):
        _deny("FrontierFuse workflow guardrail: invalid hook input; tool_input must be an object.")
    try:
        state = fc.read_state(sid)
    except fc.StateFileError:
        _deny(
            "FrontierFuse workflow guardrail: session state is invalid. Preserve it and run "
            "`frontier-dispatch config --repair` from the host before continuing."
        )
    except Exception:
        _deny(
            "FrontierFuse workflow guardrail: session state could not be validated safely; "
            "denying the tool call. Fix the state and lock paths from the host, then retry."
        )
    if not state.get("armed") and not state.get("completion_pending"):
        _allow()
    try:
        state = fc.reopen_after_blocked_stop(sid)
    except fc.StateFileError:
        _deny(
            "FrontierFuse workflow guardrail: session state is invalid. Preserve it and run "
            "`frontier-dispatch config --repair` from the host before continuing."
        )
    except Exception:
        _deny(
            "FrontierFuse workflow guardrail: session state could not be validated safely; "
            "denying the tool call. Fix the state and lock paths from the host, then retry."
        )
    if not state.get("armed"):
        _allow()
    if tool in BLOCK_TOOLS:
        if fc._as_bool(os.environ.get("FRONTIER_GATE_ALLOW_TRIVIAL")):
            _allow()
        _deny(f"{tool} blocked. {MSG}")
    if tool == "Bash":
        raw_cmd = ti.get("command") or ""
        if not isinstance(raw_cmd, str):
            _deny("FrontierFuse workflow guardrail: invalid hook input; command must be a string.")
        cmd = raw_cmd.strip()
        if bash_command_allowed(cmd, state.get("approved_gate")):
            _allow()
        _deny(f"Bash blocked: {cmd[:60]!r}. {MSG}")
    if tool in ALLOW_READONLY_TOOLS:
        _allow()
    # Armed fail-closed: mutating / unknown tool classes not on the narrow allowlist are denied.
    _deny(f"{tool or '(missing tool)'} blocked. {MSG}")


if __name__ == "__main__":
    main()
