#!/usr/bin/env python3
"""Standalone stdlib regression suite for the armed-controller Bash command policy (0.2.6).

Hostile and allowed cases for hooks/fable_gate.py. No live providers. Prints PASS only when
every assertion succeeds; exits non-zero on the first failure.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="fable-gate-sec-")
os.environ.setdefault("FABLE_CONFIG_DIR", str(Path(_TMP) / "config"))
os.environ.setdefault("FABLE_STATE_DIR", str(Path(_TMP) / "state"))
os.environ.setdefault("FABLE_RUNS_DIR", str(Path(_TMP) / "runs"))
os.environ["FABLE_CODEX_CMD"] = "echo"
os.environ["FABLE_ADVISOR_CMD"] = "echo"
# Clear optional allowlist so tests see the default structured policy.
os.environ.pop("FABLE_BASH_ALLOW", None)
os.environ.pop("FABLE_GUARDS_OFF", None)
os.environ.pop("CLAUDE_GUARDS_OFF", None)
os.environ.pop("FABLE_GATE_ALLOW_TRIVIAL", None)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fable_common as fc  # noqa: E402

GATE = ROOT / "hooks" / "fable_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("fable_gate_sec", GATE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {GATE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate_mod = _load_gate()
allowed = gate_mod.bash_command_allowed
APPROVED_GATE = {"gate": "true", "argv": ["true"], "cwd": str(ROOT)}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _assert_allowed(cmd: str, approved_gate: dict | None = None) -> None:
    _assert(allowed(cmd, approved_gate) is True, f"EXPECTED ALLOW: {cmd!r}")


def _assert_denied(cmd: str, approved_gate: dict | None = None) -> None:
    _assert(allowed(cmd, approved_gate) is False, f"EXPECTED DENY: {cmd!r}")


# --------------------------------------------------------------------------- #
# Pure policy: allowed loop-safe commands
# --------------------------------------------------------------------------- #
def test_allowed_loop_commands() -> None:
    for cmd in (
        "fable-dispatch --help",
        "fable-dispatch -h",
        "fable-dispatch help",
        "fable-dispatch doctor",
        "fable-dispatch done",
        "fable-dispatch config",
        'fable-dispatch "implement the fix with proof"',
        "fable-dispatch dispatch \"task one\"",
        "fable-dispatch --dry-run \"preview only\"",
        "fable-dispatch --parallel \"a\" \"b\"",
        "python3 fable_dispatch.py --help",
        "python3 fable_dispatch.py doctor",
        'python3 fable_dispatch.py "do the body work"',
        "python3 ./fable_dispatch.py done",
    ):
        _assert_allowed(cmd)
    _assert_allowed("fable-dispatch verify", APPROVED_GATE)
    _assert_allowed("python3 fable_dispatch.py verify", APPROVED_GATE)


def test_allowed_readonly_inspection() -> None:
    for cmd in (
        "git status",
        "git status -sb",
        "git diff",
        "git diff --stat",
        "git log -1 --oneline",
        "git show HEAD",
        "ls -la",
        "cat README.md",
        "rg TODO",
        "grep -n pattern file.py",
        "head -n 20 hooks/fable_gate.py",
        "tail -5 CHANGELOG.md",
        "wc -l fable_dispatch.py hooks/fable_gate.py fable_common.py",
        "pwd",
        "echo hello",
        "find . -name '*.py' -type f",
        "find . -maxdepth 2 -type d",
    ):
        _assert_allowed(cmd)


# --------------------------------------------------------------------------- #
# Pure policy: hostile / denied
# --------------------------------------------------------------------------- #
def test_deny_direct_body_clis() -> None:
    for cmd in (
        "codex exec --yolo 'rm -rf /'",
        "codex",
        "grok -p 'do stuff'",
        "grok",
        "claude -p 'hi'",
        "claude",
        "./codex exec",
        "bin/grok",
        "vendor/claude",
        "/usr/bin/codex",
        "/usr/local/bin/grok",
        "/private-path",
    ):
        _assert_denied(cmd)


def test_deny_disarm_and_gate_toggles() -> None:
    for cmd in (
        "fable-dispatch disarm",
        "python3 fable_dispatch.py disarm",
        "fable-dispatch arm",
        "python3 fable_dispatch.py arm",
        "fable-dispatch install-hooks",
        "fable-dispatch uninstall-hooks",
        "python3 fable_dispatch.py install-hooks",
    ):
        _assert_denied(cmd)


def test_deny_shell_separators_and_substitution() -> None:
    for cmd in (
        "git status -sb && rm -rf /tmp/should-not-run",
        "git status; echo pwned",
        "git status || echo pwned",
        "git status | echo pwned",
        "git status `echo pwned`",
        "git status $(echo pwned)",
        "git status\necho pwned",
        "git status\recho pwned",
        "git status > /tmp/out",
        "git status >> /tmp/out",
        "cat < /etc/passwd",
        "git status &",
        "python3 fable_dispatch.py --help && rm -rf /tmp/x",
        "FABLE_GUARDS_OFF=1 python3 fable_dispatch.py --help; rm -rf /tmp/x",
        "fable-dispatch doctor | tee /tmp/x",
        "echo $(codex exec)",
    ):
        _assert_denied(cmd)


def test_deny_pipelines_into_mutators() -> None:
    for cmd in (
        "git status | rm -rf /tmp/x",
        "cat README.md | tee /tmp/out",
        "ls | xargs rm",
        "find . -name '*.py' | xargs rm",
    ):
        _assert_denied(cmd)


def test_deny_wrappers_and_interpreters() -> None:
    for cmd in (
        "bash -c 'codex exec'",
        "sh -c 'fable-dispatch disarm'",
        "zsh -c 'rm -rf /'",
        "env codex exec",
        "sudo fable-dispatch disarm",
        "xargs codex",
        "nohup codex",
        "python3 -c 'import os; os.system(\"codex\")'",
        "python -c 'print(1)'",
        "python3 -m http.server",
        "python3 -",
        "source fable_dispatch.py",
        ". fable_dispatch.py",
        "perl -e 'system(\"codex\")'",
        "node -e 'require(\"fs\")'",
        "ruby -e 'system(\"codex\")'",
    ):
        _assert_denied(cmd)


def test_deny_misleading_prefixes_and_traversal() -> None:
    for cmd in (
        "git status-evil",
        # path traversal toward a denied binary or fake project script
        "../codex exec",
        "../../bin/codex",
        "python3 ../fable_dispatch.py disarm",
        "python3 hooks/../fable_dispatch.py disarm",
        "python3 /tmp/fable_dispatch.py doctor",
        "python3 /etc/passwd",
        "~/bin/codex",
        "python3 ./fable_verify.py/../../../usr/bin/codex",
        # absolute "project" scripts outside the workspace
        "/tmp/evil/fable_verify.py --gate true",
        "/private-path doctor",
    ):
        _assert_denied(cmd)


def test_deny_find_mutating_actions() -> None:
    for cmd in (
        "find . -delete",
        "find . -name '*.tmp' -delete",
        "find . -exec rm {} ;",
        "find . -execdir rm {} +",
        "find . -ok rm {} ;",
        "find . -okdir rm {} ;",
        "find . -fprint /tmp/out",
        "find . -fprintf /tmp/out '%p\\n'",
        "find . -fls /tmp/out",
    ):
        _assert_denied(cmd)


def test_deny_unsafe_verify_and_dispatch_shapes() -> None:
    for cmd in (
        "fable-dispatch verify",  # no host-frozen gate in policy context
        "fable-dispatch verify --cwd .",
        "fable-dispatch verify --gate true",
        "fable-dispatch verify --gate 'rm -rf .'",
        "fable-dispatch config --executor codex",
        "fable-dispatch config --global --effort medium",
        "python3 fable_verify.py",
        "python3 fable_verify.py --session default",
        "python3 fable_verify.py --gate true",
        "fable-dispatch verify --gate true --unknown-flag",
        "fable_verify.py --gate true; rm -rf /",
        "python3 fable_verify.py --gate true --extra evil",
        "python3 fable_dispatch.py verify --gate true --executor codex",  # verify path: only --gate/--cwd
        "FOO=bar python3 fable_dispatch.py --help",
        "FABLE_GUARDS_OFF=1 fable-dispatch doctor",
    ):
        _assert_denied(cmd)

    # Even with a frozen gate, the controller cannot replace its argv or workspace.
    _assert_denied("fable-dispatch verify --gate true", APPROVED_GATE)
    _assert_denied("fable-dispatch verify --cwd .", APPROVED_GATE)


def test_deny_git_mutating_and_write_flags() -> None:
    for cmd in (
        "git commit -m msg",
        "git push",
        "git checkout -b x",
        "git add .",
        "git reset --hard",
        "git clean -fd",
        "git branch -D work",
        "git branch -m old new",
        "git tag -d v0.2.5",
        "git tag v0.2.6",
        "git remote add origin https://example.invalid/repo.git",
        "git remote remove origin",
        "git remote set-url origin https://example.invalid/repo.git",
        "git diff --output=/tmp/x",
        "git --git-dir=/tmp/evil status",
        "git --work-tree=/tmp status",
        "git -C ../outside status",
        "git -C . status",
        # config override → external program execution (Sol-class bypass)
        "git -c diff.external=touch diff --ext-diff",
        "git -c core.editor=touch show HEAD",
        "git --config diff.external=touch diff --ext-diff",
        "git diff --ext-diff",
        "git diff --textconv",
        "git --exec-path=/tmp status",
        "git --namespace=evil status",
    ):
        _assert_denied(cmd)


def test_deny_rg_preprocessor_wrappers() -> None:
    for cmd in (
        "rg --pre cat pattern",
        "rg --pre=touch pattern",
        "grep --pre cat pattern",
    ):
        _assert_denied(cmd)


def test_deny_empty_and_garbage() -> None:
    for cmd in ("", "   ", "\t", "not-a-real-cmd", "true", "false", "make", "npm", "cargo"):
        _assert_denied(cmd)


def test_fable_bash_allow_is_cautious() -> None:
    """Whole-command/prefix overrides are ignored; only exact safe basenames extend policy."""
    old = os.environ.get("FABLE_BASH_ALLOW")
    try:
        # Prefix-style entries must NOT re-open prefix matching.
        os.environ["FABLE_BASH_ALLOW"] = "git status,codex,mytool"
        # Reload policy reads env each call — codex must stay denied.
        _assert_denied("codex exec")
        _assert_denied("git status-evil")
        # exact basename mytool may pass; multi-word ignored; codex ignored
        _assert(allowed("mytool") is True, "exact basename extra should allow mytool")
        _assert_denied("mytool -c 'evil'")
        # denied binary cannot be re-enabled
        os.environ["FABLE_BASH_ALLOW"] = "codex,grok,claude,bash"
        _assert_denied("codex")
        _assert_denied("bash")
    finally:
        if old is None:
            os.environ.pop("FABLE_BASH_ALLOW", None)
        else:
            os.environ["FABLE_BASH_ALLOW"] = old


# --------------------------------------------------------------------------- #
# Hook integration: armed / kill-switch / trivial-edit
# --------------------------------------------------------------------------- #
def _run_hook(payload: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("FABLE_GUARDS_OFF", None)
    env.pop("CLAUDE_GUARDS_OFF", None)
    env.pop("FABLE_GATE_ALLOW_TRIVIAL", None)
    env.pop("FABLE_BASH_ALLOW", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(ROOT),
    )


def _denied(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode != 0:
        return True
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    decision = (data.get("hookSpecificOutput") or {}).get("permissionDecision")
    return decision == "deny"


def _hook_allowed(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode == 0 and not _denied(proc)


def test_hook_armed_enforces_policy() -> None:
    sid = "gate-sec-armed"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=True, approved_gate=APPROVED_GATE)
        # allowed
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "fable-dispatch doctor"},
        })
        _assert(_hook_allowed(proc), f"doctor should allow: {proc.stdout!r} {proc.stderr!r}")

        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git status -sb"},
        })
        _assert(_hook_allowed(proc), f"git status should allow: {proc.stdout!r}")

        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "fable-dispatch verify"},
        })
        _assert(_hook_allowed(proc), f"frozen verify should allow: {proc.stdout!r}")

        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "fable-dispatch verify --gate 'rm -rf .'"},
        })
        _assert(_denied(proc), f"replacement gate should deny: {proc.stdout!r}")

        # denied body CLI
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "codex exec --yolo hi"},
        })
        _assert(_denied(proc), f"codex should deny: {proc.stdout!r}")

        # denied disarm
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "fable-dispatch disarm"},
        })
        _assert(_denied(proc), f"disarm should deny: {proc.stdout!r}")

        # Write blocked
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x", "content": "y"},
        })
        _assert(_denied(proc), f"Write should deny when armed: {proc.stdout!r}")
    finally:
        fc.clear_state(sid)


def test_hook_kill_switches_and_trivial_escape() -> None:
    sid = "gate-sec-kill"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=True)

        proc = _run_hook(
            {"session_id": sid, "tool_name": "Write",
             "tool_input": {"file_path": "/tmp/x", "content": "y"}},
            extra_env={"FABLE_GUARDS_OFF": "1"},
        )
        _assert(_hook_allowed(proc), f"FABLE_GUARDS_OFF should allow Write: {proc.stdout!r}")

        proc = _run_hook(
            {"session_id": sid, "tool_name": "Bash",
             "tool_input": {"command": "codex exec"}},
            extra_env={"CLAUDE_GUARDS_OFF": "1"},
        )
        _assert(_hook_allowed(proc), f"CLAUDE_GUARDS_OFF should allow Bash: {proc.stdout!r}")

        proc = _run_hook(
            {"session_id": sid, "tool_name": "Edit",
             "tool_input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}},
            extra_env={"FABLE_GATE_ALLOW_TRIVIAL": "1"},
        )
        _assert(_hook_allowed(proc), f"trivial-edit escape should allow Edit: {proc.stdout!r}")

        # trivial escape does not open Bash policy
        proc = _run_hook(
            {"session_id": sid, "tool_name": "Bash",
             "tool_input": {"command": "codex exec"}},
            extra_env={"FABLE_GATE_ALLOW_TRIVIAL": "1"},
        )
        _assert(_denied(proc), f"trivial escape must not allow codex: {proc.stdout!r}")
    finally:
        fc.clear_state(sid)


def test_hook_unarmed_allows_everything() -> None:
    sid = "gate-sec-unarmed"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=False)
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "codex exec --yolo hi"},
        })
        _assert(_hook_allowed(proc), f"unarmed should allow codex: {proc.stdout!r}")
        proc = _run_hook({
            "session_id": sid,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x", "content": "y"},
        })
        _assert(_hook_allowed(proc), f"unarmed Write should allow: {proc.stdout!r}")
    finally:
        fc.clear_state(sid)


def main() -> int:
    tests = [
        test_allowed_loop_commands,
        test_allowed_readonly_inspection,
        test_deny_direct_body_clis,
        test_deny_disarm_and_gate_toggles,
        test_deny_shell_separators_and_substitution,
        test_deny_pipelines_into_mutators,
        test_deny_wrappers_and_interpreters,
        test_deny_misleading_prefixes_and_traversal,
        test_deny_find_mutating_actions,
        test_deny_unsafe_verify_and_dispatch_shapes,
        test_deny_git_mutating_and_write_flags,
        test_deny_rg_preprocessor_wrappers,
        test_deny_empty_and_garbage,
        test_fable_bash_allow_is_cautious,
        test_hook_armed_enforces_policy,
        test_hook_kill_switches_and_trivial_escape,
        test_hook_unarmed_allows_everything,
    ]
    for fn in tests:
        fn()
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
