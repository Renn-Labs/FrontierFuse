#!/usr/bin/env python3
"""Offline contract tests for FrontierFuse (no live Codex/Claude)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="frontier-contract-")
os.environ.setdefault("FRONTIER_CONFIG_DIR", str(Path(_TMP) / "config"))
os.environ.setdefault("FRONTIER_STATE_DIR", str(Path(_TMP) / "state"))
os.environ.setdefault("FRONTIER_RUNS_DIR", str(Path(_TMP) / "runs"))
os.environ["FRONTIER_CODEX_CMD"] = "echo"
os.environ["FRONTIER_ADVISOR_CMD"] = "echo"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontier_common as fc  # noqa: E402
import frontier_advisor  # noqa: E402
import frontier_verify  # noqa: E402

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
    assert path.is_file(), f"hook missing: {path}"
    spec = importlib.util.spec_from_file_location(f"frontier_hook_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"cannot load hook: {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
        "tool_input": tool_input or {"file_path": "/tmp/frontier-contract.txt", "content": "x"},
    }


def _stop_payload(session_id: str) -> dict:
    return {"session_id": session_id, "hook_event_name": "Stop"}


def _run_dispatch(args: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), *args],
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
    old_model = _env("FRONTIER_CODEX_MODEL", "env-model")
    old_effort = _env("FRONTIER_CODEX_EFFORT", "low")
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
        _restore("FRONTIER_CODEX_MODEL", old_model)
        _restore("FRONTIER_CODEX_EFFORT", old_effort)
        fc.clear_state(sid)
        try:
            fc.GLOBAL_CONFIG.unlink()
        except (FileNotFoundError, OSError):
            pass


def test_build_codex_command_fast_swaps_effort() -> None:
    assert fc.build_codex_command(fc.resolve_config()) == ["echo"]
    old_cmd = _env("FRONTIER_CODEX_CMD", None)
    old_yolo = _env("FRONTIER_CODEX_YOLO", None)
    try:
        cfg = {
            "codex_model": "codex-test-model",
            "codex_effort": "high",
            "fast": True,
            "fast_effort": "low",
            "fast_model": "fast-test-model",
            "frontier_model": "advisor-test-model",
        }
        cmd = fc.build_codex_command(cfg)
        assert cmd == [
            "codex", "exec", "--model", "fast-test-model",
            "-c", "model_reasoning_effort=low", "-",
        ], cmd

        cfg["fast_model"] = None
        cmd = fc.build_codex_command(cfg)
        assert cmd[cmd.index("--model") + 1] == "codex-test-model"

        cfg["fast_model"] = ""
        cmd = fc.build_codex_command(cfg)
        assert "--model" not in cmd, cmd

        cfg["fast"] = False
        cmd = fc.build_codex_command(cfg)
        assert cmd == [
            "codex", "exec", "--model", "codex-test-model",
            "-c", "model_reasoning_effort=high", "-",
        ], cmd

        os.environ["FRONTIER_CODEX_YOLO"] = "1"
        assert "--yolo" in fc.build_codex_command(cfg)
    finally:
        _restore("FRONTIER_CODEX_YOLO", old_yolo)
        _restore("FRONTIER_CODEX_CMD", old_cmd)
        os.environ["FRONTIER_CODEX_CMD"] = "echo"


def test_empty_fast_model_environment_selects_account_default() -> None:
    old_fast = _env("FRONTIER_CODEX_FAST", "1")
    old_model = _env("FRONTIER_CODEX_MODEL", "pinned-model")
    old_fast_model = _env("FRONTIER_CODEX_FAST_MODEL", "")
    try:
        cfg = fc.resolve_config()
        assert cfg["fast_model"] == ""
        assert fc.build_codex_command(cfg) == ["echo"]
    finally:
        _restore("FRONTIER_CODEX_FAST", old_fast)
        _restore("FRONTIER_CODEX_MODEL", old_model)
        _restore("FRONTIER_CODEX_FAST_MODEL", old_fast_model)


def test_cli_can_restore_fast_model_inheritance() -> None:
    sid = "fast-model-inheritance"
    try:
        configured = _run_dispatch(
            ["config", "--fast", "on", "--model", "pinned-fast-model"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert configured.returncode == 0, configured
        assert fc.read_state(sid)["config"]["fast_model"] == "pinned-fast-model"

        inherited = _run_dispatch(
            ["config", "--inherit-fast-model"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert inherited.returncode == 0, inherited
        assert fc.read_state(sid)["config"]["fast_model"] is None
    finally:
        fc.clear_state(sid)


def test_build_body_command_executor() -> None:
    old_cmd = _env("FRONTIER_CODEX_CMD", None)      # unset echo so codex builds a real command
    old_body = _env("FRONTIER_BODY_CMD", None)
    old_exec = _env("FRONTIER_EXECUTOR", None)
    old_claude = _env("FRONTIER_CLAUDE_CMD", None)
    old_grok = _env("FRONTIER_GROK_CMD", None)
    old_gemini = _env("FRONTIER_GEMINI_CMD", None)
    old_grok_yolo = _env("FRONTIER_GROK_YOLO", None)
    old_grok_permission = _env("FRONTIER_GROK_PERMISSION_MODE", None)
    old_grok_effort = _env("FRONTIER_GROK_EFFORT", None)
    try:
        codex_cfg = fc.resolve_config(overrides={"executor": "codex"})
        assert fc.build_body_command(codex_cfg)[0] == "codex"

        claude_cfg = fc.resolve_config(
            overrides={"executor": "claude", "claude_model": "claude-opus-4-8"}
        )
        assert fc.build_body_command(claude_cfg) == [
            "claude", "-p", "--model", "claude-opus-4-8"
        ]
        claude_final, claude_stdin, claude_cleanup = fc._prepare_prompt_command(
            fc.build_body_command(claude_cfg), "hello from claude"
        )
        assert claude_final == ["claude", "-p", "--model", "claude-opus-4-8"]
        assert claude_stdin == "hello from claude"
        assert claude_cleanup == []
        assert fc.build_frontier_command(claude_cfg) == ["echo"]

        grok_cfg = fc.resolve_config(overrides={"executor": "grok", "grok_model": "grok-4.5"})
        assert fc.build_body_command(grok_cfg) == [
            "grok", "--model", "grok-4.5", "--reasoning-effort", "high",
            "--prompt-file", "{prompt_file}",
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

        os.environ["FRONTIER_GROK_EFFORT"] = "medium"
        env_grok_cfg = fc.resolve_config(overrides={"executor": "grok", "grok_model": "grok-4.5"})
        assert fc.build_body_command(env_grok_cfg)[
            fc.build_body_command(env_grok_cfg).index("--reasoning-effort") + 1
        ] == "medium"
        os.environ.pop("FRONTIER_GROK_EFFORT", None)

        os.environ["FRONTIER_GROK_YOLO"] = "1"
        assert fc.build_body_command(grok_cfg)[
            fc.build_body_command(grok_cfg).index("--permission-mode") + 1
        ] == "bypassPermissions"
        os.environ["FRONTIER_GROK_PERMISSION_MODE"] = "auto"
        auto_cmd = fc.build_body_command(grok_cfg)
        assert auto_cmd[auto_cmd.index("--permission-mode") + 1] == "auto"
        os.environ.pop("FRONTIER_GROK_YOLO", None)
        os.environ.pop("FRONTIER_GROK_PERMISSION_MODE", None)

        gemini_cfg = fc.resolve_config(
            overrides={"executor": "gemini", "gemini_model": "gemini-3.5-flash"}
        )
        assert fc.build_body_command(gemini_cfg) == [
            "gemini", "--model", "gemini-3.5-flash", "--prompt", "",
            "--output-format", "text",
        ]
        gemini_final, gemini_stdin, gemini_cleanup = fc._prepare_prompt_command(
            fc.build_body_command(gemini_cfg), "hello from gemini"
        )
        assert gemini_final == [
            "gemini", "--model", "gemini-3.5-flash", "--prompt", "",
            "--output-format", "text",
        ]
        assert gemini_stdin == "hello from gemini"
        assert gemini_cleanup == []

        os.environ["FRONTIER_CLAUDE_CMD"] = "claude-runner --flag"
        assert fc.build_body_command(claude_cfg) == ["claude-runner", "--flag"]
        os.environ.pop("FRONTIER_CLAUDE_CMD", None)

        os.environ["FRONTIER_GROK_CMD"] = "grok-runner --flag"
        assert fc.build_body_command(grok_cfg) == ["grok-runner", "--flag"]
        os.environ.pop("FRONTIER_GROK_CMD", None)

        os.environ["FRONTIER_BODY_CMD"] = "my-runner --flag"      # universal override wins
        assert fc.build_body_command(codex_cfg) == ["my-runner", "--flag"]
        assert fc.build_body_command(claude_cfg) == ["my-runner", "--flag"]
        assert fc.build_body_command(grok_cfg) == ["my-runner", "--flag"]
        assert fc.build_body_command(gemini_cfg) == ["my-runner", "--flag"]
    finally:
        _restore("FRONTIER_CODEX_CMD", old_cmd)
        _restore("FRONTIER_BODY_CMD", old_body)
        _restore("FRONTIER_EXECUTOR", old_exec)
        _restore("FRONTIER_CLAUDE_CMD", old_claude)
        _restore("FRONTIER_GROK_CMD", old_grok)
        _restore("FRONTIER_GEMINI_CMD", old_gemini)
        _restore("FRONTIER_GROK_YOLO", old_grok_yolo)
        _restore("FRONTIER_GROK_PERMISSION_MODE", old_grok_permission)
        _restore("FRONTIER_GROK_EFFORT", old_grok_effort)
        os.environ["FRONTIER_CODEX_CMD"] = "echo"


def test_advisor_prompt_uses_selected_claude_model() -> None:
    cfg = fc.resolve_config(
        overrides={"executor": "claude", "claude_model": "claude-opus-4-8"}
    )
    prompt = frontier_advisor._build_advisor_prompt(
        "How should the lead route this refactor?",
        "",
        frontier_advisor._lead_description(cfg),
    )
    assert "Claude (claude-opus-4-8) is the EXECUTOR" in prompt
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
    old_fable = _env("FRONTIER_GUARDS_OFF", None)
    old_claude = _env("CLAUDE_GUARDS_OFF", None)
    try:
        os.environ.pop("FRONTIER_GUARDS_OFF", None)
        os.environ.pop("CLAUDE_GUARDS_OFF", None)
        assert fc.guards_off() is False

        os.environ["FRONTIER_GUARDS_OFF"] = "1"
        assert fc.guards_off() is True
        os.environ.pop("FRONTIER_GUARDS_OFF", None)

        os.environ["CLAUDE_GUARDS_OFF"] = "yes"
        assert fc.guards_off() is True
    finally:
        _restore("FRONTIER_GUARDS_OFF", old_fable)
        _restore("CLAUDE_GUARDS_OFF", old_claude)


def test_pretool_gate_contracts() -> None:
    gate = ROOT / "hooks" / "frontier_gate.py"
    _load_hook("hooks/frontier_gate.py")

    sid = "contract-pretool"
    fc.clear_state(sid)
    old_guards = _env("FRONTIER_GUARDS_OFF", None)
    try:
        fc.write_state(sid, armed=True)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"))
        assert _pretool_denied(proc), f"armed Write should deny; stdout={proc.stdout!r} stderr={proc.stderr!r}"

        fc.write_state(sid, armed=False)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"))
        assert _pretool_allowed(proc), f"unarmed Write should allow; stdout={proc.stdout!r} stderr={proc.stderr!r}"

        fc.write_state(sid, armed=True)
        proc = _run_hook(gate, _pretool_payload(sid, "Write"), extra_env={"FRONTIER_GUARDS_OFF": "1"})
        assert _pretool_allowed(proc), f"kill-switch should allow; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    finally:
        _restore("FRONTIER_GUARDS_OFF", old_guards)
        fc.clear_state(sid)


def test_pretool_gate_blocks_bash_chaining() -> None:
    """Regression: an allowlisted prefix must not let a chained command through
    (e.g. "git status -sb && rm -rf ..."). A prefix match alone is not enough."""
    gate = ROOT / "hooks" / "frontier_gate.py"
    _load_hook("hooks/frontier_gate.py")

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


def test_pretool_gate_allows_only_frozen_verification() -> None:
    """The armed controller can inspect, dispatch, and run the host-frozen gate, but cannot
    reconfigure the workflow, replace the verifier, or smuggle behavior through environment vars."""
    gate = ROOT / "hooks" / "frontier_gate.py"
    _load_hook("hooks/frontier_gate.py")

    sid = "contract-realistic-invocations"
    fc.clear_state(sid)
    try:
        approved = {"gate": "true", "argv": ["true"], "cwd": str(ROOT)}
        fc.write_state(sid, armed=True, approved_gate=approved)

        allowed_commands = [
            "python3 frontier_dispatch.py --help",
            "python3 frontier_dispatch.py doctor",
            "frontier-dispatch config",
            "frontier-dispatch verify",
            "wc -l frontier_dispatch.py hooks/frontier_gate.py frontier_common.py",
        ]
        for cmd in allowed_commands:
            proc = _run_hook(gate, _pretool_payload(sid, "Bash", {"command": cmd}))
            assert _pretool_allowed(proc), f"{cmd!r} must be allowed; stdout={proc.stdout!r}"

        still_denied = [
            "python3 frontier_dispatch.py arm --gate true",
            "frontier-dispatch disarm",
            "frontier-dispatch config --executor codex",
            "frontier-dispatch verify --gate true",
            "python3 frontier_verify.py --gate true",
            "FRONTIER_GUARDS_OFF=1 python3 frontier_dispatch.py --help",
            "FRONTIER_GATE_ALLOW_TRIVIAL=1 wc -l frontier_dispatch.py",
            "python3 frontier_dispatch.py --help && rm -rf /tmp/x",
            "FRONTIER_GUARDS_OFF=1 python3 frontier_dispatch.py --help; rm -rf /tmp/x",
        ]
        for cmd in still_denied:
            proc = _run_hook(gate, _pretool_payload(sid, "Bash", {"command": cmd}))
            assert _pretool_denied(proc), f"{cmd!r} must still be denied; stdout={proc.stdout!r}"
    finally:
        fc.clear_state(sid)


def test_session_id_defaults_to_claude_code_session_id() -> None:
    """Regression: without FRONTIER_SESSION_ID, frontier-dispatch must key state by
    CLAUDE_CODE_SESSION_ID (what the real PreToolUse/Stop hooks receive) — not the
    literal string "default", or the hard gate never engages in a real session."""
    fake_session = "contract-claude-code-session-id-12345"
    fc.clear_state(fake_session)
    fc.clear_state("default")
    old = _env("FRONTIER_SESSION_ID", None)
    try:
        proc = _run_dispatch(["arm"], extra_env={"CLAUDE_CODE_SESSION_ID": fake_session})
        assert proc.returncode == 0, f"arm failed: {proc.stdout!r} {proc.stderr!r}"
        assert fc.read_state(fake_session)["armed"] is True, (
            "state must be written under CLAUDE_CODE_SESSION_ID when FRONTIER_SESSION_ID is unset")
        assert fc.read_state("default")["armed"] is False, "must NOT fall through to 'default'"
    finally:
        _restore("FRONTIER_SESSION_ID", old)
        fc.clear_state(fake_session)
        fc.clear_state("default")


def test_dispatch_separates_profile_frontier_and_executor_models() -> None:
    """Profile, frontier model, and executor model are independent selections."""
    sid = "contract-separated-models"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            [
                "config", "--profile", "advisor",
                "--frontier-provider", "claude", "--frontier-model", "claude-fable-5",
                "--executor", "claude", "--model", "claude-opus-4-8",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"config failed: {proc.stdout!r} {proc.stderr!r}"
        cfg = json.loads(proc.stdout)
        assert cfg["profile"] == "advisor"
        assert cfg["frontier_provider"] == "claude"
        assert cfg["frontier_model"] == "claude-fable-5"
        assert cfg["executor"] == "claude"
        assert cfg["claude_model"] == "claude-opus-4-8"

        proc = _run_dispatch(
            ["--dry-run", "--executor", "claude", "--model", "claude-opus-4-8", "execute with Claude"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"claude dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "claude"
        assert payload["mode"]["claude_model"] == "claude-opus-4-8"
        assert "claude -p --model claude-opus-4-8" in payload["cards"][0]["summary"]
    finally:
        fc.clear_state(sid)


def test_codex_xhigh_dispatch_does_not_override_grok_effort() -> None:
    sid = "contract-codex-xhigh-dispatch"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            ["--dry-run", "--executor", "codex", "--effort", "xhigh", "deep Codex task"],
            extra_env={"FRONTIER_SESSION_ID": sid, "FRONTIER_CODEX_CMD": ""},
        )
        assert proc.returncode == 0, f"Codex xhigh dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "codex"
        assert payload["mode"]["codex_effort"] == "xhigh"
        assert payload["mode"]["grok_effort"] in fc.GROK_EFFORT_LEVELS
        assert "model_reasoning_effort=xhigh" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            [
                "--dry-run", "--executor", "codex", "--fast", "on", "--effort", "xhigh",
                "--model", "fast-codex-model", "fast Codex task",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid, "FRONTIER_CODEX_CMD": ""},
        )
        assert proc.returncode == 0, proc
        payload = json.loads(proc.stdout)
        assert payload["mode"]["fast"] is True
        assert payload["mode"]["fast_effort"] == "xhigh"
        assert payload["mode"]["fast_model"] == "fast-codex-model"
        assert "model_reasoning_effort=xhigh" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            [
                "config", "--executor", "codex", "--fast", "on", "--effort", "xhigh",
                "--model", "configured-fast-model",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, proc
        configured = json.loads(proc.stdout)
        assert configured["fast_effort"] == "xhigh"
        assert configured["fast_model"] == "configured-fast-model"

        proc = _run_dispatch(
            [
                "--dry-run", "--executor", "grok", "--fast", "on", "--effort", "high",
                "--model", "fast-grok-model", "fast Grok task",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid, "FRONTIER_GROK_CMD": ""},
        )
        assert proc.returncode == 0, proc
        payload = json.loads(proc.stdout)
        assert payload["mode"]["grok_model"] == "fast-grok-model"
        assert payload["mode"]["fast_model"] == "configured-fast-model"
        assert "grok --model fast-grok-model" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            ["--dry-run", "--executor", "grok", "--fast", "on", "--effort", "xhigh", "invalid Grok task"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 2
        assert "dispatch refused" in proc.stderr
        assert "Traceback" not in proc.stderr
    finally:
        fc.clear_state(sid)


def test_dispatch_config_accepts_grok_executor() -> None:
    """Grok Build can be the lead/body executor via the local grok CLI."""
    sid = "contract-grok-executor"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            ["config", "--executor", "grok", "--grok-model", "grok-4.5", "--effort", "medium"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"config grok failed: {proc.stdout!r} {proc.stderr!r}"
        cfg = json.loads(proc.stdout)
        assert cfg["executor"] == "grok"
        assert cfg["grok_model"] == "grok-4.5"
        assert cfg["grok_effort"] == "medium"
        assert cfg["frontier_model"] == "claude-fable-5"

        proc = _run_dispatch(
            ["--dry-run", "--executor", "grok", "--grok-model", "grok-4.5", "lead with Grok; ask Fable"],
            extra_env={"FRONTIER_SESSION_ID": sid},
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
            extra_env={"FRONTIER_SESSION_ID": sid, "FRONTIER_GROK_CMD": f"{sys.executable} -c pass"},
        )
        assert proc.returncode == 0, f"doctor grok override failed: {proc.stdout!r} {proc.stderr!r}"
        assert "grok body CLI" in proc.stdout
        assert Path(sys.executable).name in proc.stdout
        assert "argument(s) redacted" in proc.stdout
    finally:
        fc.clear_state(sid)


def test_manual_hook_install_is_atomic_owner_only_and_aligned() -> None:
    with tempfile.TemporaryDirectory(prefix="frontier-hooks-") as td:
        env = {"CLAUDE_CONFIG_DIR": td, "FRONTIER_SESSION_ID": "manual-hooks-contract"}
        proc = _run_dispatch(["install-hooks"], extra_env=env)
        assert proc.returncode == 0, proc.stderr
        settings = Path(td) / "settings.json"
        assert settings.is_file()
        assert settings.stat().st_mode & 0o777 == 0o600
        payload = json.loads(settings.read_text())
        stop = payload["hooks"]["Stop"]
        assert stop and stop[0]["matcher"] == "*"

        proc = _run_dispatch(["uninstall-hooks"], extra_env=env)
        assert proc.returncode == 0, proc.stderr
        cleaned = json.loads(settings.read_text())
        assert not cleaned.get("hooks", {}).get("PreToolUse")
        assert not cleaned.get("hooks", {}).get("Stop")


def test_cmd_done_refuses_without_fresh_green() -> None:
    """Regression: `done` must not disarm without a fresh GREEN verdict — otherwise the brain
    can always run the (Bash-allowlisted) `frontier-dispatch done` to kill the gate on demand."""
    sid = "contract-done-refuses"
    fc.clear_state(sid)
    with tempfile.TemporaryDirectory(prefix="frontier-done-") as td:
        try:
            subprocess.run(["git", "init", "-q"], cwd=td, check=True, timeout=30)
            approved = {"gate": "true", "argv": ["true"], "cwd": str(Path(td).resolve())}
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None,
                           approved_gate=approved)
            proc = _run_dispatch(["done"], extra_env={"FRONTIER_SESSION_ID": sid})
            assert proc.returncode != 0, f"done without a verdict must fail; stdout={proc.stdout!r}"
            assert fc.read_state(sid)["armed"] is True, "gate must stay armed without a fresh GREEN"

            stale = fc.make_verdict("pytest -q", 0, "sha", [], 90.0, 90.0)
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=stale,
                           approved_gate=approved)
            proc = _run_dispatch(["done"], extra_env={"FRONTIER_SESSION_ID": sid})
            assert proc.returncode != 0, f"done with a stale GREEN must still fail; stdout={proc.stdout!r}"
            assert fc.read_state(sid)["armed"] is True

            legacy_fresh = fc.make_verdict("true", 0, "sha", [], 110.0, 100.0)
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=legacy_fresh,
                           approved_gate=approved)
            proc = _run_dispatch(["done"], extra_env={"FRONTIER_SESSION_ID": sid})
            assert proc.returncode != 0, "legacy timestamp-only GREEN must not close the guardrail"
            assert fc.read_state(sid)["armed"] is True

            # A body can run frontier_verify directly outside the Claude hook surface. Its GREEN
            # must still not close a loop whose host froze a different gate.
            forged_approved = {"gate": "false", "argv": ["false"], "cwd": str(Path(td).resolve())}
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None,
                           approved_gate=forged_approved)
            forged = frontier_verify.run_gate("true", session_id=sid, cwd=td)
            assert forged["result"] == "GREEN"
            proc = _run_dispatch(["done"], extra_env={"FRONTIER_SESSION_ID": sid})
            assert proc.returncode != 0, "GREEN from a non-approved gate must not disarm"
            assert fc.read_state(sid)["armed"] is True

            # Start the next independent scenario without carrying over the forged receipt.
            # Direct state mutation above intentionally discarded its managed identity, so the
            # runtime must not infer ownership and delete the file itself.
            (Path(td) / "verdict.json").unlink()
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None,
                           approved_gate=approved)
            verdict = frontier_verify.run_gate("true", session_id=sid, cwd=td)
            assert verdict["result"] == "GREEN" and verdict["schema_version"] == 2
            proc = _run_dispatch(["done"], extra_env={"FRONTIER_SESSION_ID": sid})
            assert proc.returncode == 0, (
                f"done with snapshot-bound GREEN must succeed; stdout={proc.stdout!r} "
                f"stderr={proc.stderr!r}")
            assert fc.read_state(sid)["armed"] is False, "gate must disarm on a fresh GREEN"
        finally:
            fc.clear_state(sid)


def test_arm_freezes_verification_command() -> None:
    sid = "contract-frozen-gate"
    fc.clear_state(sid)
    with (
        tempfile.TemporaryDirectory(prefix="frontier-gate-") as td,
        tempfile.TemporaryDirectory(prefix="frontier-other-") as other,
    ):
        try:
            subprocess.run(["git", "init", "-q"], cwd=td, check=True, timeout=30)
            env = {"FRONTIER_SESSION_ID": sid}
            non_git_env = {"FRONTIER_SESSION_ID": f"{sid}-non-git"}
            proc = _run_dispatch(["arm", "--gate", "true", "--cwd", other], extra_env=non_git_env)
            assert proc.returncode == 2, "a closable arm must require a Git worktree"
            proc = _run_dispatch(["arm", "--gate", "true", "--cwd", td], extra_env=env)
            assert proc.returncode == 0, f"arm failed: {proc.stdout!r} {proc.stderr!r}"

            state = fc.read_state(sid)
            assert state["armed"] is True
            assert state["approved_gate"] == {
                "gate": "true",
                "argv": ["true"],
                "cwd": str(Path(td).resolve()),
            }

            proc = _run_dispatch(["verify", "--gate", "true"], extra_env=env)
            assert proc.returncode == 2, "armed verify must reject even an identical gate restatement"
            proc = _run_dispatch(["verify", "--gate", "false"], extra_env=env)
            assert proc.returncode == 2, "armed verify must reject replacement gate argv"
            proc = _run_dispatch(["verify", "--cwd", td], extra_env=env)
            assert proc.returncode == 2, "armed verify must reject even an identical cwd restatement"
            proc = _run_dispatch(["verify", "--cwd", other], extra_env=env)
            assert proc.returncode == 2, "armed verify must reject replacement workspace"

            proc = _run_dispatch(["verify"], extra_env=env)
            assert proc.returncode == 0, f"frozen gate failed: {proc.stdout!r} {proc.stderr!r}"
            assert fc.read_state(sid)["verdict"]["schema_version"] == 2

            proc = _run_dispatch(["arm"], extra_env=env)
            assert proc.returncode == 0
            proc = _run_dispatch(["verify"], extra_env=env)
            assert proc.returncode == 2, "armed verify without a host-approved gate must fail closed"
        finally:
            fc.clear_state(sid)


def test_stop_gate_contracts() -> None:
    stop = ROOT / "hooks" / "frontier_verify_gate.py"
    _load_hook("hooks/frontier_verify_gate.py")

    sid = "contract-stop"
    fc.clear_state(sid)
    old_guards = _env("FRONTIER_GUARDS_OFF", None)
    with tempfile.TemporaryDirectory(prefix="frontier-stop-") as td:
        try:
            subprocess.run(["git", "init", "-q"], cwd=td, check=True, timeout=30)
            approved = {"gate": "true", "argv": ["true"], "cwd": str(Path(td).resolve())}
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=None,
                           approved_gate=approved)
            proc = _run_hook(stop, _stop_payload(sid))
            assert proc.returncode == 2, f"no fresh GREEN should block Stop; rc={proc.returncode}"

            stale = fc.make_verdict("pytest -q", 0, "sha", [], 90.0, 90.0)
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=stale,
                           approved_gate=approved)
            proc = _run_hook(stop, _stop_payload(sid))
            assert proc.returncode == 2, f"stale GREEN should block Stop; rc={proc.returncode}"

            legacy_fresh = fc.make_verdict("true", 0, "sha", [], 110.0, 100.0)
            fc.write_state(sid, armed=True, last_dispatch_ts=100.0, verdict=legacy_fresh,
                           approved_gate=approved)
            proc = _run_hook(stop, _stop_payload(sid))
            assert proc.returncode == 2, "legacy timestamp-only GREEN must not allow Stop"

            verdict = frontier_verify.run_gate("true", session_id=sid, cwd=td)
            assert verdict["result"] == "GREEN" and verdict["schema_version"] == 2
            proc = _run_hook(stop, _stop_payload(sid))
            assert proc.returncode == 0, (
                f"snapshot-bound GREEN should allow Stop; rc={proc.returncode} "
                f"stderr={proc.stderr!r}")
            retained = fc.read_state(sid)
            assert retained["armed"] is True
            assert retained["completion_pending"] is True
            assert retained["completion_closed"] is False
            assert retained["verdict"]["result"] == "GREEN"
        finally:
            _restore("FRONTIER_GUARDS_OFF", old_guards)
            fc.clear_state(sid)


def main() -> int:
    tests = [
        test_resolve_config_precedence,
        test_build_codex_command_fast_swaps_effort,
        test_empty_fast_model_environment_selects_account_default,
        test_cli_can_restore_fast_model_inheritance,
        test_build_body_command_executor,
        test_advisor_prompt_uses_selected_claude_model,
        test_make_verdict_and_fresh_green,
        test_state_read_write_merge_clear,
        test_handoff_card_bounded_with_artifact,
        test_run_engine_cleans_prompt_file,
        test_guards_off_honors_kill_switches,
        test_pretool_gate_contracts,
        test_pretool_gate_blocks_bash_chaining,
        test_pretool_gate_allows_only_frozen_verification,
        test_session_id_defaults_to_claude_code_session_id,
        test_dispatch_separates_profile_frontier_and_executor_models,
        test_codex_xhigh_dispatch_does_not_override_grok_effort,
        test_dispatch_config_accepts_grok_executor,
        test_manual_hook_install_is_atomic_owner_only_and_aligned,
        test_arm_freezes_verification_command,
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
        print(f"frontier_contracts: FAIL ({failed})", file=sys.stderr)
        return 1
    print("frontier_contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
