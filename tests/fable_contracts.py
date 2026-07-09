#!/usr/bin/env python3
"""Offline contract tests for FableFuse (no live Codex/Claude)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="fable-contract-")
os.environ.setdefault("FABLE_CONFIG_DIR", str(Path(_TMP) / "config"))
os.environ.setdefault("FABLE_STATE_DIR", str(Path(_TMP) / "state"))
os.environ.setdefault("FABLE_RUNS_DIR", str(Path(_TMP) / "runs"))
os.environ["FABLE_CODEX_CMD"] = "echo"
os.environ["FABLE_ADVISOR_CMD"] = "echo"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fable_common as fc  # noqa: E402
import fable_advisor  # noqa: E402

SLACK = 120  # truncation marker headroom beyond MAX_RETURN_CHARS


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


def _load_hook(rel_path: str):
    path = ROOT / rel_path
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"fable_hook_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _run_hook(script: Path, payload: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(ROOT),
    )


def _pretool_payload(session_id: str, tool_name: str = "Write", tool_input: dict | None = None) -> dict:
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input or {"file_path": "/tmp/fable-contract.txt", "content": "x"},
    }


def _stop_payload(session_id: str) -> dict:
    return {"session_id": session_id, "hook_event_name": "Stop"}


def _run_dispatch(args: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ROOT / "fable_dispatch.py"), *args],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT),
    )


def _pretool_denied(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode != 0:
        return True
    out = (proc.stdout or "").strip()
    if not out:
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    hso = data.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny"


def _pretool_allowed(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode == 0 and not (proc.stdout or "").strip()


def test_resolve_config_precedence() -> None:
    sid = "contract-precedence"
    fc.clear_state(sid)
    old_model = _env("FABLE_CODEX_MODEL", "env-model")
    old_effort = _env("FABLE_CODEX_EFFORT", "low")
    try:
        fc.save_global_config({"codex_model": "file-model", "codex_effort": "medium"})
        fc.write_state(sid, config={"codex_model": "session-model", "codex_effort": "high"})

        cfg = fc.resolve_config(session_id=sid)
        assert cfg["codex_model"] == "session-model"
        assert cfg["codex_effort"] == "high"

        cfg = fc.resolve_config(overrides={"codex_model": "override-model"}, session_id=sid)
        assert cfg["codex_model"] == "override-model"
        assert cfg["codex_effort"] == "high"

        fc.clear_state(sid)
        cfg = fc.resolve_config()
        assert cfg["codex_model"] == "file-model"
        assert cfg["codex_effort"] == "medium"

        fc.save_global_config({})
        try:
            fc.GLOBAL_CONFIG.unlink()
        except (FileNotFoundError, OSError):
            pass
        cfg = fc.resolve_config()
        assert cfg["codex_model"] == "env-model"
        assert cfg["codex_effort"] == "low"
    finally:
        _restore("FABLE_CODEX_MODEL", old_model)
        _restore("FABLE_CODEX_EFFORT", old_effort)
        fc.clear_state(sid)
        try:
            fc.GLOBAL_CONFIG.unlink()
        except (FileNotFoundError, OSError):
            pass


def test_build_codex_command_fast_swaps_effort() -> None:
    assert fc.build_codex_command(fc.resolve_config()) == ["echo"]
    old_cmd = _env("FABLE_CODEX_CMD", None)
    old_yolo = _env("FABLE_CODEX_YOLO", "1")
    try:
        cfg = {
            "codex_model": "gpt-5.5-codex",
            "codex_effort": "high",
            "fast": True,
            "fast_effort": "low",
            "fast_model": "gpt-fast-lite",
            "fable_model": "claude-fable-5",
        }
        cmd = fc.build_codex_command(cfg)
        assert cmd == [
            "codex", "exec", "--yolo", "--model", "gpt-fast-lite",
            "-c", "model_reasoning_effort=low", "-",
        ], cmd

        cfg["fast"] = False
        cmd = fc.build_codex_command(cfg)
        assert cmd == [
            "codex", "exec", "--yolo", "--model", "gpt-5.5-codex",
            "-c", "model_reasoning_effort=high", "-",
        ], cmd

        os.environ["FABLE_CODEX_YOLO"] = "0"
        assert "--yolo" not in fc.build_codex_command(cfg)
    finally:
        _restore("FABLE_CODEX_YOLO", old_yolo)
        _restore("FABLE_CODEX_CMD", old_cmd)
        os.environ["FABLE_CODEX_CMD"] = "echo"


def test_build_body_command_executor() -> None:
    old_cmd = _env("FABLE_CODEX_CMD", None)      # unset echo so codex builds a real command
    old_body = _env("FABLE_BODY_CMD", None)
    old_exec = _env("FABLE_EXECUTOR", None)
    old_opus = _env("FABLE_OPUS_CMD", None)
    old_grok = _env("FABLE_GROK_CMD", None)
    old_grok_yolo = _env("FABLE_GROK_YOLO", None)
    old_grok_permission = _env("FABLE_GROK_PERMISSION_MODE", None)
    old_grok_effort = _env("FABLE_GROK_EFFORT", None)
    try:
        codex_cfg = fc.resolve_config(overrides={"executor": "codex"})
        assert fc.build_body_command(codex_cfg)[0] == "codex"

        sonnet_cfg = fc.resolve_config(overrides={"executor": "sonnet", "sonnet_model": "claude-sonnet-5"})
        assert fc.build_body_command(sonnet_cfg) == ["claude", "-p", "--model", "claude-sonnet-5"]

        opus_cfg = fc.resolve_config(overrides={"executor": "opus", "opus_model": "claude-opus-4-8"})
        assert fc.build_body_command(opus_cfg) == ["claude", "-p", "--model", "claude-opus-4-8"]
        assert fc.build_fable_command(opus_cfg) == ["echo"]

        grok_cfg = fc.resolve_config(overrides={"executor": "grok", "grok_model": "grok-4.5"})
        assert fc.build_body_command(grok_cfg) == [
            "grok", "--model", "grok-4.5", "--reasoning-effort", "high",
            "--permission-mode", "bypassPermissions", "--prompt-file", "{prompt_file}",
        ]
        final_cmd, stdin, cleanup = fc._prepare_prompt_command(
            fc.build_body_command(grok_cfg), "hello from grok"
        )
        try:
            assert stdin is None
            assert "{prompt_file}" not in final_cmd
            assert final_cmd[-2] == "--prompt-file"
            assert Path(final_cmd[-1]).read_text() == "hello from grok"
        finally:
            for path in cleanup:
                Path(path).unlink(missing_ok=True)

        fast_grok_cfg = fc.resolve_config(overrides={
            "executor": "grok", "grok_model": "grok-4.5", "grok_effort": "high",
            "fast": True, "fast_effort": "low",
        })
        assert "--reasoning-effort" in fc.build_body_command(fast_grok_cfg)
        assert fc.build_body_command(fast_grok_cfg)[
            fc.build_body_command(fast_grok_cfg).index("--reasoning-effort") + 1
        ] == "low"

        os.environ["FABLE_GROK_EFFORT"] = "medium"
        env_grok_cfg = fc.resolve_config(overrides={"executor": "grok", "grok_model": "grok-4.5"})
        assert fc.build_body_command(env_grok_cfg)[
            fc.build_body_command(env_grok_cfg).index("--reasoning-effort") + 1
        ] == "medium"
        os.environ.pop("FABLE_GROK_EFFORT", None)

        os.environ["FABLE_GROK_YOLO"] = "0"
        assert "--permission-mode" not in fc.build_body_command(grok_cfg)
        os.environ["FABLE_GROK_PERMISSION_MODE"] = "auto"
        auto_cmd = fc.build_body_command(grok_cfg)
        assert auto_cmd[auto_cmd.index("--permission-mode") + 1] == "auto"
        os.environ.pop("FABLE_GROK_YOLO", None)
        os.environ.pop("FABLE_GROK_PERMISSION_MODE", None)

        os.environ["FABLE_OPUS_CMD"] = "opus-runner --flag"
        assert fc.build_body_command(opus_cfg) == ["opus-runner", "--flag"]
        os.environ.pop("FABLE_OPUS_CMD", None)

        os.environ["FABLE_GROK_CMD"] = "grok-runner --flag"
        assert fc.build_body_command(grok_cfg) == ["grok-runner", "--flag"]
        os.environ.pop("FABLE_GROK_CMD", None)

        os.environ["FABLE_BODY_CMD"] = "my-runner --flag"      # universal override wins
        assert fc.build_body_command(codex_cfg) == ["my-runner", "--flag"]
        assert fc.build_body_command(opus_cfg) == ["my-runner", "--flag"]
        assert fc.build_body_command(grok_cfg) == ["my-runner", "--flag"]
    finally:
        _restore("FABLE_CODEX_CMD", old_cmd)
        _restore("FABLE_BODY_CMD", old_body)
        _restore("FABLE_EXECUTOR", old_exec)
        _restore("FABLE_OPUS_CMD", old_opus)
        _restore("FABLE_GROK_CMD", old_grok)
        _restore("FABLE_GROK_YOLO", old_grok_yolo)
        _restore("FABLE_GROK_PERMISSION_MODE", old_grok_permission)
        _restore("FABLE_GROK_EFFORT", old_grok_effort)
        os.environ["FABLE_CODEX_CMD"] = "echo"


def test_advisor_prompt_uses_selected_opus_lead() -> None:
    cfg = fc.resolve_config(overrides={"executor": "opus", "opus_model": "claude-opus-4-8"})
    prompt = fable_advisor._build_advisor_prompt(
        "How should the lead route this refactor?",
        "",
        fable_advisor._lead_description(cfg),
    )
    assert "Opus (claude-opus-4-8) is the EXECUTOR/LEAD" in prompt
    assert "Your role is ADVISOR ONLY" in prompt


def test_make_verdict_and_fresh_green() -> None:
    green = fc.make_verdict("pytest -q", 0, "abc", ["a.py"], 100.0, 90.0)
    red = fc.make_verdict("pytest -q", 1, "abc", ["a.py"], 100.0, 90.0)
    assert green["result"] == "GREEN"
    assert red["result"] == "RED"

    assert fc.verdict_is_fresh_green(green, 100.0) is True
    assert fc.verdict_is_fresh_green(green, 100.5) is False
    assert fc.verdict_is_fresh_green(red, 0.0) is False
    assert fc.verdict_is_fresh_green(None, 0.0) is False

    stale = fc.make_verdict("pytest -q", 0, "abc", [], 50.0, 50.0)
    assert fc.verdict_is_fresh_green(stale, 60.0) is False


def test_state_read_write_merge_clear() -> None:
    sid = "contract-state"
    fc.clear_state(sid)
    try:
        assert fc.read_state(sid)["armed"] is False
        fc.write_state(sid, armed=True, last_dispatch_ts=42.0)
        st = fc.read_state(sid)
        assert st["armed"] is True
        assert st["last_dispatch_ts"] == 42.0

        fc.write_state(sid, config={"codex_effort": "low"})
        fc.write_state(sid, config={"fast": True})
        st = fc.read_state(sid)
        assert st["config"]["codex_effort"] == "low"
        assert st["config"]["fast"] is True

        v = fc.make_verdict("true", 0, "sha", [], 99.0, 42.0)
        fc.write_state(sid, verdict=v)
        assert fc.read_state(sid)["verdict"]["result"] == "GREEN"

        p = fc.state_path(sid)
        assert p.is_file()
        fc.clear_state(sid)
        assert not p.exists()
    finally:
        fc.clear_state(sid)


def test_handoff_card_bounded_with_artifact() -> None:
    runs = Path(_TMP) / "handoff-runs"
    raw = "Finding: keep this\n" + ("detail-line " * 900) + "\nPASS verification"
    run_id = fc.new_run_id()
    artifact = fc.write_artifact(runs, run_id, "worker-0", "contract task", raw)
    card = fc.handoff_card("worker-0", "contract task", raw, artifact)

    assert card["artifact"] == artifact["path"]
    assert card["raw_sha256"] == artifact["sha256"]
    assert artifact["sha256"]
    assert Path(card["artifact"]).is_file()
    assert len(card["summary"]) <= fc.MAX_RETURN_CHARS + SLACK


def test_run_engine_cleans_prompt_file() -> None:
    """Grok-style prompt files must be deleted after the body command returns."""
    script = (
        "import pathlib, sys; "
        "p = pathlib.Path(sys.argv[1]); "
        "print(p); "
        "print(p.read_text())"
    )
    rc, out, err = fc.run_engine([sys.executable, "-c", script, "{prompt_file}"], "large grok spec")
    assert rc == 0, f"prompt-file body failed: stdout={out!r} stderr={err!r}"
    first_line = (out or "").splitlines()[0]
    assert "large grok spec" in out
    assert not Path(first_line).exists(), "managed prompt file must be removed after run_engine returns"


def test_guards_off_honors_kill_switches() -> None:
    old_fable = _env("FABLE_GUARDS_OFF", None)
    old_claude = _env("CLAUDE_GUARDS_OFF", None)
    try:
        os.environ.pop("FABLE_GUARDS_OFF", None)
        os.environ.pop("CLAUDE_GUARDS_OFF", None)
        assert fc.guards_off() is False

        os.environ["FABLE_GUARDS_OFF"] = "1"
        assert fc.guards_off() is True
        os.environ.pop("FABLE_GUARDS_OFF", None)

        os.environ["CLAUDE_GUARDS_OFF"] = "yes"
        assert fc.guards_off() is True
    finally:
        _restore("FABLE_GUARDS_OFF", old_fable)
        _restore("CLAUDE_GUARDS_OFF", old_claude)


def test_pretool_gate_contracts() -> None:
    gate = ROOT / "hooks" / "fable_gate.py"
    if not gate.is_file() or _load_hook("hooks/fable_gate.py") is None:
        return

    sid = "contract-pretool"
    fc.clear_state(sid)
    old_guards = _env("FABLE_GUARDS_OFF", None)
    try:
        fc.write_state(sid, armed=True)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"))
        assert _pretool_denied(proc), f"armed Write should deny; stdout={proc.stdout!r} stderr={proc.stderr!r}"

        fc.write_state(sid, armed=False)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"))
        assert _pretool_allowed(proc), f"unarmed Write should allow; stdout={proc.stdout!r} stderr={proc.stderr!r}"

        fc.write_state(sid, armed=True)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"), extra_env={"FABLE_GUARDS_OFF": "1"})
        assert _pretool_allowed(proc), f"kill-switch should allow; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    finally:
        _restore("FABLE_GUARDS_OFF", old_guards)
        fc.clear_state(sid)


def test_pretool_gate_blocks_bash_chaining() -> None:
    """Regression: an allowlisted prefix must not let a chained command through
    (e.g. "git status -sb && rm -rf ..."). A prefix match alone is not enough."""
    gate = ROOT / "hooks" / "fable_gate.py"
    if not gate.is_file() or _load_hook("hooks/fable_gate.py") is None:
        return

    sid = "contract-bash-chain"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=True)

        chained = _pretool_payload(sid, "Bash", {"command": "git status -sb && rm -rf /tmp/should-not-run"})
        proc = _run_hook(gate, chained)
        assert _pretool_denied(proc), (
            f"chained command via an allowlisted prefix must be denied; "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

        for sep in (";", "|", "`", "$(", "\n", ">"):
            payload = _pretool_payload(sid, "Bash", {"command": f"git status {sep} echo pwned"})
            proc = _run_hook(gate, payload)
            assert _pretool_denied(proc), f"separator {sep!r} should be denied; stdout={proc.stdout!r}"

        simple = _pretool_payload(sid, "Bash", {"command": "git status -sb"})
        proc = _run_hook(gate, simple)
        assert _pretool_allowed(proc), (
            f"a plain allowlisted command must still be allowed; "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
    finally:
        fc.clear_state(sid)


def test_pretool_gate_allows_realistic_invocation_forms() -> None:
    """Regression (found via live dogfooding, not a synthetic test): a just-armed session's own
    brain naturally invokes allowlisted scripts as `python3 <script> ...` and sometimes prefixes a
    scoped env var (`FOO=bar cmd`) — neither form matched a literal prefix check, so the brain
    immediately got blocked fighting its own gate on the most natural invocations. These must be
    ALLOWED (the metacharacter/chaining check must still catch anything actually dangerous)."""
    gate = ROOT / "hooks" / "fable_gate.py"
    if not gate.is_file() or _load_hook("hooks/fable_gate.py") is None:
        return

    sid = "contract-realistic-invocations"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=True)

        allowed_commands = [
            "python3 fable_dispatch.py --help",
            "python3 fable_dispatch.py arm",
            "FABLE_GUARDS_OFF=1 python3 fable_dispatch.py --help",
            "FABLE_GATE_ALLOW_TRIVIAL=1 wc -l fable_dispatch.py hooks/fable_gate.py fable_common.py",
            "fable-dispatch disarm",
            "python3 fable_verify.py --gate \"true\"",
        ]
        for cmd in allowed_commands:
            proc = _run_hook(gate, _pretool_payload(sid, "Bash", {"command": cmd}))
            assert _pretool_allowed(proc), f"{cmd!r} must be allowed; stdout={proc.stdout!r}"

        # normalization must never defeat the chaining check, even with an env/interpreter prefix
        still_denied = [
            "python3 fable_dispatch.py --help && rm -rf /tmp/x",
            "FABLE_GUARDS_OFF=1 python3 fable_dispatch.py --help; rm -rf /tmp/x",
        ]
        for cmd in still_denied:
            proc = _run_hook(gate, _pretool_payload(sid, "Bash", {"command": cmd}))
            assert _pretool_denied(proc), f"{cmd!r} must still be denied; stdout={proc.stdout!r}"
    finally:
        fc.clear_state(sid)


def test_session_id_defaults_to_claude_code_session_id() -> None:
    """Regression: without FABLE_SESSION_ID, fable-dispatch must key state by
    CLAUDE_CODE_SESSION_ID (what the real PreToolUse/Stop hooks receive) — not the
    literal string "default", or the hard gate never engages in a real session."""
    fake_session = "contract-claude-code-session-id-12345"
    fc.clear_state(fake_session)
    fc.clear_state("default")
    old = _env("FABLE_SESSION_ID", None)
    try:
        proc = _run_dispatch(["arm"], extra_env={"CLAUDE_CODE_SESSION_ID": fake_session})
        assert proc.returncode == 0, f"arm failed: {proc.stdout!r} {proc.stderr!r}"
        assert fc.read_state(fake_session)["armed"] is True, (
            "state must be written under CLAUDE_CODE_SESSION_ID when FABLE_SESSION_ID is unset")
        assert fc.read_state("default")["armed"] is False, "must NOT fall through to 'default'"
    finally:
        _restore("FABLE_SESSION_ID", old)
        fc.clear_state(fake_session)
        fc.clear_state("default")


def test_dispatch_config_accepts_opus_executor() -> None:
    """Reverse-advisor lead selection: Opus can be the lead/body executor while
    the Fable advisor command remains independently configured."""
    sid = "contract-opus-executor"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            ["config", "--executor", "opus", "--opus-model", "claude-opus-4-8"],
            extra_env={"FABLE_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"config opus failed: {proc.stdout!r} {proc.stderr!r}"
        cfg = json.loads(proc.stdout)
        assert cfg["executor"] == "opus"
        assert cfg["opus_model"] == "claude-opus-4-8"
        assert cfg["fable_model"] == "claude-fable-5"

        proc = _run_dispatch(
            ["--dry-run", "--executor", "opus", "--opus-model", "claude-opus-4-8", "lead with Opus; ask Fable"],
            extra_env={"FABLE_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"opus dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "opus"
        assert payload["mode"]["opus_model"] == "claude-opus-4-8"
        assert "claude -p --model claude-opus-4-8" in payload["cards"][0]["summary"]
    finally:
        fc.clear_state(sid)


def test_dispatch_config_accepts_grok_executor() -> None:
    """Grok Build can be the lead/body executor via the local grok CLI."""
    sid = "contract-grok-executor"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            ["config", "--executor", "grok", "--grok-model", "grok-4.5", "--effort", "medium"],
            extra_env={"FABLE_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"config grok failed: {proc.stdout!r} {proc.stderr!r}"
        cfg = json.loads(proc.stdout)
        assert cfg["executor"] == "grok"
        assert cfg["grok_model"] == "grok-4.5"
        assert cfg["grok_effort"] == "medium"
        assert cfg["fable_model"] == "claude-fable-5"

        proc = _run_dispatch(
            ["--dry-run", "--executor", "grok", "--grok-model", "grok-4.5", "lead with Grok; ask Fable"],
            extra_env={"FABLE_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"grok dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "grok"
        assert payload["mode"]["grok_model"] == "grok-4.5"
        assert "grok --model grok-4.5" in payload["cards"][0]["summary"]
        assert "--reasoning-effort medium" in payload["cards"][0]["summary"]
        assert "--prompt-file <prompt-file>" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            ["doctor"],
            extra_env={"FABLE_SESSION_ID": sid, "FABLE_GROK_CMD": f"{sys.executable} -c pass"},
        )
        assert proc.returncode == 0, f"doctor grok override failed: {proc.stdout!r} {proc.stderr!r}"
        assert "grok build CLI" in proc.stdout
        assert sys.executable in proc.stdout
    finally:
        fc.clear_state(sid)


def test_cmd_done_refuses_without_fresh_green() -> None:
    """Regression: `done` must not disarm without a fresh GREEN verdict — otherwise the brain
    can always run the (Bash-allowlisted) `fable-dispatch done` to kill the gate on demand."""
    sid = "contract-done-refuses"
    fc.clear_state(sid)
    try:
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None)
        proc = _run_dispatch(["done"], extra_env={"FABLE_SESSION_ID": sid})
        assert proc.returncode != 0, f"done without a verdict must fail; stdout={proc.stdout!r}"
        assert fc.read_state(sid)["armed"] is True, "gate must stay armed without a fresh GREEN"

        stale = fc.make_verdict("pytest -q", 0, "sha", [], 90.0, 90.0)
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=stale)
        proc = _run_dispatch(["done"], extra_env={"FABLE_SESSION_ID": sid})
        assert proc.returncode != 0, f"done with a stale GREEN must still fail; stdout={proc.stdout!r}"
        assert fc.read_state(sid)["armed"] is True

        fresh = fc.make_verdict("pytest -q", 0, "sha", [], 110.0, 100.0)
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=fresh)
        proc = _run_dispatch(["done"], extra_env={"FABLE_SESSION_ID": sid})
        assert proc.returncode == 0, f"done with a fresh GREEN must succeed; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert fc.read_state(sid)["armed"] is False, "gate must disarm on a fresh GREEN"
    finally:
        fc.clear_state(sid)


def test_stop_gate_contracts() -> None:
    stop = ROOT / "hooks" / "fable_verify_gate.py"
    if not stop.is_file() or _load_hook("hooks/fable_verify_gate.py") is None:
        return

    sid = "contract-stop"
    fc.clear_state(sid)
    old_guards = _env("FABLE_GUARDS_OFF", None)
    try:
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None)
        proc = _run_hook(stop, _stop_payload(sid))
        assert proc.returncode == 2, f"no fresh GREEN should block Stop; rc={proc.returncode}"

        stale = fc.make_verdict("pytest -q", 0, "sha", [], 90.0, 90.0)
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=stale)
        proc = _run_hook(stop, _stop_payload(sid))
        assert proc.returncode == 2, f"stale GREEN should block Stop; rc={proc.returncode}"

        fresh = fc.make_verdict("pytest -q", 0, "sha", [], 110.0, 100.0)
        fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=fresh)
        proc = _run_hook(stop, _stop_payload(sid))
        assert proc.returncode == 0, f"fresh GREEN should allow Stop; rc={proc.returncode} stderr={proc.stderr!r}"
    finally:
        _restore("FABLE_GUARDS_OFF", old_guards)
        fc.clear_state(sid)


def main() -> int:
    tests = [
        test_resolve_config_precedence,
        test_build_codex_command_fast_swaps_effort,
        test_build_body_command_executor,
        test_advisor_prompt_uses_selected_opus_lead,
        test_make_verdict_and_fresh_green,
        test_state_read_write_merge_clear,
        test_handoff_card_bounded_with_artifact,
        test_run_engine_cleans_prompt_file,
        test_guards_off_honors_kill_switches,
        test_pretool_gate_contracts,
        test_pretool_gate_blocks_bash_chaining,
        test_pretool_gate_allows_realistic_invocation_forms,
        test_session_id_defaults_to_claude_code_session_id,
        test_dispatch_config_accepts_opus_executor,
        test_dispatch_config_accepts_grok_executor,
        test_cmd_done_refuses_without_fresh_green,
        test_stop_gate_contracts,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"fable_contracts: FAIL ({failed})", file=sys.stderr)
        return 1
    print("fable_contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
