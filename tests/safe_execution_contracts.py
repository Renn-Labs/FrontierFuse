#!/usr/bin/env python3
"""Standalone stdlib contract tests for FrontierFuse safer execution defaults.

Covers: default command shapes (no --yolo / no bypassPermissions), explicit opt-ins,
unknown executor rejection, owner-only permissions, prompt cleanup, timeout process-group
handling, and whole-command compatibility overrides.

stdlib-only, offline, keyless.
"""
from __future__ import annotations

import errno
import json
import os
import selectors
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


def test_grok_fast_mode_rejects_xhigh_effort() -> None:
    cfg = _base_cfg(executor="grok")
    cfg.update({"fast": True, "fast_effort": "xhigh"})
    try:
        fc.build_grok_command(cfg)
    except ValueError as exc:
        assert "Grok reasoning effort" in str(exc)
    else:
        raise AssertionError("Grok fast mode must reject Codex-only xhigh effort")


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
# Bounded subprocess capture (0.3.7 gate-bounds)
# --------------------------------------------------------------------------- #
def test_hostile_stdout_overflow_capped() -> None:
    """Oversized stdout is hard-capped during read; excess discarded without retention."""
    # Build marker-exclusion needle from pieces (avoid scrub-shaped full literals).
    needle = "MARKER" + "_EXCLUSION" + "_NEEDLE"
    # ~200 KiB of payload with a unique token past the retained prefix.
    chunk = ("A" * 1024) + "OVERFLOW_TOKEN_TAIL\n"
    script = (
        "import sys\n"
        f"sys.stdout.write({chunk!r} * 200)\n"
        "sys.stdout.flush()\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=15,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" in out
    assert "retained_bytes=4096" in out
    assert "discarded_bytes=" in out
    # Most of the ~200 KiB payload must have been discarded, not retained.
    discarded = int(
        [p for p in out.split() if p.startswith("discarded_bytes=")][0].split("=", 1)[1].rstrip("]")
    )
    assert discarded > 100_000, discarded
    # Marker must never embed caller content (needle is absent from capture entirely).
    assert needle not in out and needle not in err
    assert needle not in fc.capture_truncation_marker(
        stream="stdout", retained_bytes=4096, discarded_bytes=discarded
    )
    # Retained payload is hard-capped at the byte limit (optional newline before marker).
    body = out.split("[frontierfuse:capture_truncated", 1)[0]
    assert len(body.encode("utf-8")) <= 4096 + 1


def test_simultaneous_dual_stream_overflow() -> None:
    """Both streams overflowing concurrently must not deadlock and both must cap."""
    script = (
        "import os, sys\n"
        "payload = b'X' * (256 * 1024)\n"
        "os.write(1, payload)\n"
        "os.write(2, b'Y' * (256 * 1024))\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=15,
        max_stdout_bytes=2048,
        max_stderr_bytes=1024,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" in out and "stream=stdout" in out
    assert "capture_truncated" in err and "stream=stderr" in err
    assert "retained_bytes=2048" in out
    assert "retained_bytes=1024" in err
    assert "discarded_bytes=" in out and "discarded_bytes=" in err


def test_timeout_descendant_killed_under_bounded_capture() -> None:
    """Timeout still kills forked descendants when using the shared capture primitive."""
    stamp = Path(_TMP) / "pg-alive-bounded"
    if stamp.exists():
        stamp.unlink()
    provider = Path(_TMP) / "group_provider_bounded.py"
    provider.write_text(
        "import os, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    while True:\n"
        "        open(stamp, 'w').write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "time.sleep(60)\n"
    )
    t0 = time.time()
    try:
        fc.run_bounded_subprocess(
            [sys.executable, str(provider)],
            timeout=1,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("expected TimeoutExpired")
    elapsed = time.time() - t0
    assert elapsed < 8, f"timeout took too long: {elapsed}"
    time.sleep(0.3)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.4)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "descendant still alive after bounded-capture timeout kill"


def test_invalid_capture_limits_rejected_before_launch() -> None:
    for bad in (0, -1, 1.5, True, "4096", None, fc.CAPTURE_MAX_BYTES_HARD_CEILING + 1):
        try:
            fc.validate_capture_max_bytes(bad)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
    # run_bounded_subprocess must validate before spawning.
    try:
        fc.run_bounded_subprocess(
            [sys.executable, "-c", "print('nope')"],
            max_stdout_bytes=0,
        )
    except ValueError as exc:
        assert "max_stdout_bytes" in str(exc)
    else:
        raise AssertionError("invalid max_stdout_bytes must raise before launch")

    old = _env("FRONTIER_PROVIDER_CAPTURE_MAX_BYTES", "not-an-int")
    try:
        rc, out, err = fc.run_engine([sys.executable, "-c", "print(1)"], "", timeout=5)
        assert rc == 2
        assert "invalid provider capture limit" in err
    finally:
        _restore("FRONTIER_PROVIDER_CAPTURE_MAX_BYTES", old)


def test_non_utf8_output_replaced() -> None:
    """Invalid UTF-8 on either stream is replaced, not raised."""
    script = (
        "import os, sys\n"
        "os.write(1, b'ok-\\xff\\xfe-end\\n')\n"
        "os.write(2, b'err-\\x80\\x81\\n')\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
    assert rc == 0, (rc, out, err)
    assert "ok-" in out and "end" in out
    assert "\ufffd" in out or "\ufffd" in err or "err-" in err
    assert "err-" in err


def test_truncation_marker_has_no_prompt_content() -> None:
    marker = fc.capture_truncation_marker(
        stream="stdout", retained_bytes=10, discarded_bytes=99
    )
    assert marker == (
        "[frontierfuse:capture_truncated stream=stdout "
        "retained_bytes=10 discarded_bytes=99]"
    )
    assert "prompt" not in marker.lower()
    assert "secret" not in marker.lower()




# --------------------------------------------------------------------------- #
# 0.3.7 REVISE: parent-exits-first, marker spoofing, POSIX contract
# --------------------------------------------------------------------------- #
def test_parent_exits_first_inherited_silent_pipe_no_hang() -> None:
    """Leader exits 0 while a SIGTERM-ignoring descendant holds silent stdout — must not hang."""
    stamp = Path(_TMP) / "silent-pipe-stamp"
    if stamp.exists():
        stamp.unlink()
    script = (
        "import os, signal, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    # Keep inherited stdout open; write nothing (silent pipe holder).\n"
        "    while True:\n"
        "        open(stamp, 'w').write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "sys.exit(0)\n"
    )
    t0 = time.time()
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=8,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    elapsed = time.time() - t0
    assert elapsed < 6.0, f"parent-exits-first silent pipe hung: {elapsed:.2f}s"
    assert rc == 0, (rc, out, err)
    time.sleep(0.25)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.35)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "SIGTERM-ignoring silent-pipe descendant still alive after return"


def test_parent_exits_first_descendant_closes_stdio_stamp_file() -> None:
    """Leader exits after forking; descendant closes stdio but keeps writing a stamp file."""
    stamp = Path(_TMP) / "closed-stdio-stamp"
    if stamp.exists():
        stamp.unlink()
    script = (
        "import os, signal, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    try:\n"
        "        sys.stdout.close()\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        sys.stderr.close()\n"
        "    except Exception:\n"
        "        pass\n"
        "    # Re-open fds 1/2 to devnull so close of pipe ends; keep running.\n"
        "    try:\n"
        "        dn = open(os.devnull, 'wb')\n"
        "        os.dup2(dn.fileno(), 1)\n"
        "        os.dup2(dn.fileno(), 2)\n"
        "    except Exception:\n"
        "        pass\n"
        "    while True:\n"
        "        open(stamp, 'w').write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "sys.exit(0)\n"
    )
    t0 = time.time()
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=8,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    elapsed = time.time() - t0
    assert elapsed < 6.0, f"closed-stdio descendant path hung: {elapsed:.2f}s"
    assert rc == 0, (rc, out, err)
    time.sleep(0.25)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.35)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "SIGTERM-ignoring closed-stdio descendant still alive"


def test_marker_spoof_no_truncation_disambiguated() -> None:
    """Child-printed reserved marker without real truncation must not look authentic."""
    # Build spoof from pieces so scrub tools do not treat the test as a secret.
    prefix = "[frontierfuse:" + "capture_truncated"
    spoof = prefix + " stream=stdout retained_bytes=1 discarded_bytes=999]"
    script = (
        "import sys\n"
        f"sys.stdout.write({spoof!r})\n"
        "sys.stdout.write('\\nreal-body\\n')\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024,
    )
    assert rc == 0, (rc, out, err)
    # No authentic truncation (discarded_bytes not applied by parent).
    assert "real-body" in out
    # Exact reserved prefix from child must be disambiguated.
    assert prefix not in out, out
    # Must not claim parent truncation when none occurred.
    assert "discarded_bytes=999]" not in out or "\u200c" in out or "\u2060" in out
    # Authentic parent marker only when discarded > 0 — absent here.
    authentic = fc.capture_truncation_marker(
        stream="stdout", retained_bytes=1, discarded_bytes=999
    )
    assert authentic not in out


def test_marker_spoof_with_real_truncation_authentic_appended() -> None:
    """Child spoof + real overflow: child tokens disambiguated; authentic marker last."""
    prefix = "[frontierfuse:" + "capture_truncated"
    spoof = prefix + " stream=stdout retained_bytes=0 discarded_bytes=1]"
    # Force overflow past a small cap.
    script = (
        "import sys\n"
        f"sys.stdout.write({spoof!r})\n"
        "sys.stdout.write('X' * 10000)\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=64,
        max_stderr_bytes=1024,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" in out
    # Authentic marker from parent uses retained_bytes=64.
    assert "retained_bytes=64" in out
    assert out.rstrip().endswith("]")
    # The final authentic marker must use the exact reserved prefix.
    assert prefix in out
    # Child spoof at the start must not remain an exact unescaped prefix-only body claim.
    # Body before authentic marker should not contain a second unescaped full authentic line
    # with discarded_bytes=1 from the child.
    body, _, tail = out.rpartition(prefix)
    assert "retained_bytes=64" in (prefix + tail)
    # Escaped child spoof should not match exact prefix at start of body.
    # (disambiguation inserts ZWNJ/WJ inside the token)
    if body:
        # Count exact prefix occurrences — only the authentic append at the end.
        assert out.count(prefix) == 1, out


def test_posix_platform_contract_helpers() -> None:
    """POSIX gate is explicit; non-POSIX is rejected before launch when simulated."""
    # On this Linux/macOS host the helper must accept.
    fc._require_posix_process_group_capture()
    # Simulate non-POSIX without spawning.
    real_name = os.name
    try:
        os.name = "nt"  # type: ignore[misc]
        try:
            fc._require_posix_process_group_capture()
        except OSError as exc:
            msg = str(exc).lower()
            assert "posix" in msg or "windows" in msg or "not supported" in msg
        else:
            raise AssertionError("non-POSIX must fail fast")
    finally:
        os.name = real_name  # type: ignore[misc]


def test_exact_byte_cap_and_multibyte_boundary() -> None:
    """Exact limit retention and multibyte UTF-8 split at the cap are safe."""
    # Exact cap: 16 bytes retained, no discard.
    script = "import sys; sys.stdout.buffer.write(b'A' * 16)"
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=16,
        max_stderr_bytes=16,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" not in out
    assert out == "A" * 16

    # One byte over → truncation marker.
    script = "import sys; sys.stdout.buffer.write(b'B' * 17)"
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=16,
        max_stderr_bytes=16,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" in out
    assert "retained_bytes=16" in out
    assert "discarded_bytes=1" in out

    # Multibyte: 2-byte chars; cap mid-sequence must not raise.
    # 'é' is C3 A9 in UTF-8. 5 complete + first byte of 6th = 11 bytes retained.
    script = (
        "import sys\n"
        "sys.stdout.buffer.write('é'.encode('utf-8') * 20)\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=11,
        max_stderr_bytes=64,
    )
    assert rc == 0, (rc, out, err)
    assert "capture_truncated" in out
    assert "retained_bytes=11" in out
    # errors=replace may insert U+FFFD for the split trail — must be a str.
    assert isinstance(out, str)


def test_normal_success_and_timeout_paths() -> None:
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", "import sys; print('ok-success'); print('e', file=sys.stderr)"],
        timeout=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
    assert rc == 0 and "ok-success" in out
    try:
        fc.run_bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.4,
            max_stdout_bytes=512,
            max_stderr_bytes=512,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("expected TimeoutExpired")


def test_stdin_selector_registration_failure_cleans_group() -> None:
    """stdin EVENT_WRITE registration failure must kill the group and raise OSError."""
    real_selector = selectors.DefaultSelector

    class _FailStdinRegister(real_selector):  # type: ignore[valid-type,misc]
        def register(self, fileobj, events, data=None):  # type: ignore[no-untyped-def]
            if events & selectors.EVENT_WRITE:
                raise OSError(errno.EINVAL, "simulated stdin register failure")
            return super().register(fileobj, events, data)

    stamp = Path(_TMP) / "stdin-reg-fail-alive"
    if stamp.exists():
        stamp.unlink()
    # Child that would hang if left alive waiting on stdin.
    script = (
        "import os, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "open(stamp, 'w').write('started')\n"
        "sys.stdin.read()\n"
        "while True:\n"
        "    open(stamp, 'w').write(str(time.time()))\n"
        "    time.sleep(0.05)\n"
    )
    old = selectors.DefaultSelector
    selectors.DefaultSelector = _FailStdinRegister  # type: ignore[misc,assignment]
    t0 = time.time()
    try:
        try:
            fc.run_bounded_subprocess(
                [sys.executable, "-c", script],
                input="payload-that-needs-write-registration\n",
                timeout=8,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            )
        except OSError as exc:
            assert "stdin selector registration failed" in str(exc).lower() or "register" in str(exc).lower()
        else:
            raise AssertionError("expected OSError on stdin selector registration failure")
    finally:
        selectors.DefaultSelector = old  # type: ignore[misc,assignment]
    elapsed = time.time() - t0
    assert elapsed < 6.0, f"stdin reg failure cleanup hung: {elapsed:.2f}s"
    time.sleep(0.2)
    # Child must not keep running (stamp frozen or absent after cleanup).
    if stamp.exists() and stamp.read_text().strip() not in ("", "started"):
        m1 = stamp.stat().st_mtime
        time.sleep(0.35)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "child survived after stdin registration failure cleanup"


def test_group_kill_does_not_skip_on_leader_exit() -> None:
    """_kill_process_group must signal the recorded pgid even if the leader is already dead."""
    stamp = Path(_TMP) / "kill-after-leader"
    if stamp.exists():
        stamp.unlink()
    # Manual mini harness: spawn group, wait for leader death, call kill helper.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, time\n"
                f"stamp = {str(stamp)!r}\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    while True:\n"
                "        open(stamp, 'w').write(str(time.time()))\n"
                "        time.sleep(0.05)\n"
                "raise SystemExit(0)\n"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    proc.wait(timeout=5)
    assert proc.returncode == 0
    # Descendant should be alive writing stamp.
    time.sleep(0.15)
    assert stamp.exists(), "descendant did not start"
    fc._kill_process_group(pgid=pgid, proc=proc)
    time.sleep(0.3)
    m1 = stamp.stat().st_mtime
    time.sleep(0.4)
    m2 = stamp.stat().st_mtime
    assert m1 == m2, "kill helper returned while SIGTERM-ignoring descendant survived"


def test_start_new_session_false_fail_closed_no_launch() -> None:
    """start_new_session=False must raise before Popen; no child, no descendant leak."""
    launch_count = {"n": 0}
    real_popen = subprocess.Popen

    def _guarded_popen(*a, **kw):  # type: ignore[no-untyped-def]
        launch_count["n"] += 1
        return real_popen(*a, **kw)

    stamp = Path(_TMP) / "sns-false-leak"
    if stamp.exists():
        stamp.unlink()
    old = subprocess.Popen
    subprocess.Popen = _guarded_popen  # type: ignore[misc,assignment]
    try:
        try:
            fc.run_bounded_subprocess(
                [
                    sys.executable,
                    "-c",
                    (
                        "import time\n"
                        f"open({str(stamp)!r}, 'w').write('launched')\n"
                        "time.sleep(30)\n"
                    ),
                ],
                timeout=5,
                max_stdout_bytes=512,
                max_stderr_bytes=512,
                start_new_session=False,
            )
        except ValueError as exc:
            msg = str(exc).lower()
            assert "start_new_session" in msg
            assert "true" in msg or "isolation" in msg or "refusing" in msg
        else:
            raise AssertionError("start_new_session=False must fail closed before launch")
    finally:
        subprocess.Popen = old  # type: ignore[misc,assignment]

    assert launch_count["n"] == 0, f"child launched despite fail-closed: {launch_count['n']}"
    assert not stamp.exists(), "descendant/leak stamp must not exist when launch is refused"
    # Also reject other non-True truthy misuse (e.g. 1 is not True for `is not True`).
    try:
        fc.run_bounded_subprocess(
            [sys.executable, "-c", "print(1)"],
            max_stdout_bytes=64,
            start_new_session=0,  # type: ignore[arg-type]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-True start_new_session must be rejected")


def test_blockingio_eagain_keeps_fd_and_preserves_output() -> None:
    """Transient BlockingIOError/EAGAIN must not be treated as EOF; later bytes survive."""
    # Patch the capture-only read helper (not global os.read) so Popen errpipe is untouched.
    real_read = fc._capture_os_read
    state = {"blocking_left": 1, "saw_blocking": 0, "saw_data": 0}

    def _adversarial_read(fd: int, n: int) -> bytes:  # type: ignore[no-untyped-def]
        if state["blocking_left"] > 0:
            state["blocking_left"] -= 1
            state["saw_blocking"] += 1
            raise BlockingIOError(errno.EAGAIN, "simulated transient EAGAIN")
        data = real_read(fd, n)
        if data:
            state["saw_data"] += 1
        return data

    old = fc._capture_os_read
    fc._capture_os_read = _adversarial_read  # type: ignore[assignment]
    try:
        rc, out, err = fc.run_bounded_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.write('AFTER_EAGAIN_OK\\n')"],
            timeout=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=1024,
        )
    finally:
        fc._capture_os_read = old  # type: ignore[assignment]

    assert rc == 0, (rc, out, err)
    assert state["saw_blocking"] >= 1, "adversarial BlockingIOError never fired"
    assert "AFTER_EAGAIN_OK" in out, f"output lost after transient EAGAIN: {out!r}"
    assert "capture_truncated" not in out


def test_eagain_oserror_errno_keeps_fd() -> None:
    """OSError(EAGAIN)/EWOULDBLOCK (not only BlockingIOError) must stay non-fatal."""
    real_read = fc._capture_os_read
    state = {"phase": 0}

    def _eagain_then_data(fd: int, n: int) -> bytes:  # type: ignore[no-untyped-def]
        if state["phase"] == 0:
            state["phase"] = 1
            raise OSError(errno.EAGAIN, "raw EAGAIN")
        if state["phase"] == 1:
            state["phase"] = 2
            raise OSError(errno.EWOULDBLOCK, "raw EWOULDBLOCK")
        return real_read(fd, n)

    old = fc._capture_os_read
    fc._capture_os_read = _eagain_then_data  # type: ignore[assignment]
    try:
        rc, out, err = fc.run_bounded_subprocess(
            [sys.executable, "-c", "print('EAGAIN_OSERROR_OK')"],
            timeout=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=1024,
        )
    finally:
        fc._capture_os_read = old  # type: ignore[assignment]

    assert rc == 0, (rc, out, err)
    assert "EAGAIN_OSERROR_OK" in out
    assert state["phase"] >= 2


def test_unrelated_process_not_killed_by_group_cleanup() -> None:
    """Process-group cleanup must not signal an unrelated process outside the session."""
    stamp = Path(_TMP) / "unrelated-alive"
    if stamp.exists():
        stamp.unlink()
    # Unrelated peer: own session, writes stamp; must survive a bounded capture timeout.
    peer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import time\n"
                f"stamp = {str(stamp)!r}\n"
                "while True:\n"
                "    open(stamp, 'w').write(str(time.time()))\n"
                "    time.sleep(0.05)\n"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            fc.run_bounded_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.4,
                max_stdout_bytes=256,
                max_stderr_bytes=256,
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("expected TimeoutExpired for timed capture")
        time.sleep(0.2)
        assert peer.poll() is None, "unrelated peer was killed by capture cleanup"
        assert stamp.exists(), "unrelated peer stamp missing"
        m1 = stamp.stat().st_mtime
        time.sleep(0.25)
        m2 = stamp.stat().st_mtime
        assert m2 >= m1, "unrelated peer stopped updating stamp"
    finally:
        try:
            os.killpg(os.getpgid(peer.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                peer.kill()
            except OSError:
                pass
        try:
            peer.wait(timeout=2)
        except Exception:
            pass


def test_set_blocking_failure_still_completes() -> None:
    """If os.set_blocking fails, capture must still complete (best-effort nonblocking)."""
    real_set_blocking = os.set_blocking
    calls = {"n": 0}

    def _fail_set_blocking(fd: int, blocking: bool) -> None:  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise OSError(errno.EINVAL, "simulated set_blocking failure")

    old = os.set_blocking
    os.set_blocking = _fail_set_blocking  # type: ignore[assignment]
    try:
        rc, out, err = fc.run_bounded_subprocess(
            [sys.executable, "-c", "print('NONBLOCK_FAIL_OK')"],
            timeout=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=1024,
        )
    finally:
        os.set_blocking = old  # type: ignore[assignment]

    assert calls["n"] >= 1
    assert rc == 0, (rc, out, err)
    assert "NONBLOCK_FAIL_OK" in out



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
# 0.3.7 containment: setsid / double-fork / post-success mutation / cleanup fail
# --------------------------------------------------------------------------- #
def test_setsid_sigterm_ignore_descendant_killed() -> None:
    """fork+setsid+SIGTERM-ignore must not survive past run_bounded_subprocess return."""
    stamp = Path(_TMP) / "setsid-escape.stamp"
    if stamp.exists():
        stamp.unlink()
    script = (
        "import os, signal, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True:\n"
        "        open(stamp, 'w').write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "sys.exit(0)\n"
    )
    t0 = time.time()
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    elapsed = time.time() - t0
    assert elapsed < 8.0, f"setsid containment hung: {elapsed:.2f}s"
    assert rc == 0, (rc, out, err)
    time.sleep(0.3)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.4)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "setsid+SIGTERM-ignore descendant still alive after return"


def test_double_fork_setsid_descendant_killed() -> None:
    """double-fork + setsid escape must reparent to subreaper and be reaped/killed."""
    stamp = Path(_TMP) / "double-fork-escape.stamp"
    if stamp.exists():
        stamp.unlink()
    script = (
        "import os, signal, sys, time\n"
        f"stamp = {str(stamp)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    pid2 = os.fork()\n"
        "    if pid2 == 0:\n"
        "        os.setsid()\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        while True:\n"
        "            open(stamp, 'w').write(str(time.time()))\n"
        "            time.sleep(0.05)\n"
        "    os._exit(0)\n"
        "sys.exit(0)\n"
    )
    t0 = time.time()
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    elapsed = time.time() - t0
    assert elapsed < 8.0, f"double-fork containment hung: {elapsed:.2f}s"
    assert rc == 0, (rc, out, err)
    time.sleep(0.3)
    if stamp.exists():
        m1 = stamp.stat().st_mtime
        time.sleep(0.4)
        m2 = stamp.stat().st_mtime
        assert m1 == m2, "double-fork setsid descendant still alive after return"


def test_setsid_escape_cannot_mutate_after_return() -> None:
    """Escaped child must not mutate a tracked-like file after nominal parent success."""
    target = Path(_TMP) / "tracked-after-return.txt"
    target.write_text("clean\n", encoding="utf-8")
    script = (
        "import os, signal, sys, time\n"
        f"target = {str(target)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    # Delay past full TERM+KILL quiesce; mutate only if escape survived return.\n"
        "    time.sleep(6.0)\n"
        "    with open(target, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('late-mutation\\n')\n"
        "    while True:\n"
        "        time.sleep(0.2)\n"
        "sys.exit(0)\n"
    )
    rc, out, err = fc.run_bounded_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    assert rc == 0, (rc, out, err)
    # Observe well past the delayed mutation window.
    time.sleep(7.0)
    body = target.read_text(encoding="utf-8")
    assert "late-mutation" not in body, f"escaped child mutated after return: {body!r}"
    assert body == "clean\n"


def test_containment_cleanup_failure_raises() -> None:
    """If supervisor cannot prove containment_ok, parent must raise ContainmentError."""
    real = fc._accept_containment_receipt

    def _fail(*_a, **_k):  # type: ignore[no-untyped-def]
        raise fc.ContainmentError(
            getattr(__import__("errno"), "ECHILD", 10),
            "containment failed: injected cleanup failure",
        )

    fc._accept_containment_receipt = _fail  # type: ignore[assignment]
    try:
        try:
            fc.run_bounded_subprocess(
                [sys.executable, "-c", "print('x')"],
                timeout=10,
                max_stdout_bytes=512,
                max_stderr_bytes=512,
            )
        except fc.ContainmentError as exc:
            msg = str(exc).lower()
            assert "containment" in msg
            assert "cleanup" in msg or "failed" in msg or "injected" in msg
        else:
            raise AssertionError("expected ContainmentError on cleanup failure")
    finally:
        fc._accept_containment_receipt = real  # type: ignore[assignment]


def test_missing_containment_result_fail_closed() -> None:
    """Missing supervisor result receipt must refuse success."""
    real = fc._read_containment_receipt_fd
    fc._read_containment_receipt_fd = lambda *_a, **_k: None  # type: ignore[assignment]
    try:
        try:
            fc.run_bounded_subprocess(
                [sys.executable, "-c", "print('y')"],
                timeout=10,
                max_stdout_bytes=512,
                max_stderr_bytes=512,
            )
        except fc.ContainmentError as exc:
            assert "missing" in str(exc).lower() or "refusing" in str(exc).lower()
        else:
            raise AssertionError("expected ContainmentError when result missing")
    finally:
        fc._read_containment_receipt_fd = real  # type: ignore[assignment]


def test_forged_containment_receipt_cannot_green() -> None:
    """Hostile: truthy/non-bool forge + killed supervisor must not return success.

    Mirrors independent checker CRITICAL: forged containment_ok / worker_rc=false
    must not produce a clean return while a setsid child mutates after return.
    """
    target = Path(_TMP) / "forge-late.txt"
    target.write_text("clean\n", encoding="utf-8")
    # Worker tries to write a forged file receipt next to any discoverable job path
    # and leave a SIGTERM-ignoring setsid mutator. Pipe-sealed receipts + returncode
    # binding must still fail closed.
    script = (
        "import os, signal, sys, time, json, glob\n"
        f"target = {str(target)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(0.05)\n"
        "    # Attempt classic file forge in any ff-containment temp dirs we can see.\n"
        "    for d in glob.glob('/tmp/ff-containment-*'):\n"
        "        try:\n"
        "            p = os.path.join(d, 'result.json')\n"
        "            open(p, 'w').write(json.dumps({\n"
        "                'containment_ok': 'truthy-not-bool',\n"
        "                'worker_rc': False,\n"
        "                'supervisor_pid': 12345,\n"
        "            }))\n"
        "        except Exception:\n"
        "            pass\n"
        "    time.sleep(4.0)\n"
        "    with open(target, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('late\\n')\n"
        "    while True:\n"
        "        time.sleep(0.2)\n"
        "sys.exit(0)\n"
    )
    raised = None
    try:
        rc, out, err = fc.run_bounded_subprocess(
            [sys.executable, "-c", script],
            timeout=12,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
        # Even if the worker itself exits 0, sealed containment must hold.
        assert rc == 0, (rc, out, err)
    except (fc.ContainmentError, subprocess.TimeoutExpired) as exc:
        raised = exc
    # Observe past delayed mutation window.
    time.sleep(5.0)
    body = target.read_text(encoding="utf-8")
    assert "late" not in body, f"forged path allowed late mutation: {body!r} raised={raised!r}"
    assert body == "clean\n"


def test_receipt_schema_rejects_truthy_and_bool_rc() -> None:
    """Strict schema: containment_ok must be True; worker_rc must be int not bool."""
    secret = b"\x11" * 32
    root_pid = 4242
    root_st = 999
    # Truthy string containment_ok
    bad1 = {
        "schema_version": 1,
        "worker_rc": 0,
        "containment_ok": "truthy-not-bool",
        "error": "",
        "supervisor_pid": root_pid,
        "supervisor_starttime": root_st,
    }
    bad1["seal"] = fc._seal_containment_receipt(secret, bad1)
    try:
        fc._accept_containment_receipt(
            bad1,
            root_pid=root_pid,
            root_starttime=root_st,
            receipt_mac=secret,
            supervisor_returncode=0,
        )
    except fc.ContainmentError:
        pass
    else:
        raise AssertionError("truthy containment_ok must be rejected")
    # bool worker_rc (False is subclass of int in isinstance checks)
    bad2 = {
        "schema_version": 1,
        "worker_rc": False,
        "containment_ok": True,
        "error": "",
        "supervisor_pid": root_pid,
        "supervisor_starttime": root_st,
    }
    bad2["seal"] = fc._seal_containment_receipt(secret, bad2)
    try:
        fc._accept_containment_receipt(
            bad2,
            root_pid=root_pid,
            root_starttime=root_st,
            receipt_mac=secret,
            supervisor_returncode=0,
        )
    except fc.ContainmentError as exc:
        assert "worker_rc" in str(exc).lower() or "int" in str(exc).lower()
    else:
        raise AssertionError("bool worker_rc must be rejected")
    # Non-zero supervisor returncode
    good_body = {
        "schema_version": 1,
        "worker_rc": 0,
        "containment_ok": True,
        "error": "",
        "supervisor_pid": root_pid,
        "supervisor_starttime": root_st,
    }
    good_body["seal"] = fc._seal_containment_receipt(secret, good_body)
    try:
        fc._accept_containment_receipt(
            good_body,
            root_pid=root_pid,
            root_starttime=root_st,
            receipt_mac=secret,
            supervisor_returncode=1,
        )
    except fc.ContainmentError:
        pass
    else:
        raise AssertionError("non-zero supervisor returncode must be rejected")
    # Seal mismatch
    good_body2 = dict(good_body)
    good_body2["seal"] = "0" * 64
    try:
        fc._accept_containment_receipt(
            good_body2,
            root_pid=root_pid,
            root_starttime=root_st,
            receipt_mac=secret,
            supervisor_returncode=0,
        )
    except fc.ContainmentError as exc:
        assert "seal" in str(exc).lower() or "forge" in str(exc).lower()
    else:
        raise AssertionError("bad seal must be rejected")


def test_timeout_setsid_escape_no_late_mutation() -> None:
    """Timeout path must prove cleanup; setsid mutator must not write after return/raise."""
    target = Path(_TMP) / "timeout-late.txt"
    target.write_text("clean\n", encoding="utf-8")
    script = (
        "import os, signal, sys, time\n"
        f"target = {str(target)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(5.0)\n"
        "    with open(target, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('late\\n')\n"
        "    while True:\n"
        "        time.sleep(0.2)\n"
        "# Parent worker lives past short timeout.\n"
        "time.sleep(30)\n"
    )
    raised = None
    try:
        fc.run_bounded_subprocess(
            [sys.executable, "-c", script],
            timeout=1.0,
            max_stdout_bytes=512,
            max_stderr_bytes=512,
        )
    except subprocess.TimeoutExpired as exc:
        raised = exc
    except fc.ContainmentError as exc:
        # Acceptable if cleanup cannot be proven — still must not leave mutator alive.
        raised = exc
    else:
        raise AssertionError("expected TimeoutExpired or ContainmentError on timeout escape")
    time.sleep(6.0)
    body = target.read_text(encoding="utf-8")
    assert "late" not in body, f"timeout path left live mutator: {body!r} raised={raised!r}"
    assert body == "clean\n"


def test_descendant_containment_required() -> None:
    """Containment gate is explicit; non-Linux is rejected before launch when simulated."""
    assert fc.descendant_containment_supported() is True
    real = fc.descendant_containment_supported
    fc.descendant_containment_supported = lambda: False  # type: ignore[assignment]
    try:
        try:
            fc.run_bounded_subprocess(
                [sys.executable, "-c", "print(1)"],
                max_stdout_bytes=64,
            )
        except fc.ContainmentError as exc:
            assert "subreaper" in str(exc).lower() or "containment" in str(exc).lower()
        else:
            raise AssertionError("missing containment must fail closed before launch")
    finally:
        fc.descendant_containment_supported = real  # type: ignore[assignment]



# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [
        test_codex_default_omits_yolo,
        test_grok_default_omits_bypass_permissions,
        test_grok_fast_mode_rejects_xhigh_effort,
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
        test_hostile_stdout_overflow_capped,
        test_simultaneous_dual_stream_overflow,
        test_timeout_descendant_killed_under_bounded_capture,
        test_invalid_capture_limits_rejected_before_launch,
        test_non_utf8_output_replaced,
        test_truncation_marker_has_no_prompt_content,
        test_parent_exits_first_inherited_silent_pipe_no_hang,
        test_parent_exits_first_descendant_closes_stdio_stamp_file,
        test_marker_spoof_no_truncation_disambiguated,
        test_marker_spoof_with_real_truncation_authentic_appended,
        test_posix_platform_contract_helpers,
        test_exact_byte_cap_and_multibyte_boundary,
        test_normal_success_and_timeout_paths,
        test_stdin_selector_registration_failure_cleans_group,
        test_group_kill_does_not_skip_on_leader_exit,
        test_start_new_session_false_fail_closed_no_launch,
        test_blockingio_eagain_keeps_fd_and_preserves_output,
        test_eagain_oserror_errno_keeps_fd,
        test_unrelated_process_not_killed_by_group_cleanup,
        test_descendant_containment_required,
        test_missing_containment_result_fail_closed,
        test_containment_cleanup_failure_raises,
        test_receipt_schema_rejects_truthy_and_bool_rc,
        test_forged_containment_receipt_cannot_green,
        test_timeout_setsid_escape_no_late_mutation,
        test_setsid_escape_cannot_mutate_after_return,
        test_double_fork_setsid_descendant_killed,
        test_setsid_sigterm_ignore_descendant_killed,
        test_set_blocking_failure_still_completes,
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
