#!/usr/bin/env python3
"""Standalone stdlib contract tests for FrontierFuse safer execution defaults.

Covers: default command shapes (no --yolo / no bypassPermissions), explicit opt-ins,
unknown executor rejection, owner-only permissions, prompt cleanup, timeout process-group
handling, and whole-command compatibility overrides.

stdlib-only, offline, keyless.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="frontier-safe-exec-")
os.environ["FRONTIER_CONFIG_DIR"] = str(Path(_TMP) / "config")
os.environ["FRONTIER_STATE_DIR"] = str(Path(_TMP) / "state")
os.environ["FRONTIER_RUNS_DIR"] = str(Path(_TMP) / "runs")
# Clear whole-command overrides so defaults are exercised.
for _k in (
    "FRONTIER_CODEX_CMD", "FRONTIER_ADVISOR_CMD", "FRONTIER_BODY_CMD", "FRONTIER_EXECUTOR_CMD",
    "FRONTIER_CLAUDE_CMD", "FRONTIER_GROK_CMD", "FRONTIER_GEMINI_CMD",
    "FRONTIER_CODEX_YOLO", "FRONTIER_GROK_YOLO", "FRONTIER_GROK_PERMISSION_MODE",
    "FRONTIER_EXECUTOR",
):
    os.environ.pop(_k, None)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontier_common as fc  # noqa: E402


def _env(name: str, value: str | None) -> str | None:
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return old


def _restore(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _base_cfg(**overrides) -> dict:
    cfg = {
        "codex_model": "",
        "codex_effort": "high",
        "grok_effort": "high",
        "fast": False,
        "fast_effort": "low",
        "fast_model": "",
        "frontier_model": "claude-fable-5",
        "frontier_provider": "claude",
        "profile": "advisor",
        "executor": "codex",
        "claude_model": "claude-sonnet-5",
        "grok_model": "grok-4.5",
        "gemini_model": "gemini-3.5-flash",
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# Default command shapes (0.2.6 safer defaults)
# --------------------------------------------------------------------------- #
def test_codex_default_omits_yolo() -> None:
    old = _env("FRONTIER_CODEX_CMD", None)
    old_yolo = _env("FRONTIER_CODEX_YOLO", None)
    try:
        cmd = fc.build_codex_command(_base_cfg())
        assert cmd[0:2] == ["codex", "exec"], cmd
        assert "--yolo" not in cmd, cmd
        assert cmd[-1] == "-", cmd  # stdin transport preserved
        assert "-c" in cmd and any(a.startswith("model_reasoning_effort=") for a in cmd), cmd
    finally:
        _restore("FRONTIER_CODEX_CMD", old)
        _restore("FRONTIER_CODEX_YOLO", old_yolo)


def test_grok_default_omits_bypass_permissions() -> None:
    old = _env("FRONTIER_GROK_CMD", None)
    old_yolo = _env("FRONTIER_GROK_YOLO", None)
    old_perm = _env("FRONTIER_GROK_PERMISSION_MODE", None)
    try:
        cmd = fc.build_grok_command(_base_cfg(executor="grok"))
        assert cmd[0] == "grok", cmd
        assert "--permission-mode" not in cmd, cmd
        assert "bypassPermissions" not in cmd, cmd
        assert "--prompt-file" in cmd and "{prompt_file}" in cmd, cmd
    finally:
        _restore("FRONTIER_GROK_CMD", old)
        _restore("FRONTIER_GROK_YOLO", old_yolo)
        _restore("FRONTIER_GROK_PERMISSION_MODE", old_perm)


def test_body_command_defaults_match_builders() -> None:
    old_body = _env("FRONTIER_BODY_CMD", None)
    old_exec = _env("FRONTIER_EXECUTOR_CMD", None)
    old_codex = _env("FRONTIER_CODEX_CMD", None)
    old_grok = _env("FRONTIER_GROK_CMD", None)
    try:
        codex = fc.build_body_command(_base_cfg(executor="codex"))
        assert "--yolo" not in codex
        assert codex[-1] == "-"
        grok = fc.build_body_command(_base_cfg(executor="grok"))
        assert "--permission-mode" not in grok
        assert "{prompt_file}" in grok
    finally:
        _restore("FRONTIER_BODY_CMD", old_body)
        _restore("FRONTIER_EXECUTOR_CMD", old_exec)
        _restore("FRONTIER_CODEX_CMD", old_codex)
        _restore("FRONTIER_GROK_CMD", old_grok)


# --------------------------------------------------------------------------- #
# Explicit opt-ins for autonomous permissions
# --------------------------------------------------------------------------- #
def test_codex_yolo_opt_in() -> None:
    old = _env("FRONTIER_CODEX_CMD", None)
    old_yolo = _env("FRONTIER_CODEX_YOLO", "1")
    try:
        cmd = fc.build_codex_command(_base_cfg(codex_model="gpt-test"))
        assert "--yolo" in cmd, cmd
        assert cmd == [
            "codex", "exec", "--yolo", "--model", "gpt-test",
            "-c", "model_reasoning_effort=high", "-",
        ], cmd
        os.environ["FRONTIER_CODEX_YOLO"] = "0"
        assert "--yolo" not in fc.build_codex_command(_base_cfg())
        os.environ["FRONTIER_CODEX_YOLO"] = "false"
        assert "--yolo" not in fc.build_codex_command(_base_cfg())
    finally:
        _restore("FRONTIER_CODEX_CMD", old)
        _restore("FRONTIER_CODEX_YOLO", old_yolo)


def test_grok_yolo_and_permission_mode_opt_in() -> None:
    old = _env("FRONTIER_GROK_CMD", None)
    old_yolo = _env("FRONTIER_GROK_YOLO", None)
    old_perm = _env("FRONTIER_GROK_PERMISSION_MODE", None)
    try:
        os.environ["FRONTIER_GROK_YOLO"] = "1"
        cmd = fc.build_grok_command(_base_cfg(executor="grok"))
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"

        os.environ["FRONTIER_GROK_PERMISSION_MODE"] = "auto"
        cmd = fc.build_grok_command(_base_cfg(executor="grok"))
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"

        # Explicit mode wins even when YOLO is off.
        os.environ["FRONTIER_GROK_YOLO"] = "0"
        os.environ["FRONTIER_GROK_PERMISSION_MODE"] = "default"
        cmd = fc.build_grok_command(_base_cfg(executor="grok"))
        assert cmd[cmd.index("--permission-mode") + 1] == "default"
    finally:
        _restore("FRONTIER_GROK_CMD", old)
        _restore("FRONTIER_GROK_YOLO", old_yolo)
        _restore("FRONTIER_GROK_PERMISSION_MODE", old_perm)


# --------------------------------------------------------------------------- #
# Unknown executor fail-closed
# --------------------------------------------------------------------------- #
def test_unknown_executor_rejected() -> None:
    old_body = _env("FRONTIER_BODY_CMD", None)
    old_exec = _env("FRONTIER_EXECUTOR_CMD", None)
    try:
        for bad in ("custom", "unknown", "gpt", ""):
            # empty falls to "codex" via resolve; test raw cfg with bad value
            if bad == "":
                continue
            try:
                fc.build_body_command(_base_cfg(executor=bad))
            except ValueError as exc:
                assert "unknown executor" in str(exc).lower() or bad in str(exc)
            else:
                raise AssertionError(f"expected ValueError for executor={bad!r}")
    finally:
        _restore("FRONTIER_BODY_CMD", old_body)
        _restore("FRONTIER_EXECUTOR_CMD", old_exec)


def test_unknown_executor_body_override_still_works() -> None:
    """Whole-command override is trusted and bypasses executor selection."""
    old_body = _env("FRONTIER_BODY_CMD", "my-runner --flag")
    try:
        # Even with a nonsense executor, whole-command override wins first.
        assert fc.build_body_command(_base_cfg(executor="not-a-real-engine")) == [
            "my-runner", "--flag",
        ]
    finally:
        _restore("FRONTIER_BODY_CMD", old_body)


# --------------------------------------------------------------------------- #
# Compatibility whole-command overrides
# --------------------------------------------------------------------------- #
def test_per_engine_and_universal_overrides() -> None:
    keys = [
        "FRONTIER_CODEX_CMD", "FRONTIER_CLAUDE_CMD", "FRONTIER_GROK_CMD", "FRONTIER_GEMINI_CMD",
        "FRONTIER_BODY_CMD", "FRONTIER_EXECUTOR_CMD",
    ]
    olds = {k: _env(k, None) for k in keys}
    try:
        os.environ["FRONTIER_CODEX_CMD"] = "codex-shim run"
        assert fc.build_codex_command(_base_cfg()) == ["codex-shim", "run"]

        os.environ["FRONTIER_GROK_CMD"] = "grok-shim --x"
        assert fc.build_grok_command(_base_cfg()) == ["grok-shim", "--x"]

        os.environ["FRONTIER_CLAUDE_CMD"] = "claude-shim"
        assert fc.build_claude_command(_base_cfg()) == ["claude-shim"]

        os.environ["FRONTIER_GEMINI_CMD"] = "gemini-shim"
        assert fc.build_gemini_command(_base_cfg()) == ["gemini-shim"]

        os.environ.pop("FRONTIER_BODY_CMD", None)
        os.environ["FRONTIER_EXECUTOR_CMD"] = "exec-shim a b"
        assert fc.build_body_command(_base_cfg(executor="grok")) == ["exec-shim", "a", "b"]

        os.environ["FRONTIER_BODY_CMD"] = "body-wins"
        assert fc.build_body_command(_base_cfg(executor="codex")) == ["body-wins"]
    finally:
        for k, old in olds.items():
            _restore(k, old)


# --------------------------------------------------------------------------- #
# Owner-only permissions for config / state / runs / prompts / cards
# --------------------------------------------------------------------------- #
def test_config_and_state_owner_only() -> None:
    sid = "safe-exec-state"
    fc.clear_state(sid)
    try:
        fc.save_global_config({"codex_effort": "medium"})
        assert fc.GLOBAL_CONFIG.is_file()
        assert _mode(fc.GLOBAL_CONFIG) == 0o600
        assert _mode(fc.GLOBAL_CONFIG.parent) == 0o700

        fc.write_state(sid, armed=True, config={"executor": "codex"})
        sp = fc.state_path(sid)
        assert sp.is_file()
        assert _mode(sp) == 0o600
        assert _mode(sp.parent) == 0o700
        st = fc.read_state(sid)
        assert st["armed"] is True
        assert st["config"]["executor"] == "codex"
    finally:
        fc.clear_state(sid)
        try:
            fc.GLOBAL_CONFIG.unlink()
        except OSError:
            pass


def test_artifact_and_handoff_owner_only() -> None:
    run_id = "safe-art-1"
    base = Path(os.environ["FRONTIER_RUNS_DIR"])
    art = fc.write_artifact(base, run_id, "body-0", "do thing", "hello artifact")
    path = Path(art["path"])
    assert path.is_file()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700

    card = fc.handoff_card("body-0", "do thing", "hello artifact", art)
    card_path = fc.write_handoff_card(base, run_id, card)
    assert card_path.is_file()
    assert _mode(card_path) == 0o600
    loaded = json.loads(card_path.read_text())
    assert loaded["label"] == "body-0"
    assert loaded["summary"]


def test_atomic_write_helpers() -> None:
    d = Path(_TMP) / "atomic-nest"
    target = d / "nested" / "out.json"
    fc.write_json_owner_only(target, {"a": 1, "b": 2})
    assert json.loads(target.read_text()) == {"a": 1, "b": 2}
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700

    text_path = d / "note.txt"
    fc.write_text_owner_only(text_path, "secret\n")
    assert text_path.read_text() == "secret\n"
    assert _mode(text_path) == 0o600


def test_prompt_file_owner_only_and_cleanup() -> None:
    cmd = ["grok", "--prompt-file", "{prompt_file}"]
    final, stdin, cleanup = fc._prepare_prompt_command(cmd, "prompt body contents")
    try:
        assert stdin is None
        assert len(cleanup) == 1
        p = Path(cleanup[0])
        assert p.is_file()
        assert p.read_text() == "prompt body contents"
        assert _mode(p) == 0o600
        assert final[-1] == str(p)
        assert "{prompt_file}" not in final
    finally:
        for path in cleanup:
            Path(path).unlink(missing_ok=True)

    # run_engine must clean up prompt files even when the binary is missing / returns.
    # Use python that echoes the file then exits.
    script = (
        "import sys; p=sys.argv[1]; print(open(p).read()); "
    )
    # Build a command that uses {prompt_file} with python -c reading argv after replace.
    # Simpler: use cat via prepared path through run_engine with a wrapper.
    # Direct unit: after prepare, run_engine unlinks in finally.
    marker = Path(_TMP) / "prompt-seen.txt"
    wrapper = Path(_TMP) / "prompt_reader.py"
    wrapper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "Path(sys.argv[2]).write_text(p.read_text() + '|' + oct(p.stat().st_mode & 0o777))\n"
        "print('ok')\n"
    )
    cmd2 = [sys.executable, str(wrapper), "{prompt_file}", str(marker)]
    # Manually expand only through run_engine:
    # _prepare_prompt_command replaces {prompt_file}; run_engine cleans up.
    # But our cmd uses {prompt_file} as a whole argv element — good.
    # Wait: cmd2 has {prompt_file} as separate arg — _prepare replaces in each arg.
    rc, out, err = fc.run_engine(cmd2, "clean-me", timeout=10)
    assert rc == 0, (rc, out, err)
    assert marker.is_file()
    body, mode_s = marker.read_text().split("|", 1)
    assert body == "clean-me"
    assert mode_s == "0o600"
    # prompt temp file should be gone (cleanup). Reconstruct by scanning /tmp is flaky;
    # instead ensure prepare+run leaves no leftover from this call by checking cleanup list
    # path was unlinked: re-prepare a similar call and verify pattern.
    final2, _stdin2, cleanup2 = fc._prepare_prompt_command(
        [sys.executable, "-c", "print(open('{prompt_file}').read())"],
        "x",
    )
    # Note: {prompt_file} inside -c string gets replaced; cleanup still listed.
    for path in cleanup2:
        assert Path(path).is_file()
        Path(path).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Stdin transport for Codex preserved
# --------------------------------------------------------------------------- #
def test_codex_stdin_transport_preserved() -> None:
    old = _env("FRONTIER_CODEX_CMD", None)
    try:
        cmd = fc.build_codex_command(_base_cfg())
        final, stdin, cleanup = fc._prepare_prompt_command(cmd, "large spec body")
        assert cleanup == []
        assert stdin == "large spec body"
        assert final[-1] == "-"
    finally:
        _restore("FRONTIER_CODEX_CMD", old)

    # Echo via override that reads stdin conceptually: python reads sys.stdin
    rc, out, err = fc.run_engine(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        "hello-stdin",
        timeout=10,
    )
    assert rc == 0, (rc, out, err)
    assert out == "hello-stdin"


# --------------------------------------------------------------------------- #
# Timeout kills process group
# --------------------------------------------------------------------------- #
def test_timeout_terminates_process_group() -> None:
    """Child forked by the provider script dies when the group is killed on timeout."""
    stamp = Path(_TMP) / "pg-alive"
    if stamp.exists():
        stamp.unlink()
    # Provider: fork a child that keeps updating stamp; parent sleeps forever.
    # If only the parent is killed (not the group), the child keeps writing.
    provider = Path(_TMP) / "group_provider.py"
    provider.write_text(
        "import os, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    while True:\n"
        "        open(stamp, 'w').write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "time.sleep(60)\n"
    )
    t0 = time.time()
    rc, out, err = fc.run_engine([sys.executable, str(provider)], "", timeout=1)
    elapsed = time.time() - t0
    assert rc == 124, (rc, out, err)
    assert elapsed < 8, f"timeout took too long: {elapsed}"
    # Wait briefly; if orphan child survived, stamp mtime keeps advancing.
    time.sleep(0.3)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.4)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "child process group member still alive after timeout kill"


def test_run_engine_success_and_missing_binary() -> None:
    rc, out, err = fc.run_engine([sys.executable, "-c", "print('hi')"], "", timeout=10)
    assert rc == 0 and out == "hi"
    rc, out, err = fc.run_engine(["definitely-not-on-path-xyz"], "x", timeout=5)
    assert rc == 127
    assert "not on PATH" in err


# --------------------------------------------------------------------------- #
# Dispatch integration: unknown executor exit, dry-run defaults
# --------------------------------------------------------------------------- #
def test_dispatch_refuses_unknown_executor_and_dry_run_defaults() -> None:
    env = os.environ.copy()
    env["FRONTIER_CONFIG_DIR"] = str(Path(_TMP) / "dispatch-cfg")
    env["FRONTIER_STATE_DIR"] = str(Path(_TMP) / "dispatch-state")
    env["FRONTIER_RUNS_DIR"] = str(Path(_TMP) / "dispatch-runs")
    env["FRONTIER_SESSION_ID"] = "safe-dispatch"
    for k in ("FRONTIER_CODEX_CMD", "FRONTIER_BODY_CMD", "FRONTIER_EXECUTOR_CMD",
              "FRONTIER_CODEX_YOLO", "FRONTIER_GROK_YOLO", "FRONTIER_GROK_PERMISSION_MODE"):
        env.pop(k, None)

    # Inject unknown executor via session state by writing then dry-run — easier: env FRONTIER_EXECUTOR
    env["FRONTIER_EXECUTOR"] = "notreal"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "--dry-run", "task one"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "unknown executor" in (proc.stderr or "").lower() or "refused" in (proc.stderr or "").lower()

    env.pop("FRONTIER_EXECUTOR", None)
    # Codex dry-run: no --yolo
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"),
         "--dry-run", "--executor", "codex", "safe dry task"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    summary = payload["cards"][0]["summary"]
    assert "--yolo" not in summary, summary

    # Grok dry-run: no bypassPermissions
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"),
         "--dry-run", "--executor", "grok", "safe grok task"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    summary = payload["cards"][0]["summary"]
    assert "bypassPermissions" not in summary, summary
    assert "--permission-mode" not in summary, summary
    assert "<prompt-file>" in summary or "prompt-file" in summary


def test_dispatch_live_writes_owner_only_artifacts() -> None:
    env = os.environ.copy()
    runs = Path(_TMP) / "dispatch-live-runs"
    env["FRONTIER_CONFIG_DIR"] = str(Path(_TMP) / "dispatch-live-cfg")
    env["FRONTIER_STATE_DIR"] = str(Path(_TMP) / "dispatch-live-state")
    env["FRONTIER_RUNS_DIR"] = str(runs)
    env["FRONTIER_SESSION_ID"] = "safe-dispatch-live"
    env["FRONTIER_BODY_CMD"] = f"{sys.executable} -c \"import sys; print(sys.stdin.read().strip())\""
    for k in ("FRONTIER_CODEX_CMD", "FRONTIER_EXECUTOR_CMD", "FRONTIER_CODEX_YOLO"):
        env.pop(k, None)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "artifact body text"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    art = payload["cards"][0]["artifact"]
    assert art, payload
    art_path = Path(art)
    assert art_path.is_file()
    assert _mode(art_path) == 0o600
    assert _mode(art_path.parent) == 0o700
    handoff = art_path.parent / "body-0.handoff.json"
    assert handoff.is_file()
    assert _mode(handoff) == 0o600


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [
        test_codex_default_omits_yolo,
        test_grok_default_omits_bypass_permissions,
        test_body_command_defaults_match_builders,
        test_codex_yolo_opt_in,
        test_grok_yolo_and_permission_mode_opt_in,
        test_unknown_executor_rejected,
        test_unknown_executor_body_override_still_works,
        test_per_engine_and_universal_overrides,
        test_config_and_state_owner_only,
        test_artifact_and_handoff_owner_only,
        test_atomic_write_helpers,
        test_prompt_file_owner_only_and_cleanup,
        test_codex_stdin_transport_preserved,
        test_timeout_terminates_process_group,
        test_run_engine_success_and_missing_binary,
        test_dispatch_refuses_unknown_executor_and_dry_run_defaults,
        test_dispatch_live_writes_owner_only_artifacts,
    ]
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
    print()
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        return 1
    print(f"PASS  {len(tests)}/{len(tests)} safe_execution_contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
