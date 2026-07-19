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
import frontier_dispatch  # noqa: E402
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


def test_effective_frontier_model_is_not_hard_wired_to_fable() -> None:
    """Managed frontier defaults follow the selected provider; Codex stays unpinned."""
    old_adv = _env("FRONTIER_ADVISOR_CMD", None)
    try:
        assert fc.effective_frontier_model({"frontier_provider": "claude", "frontier_model": ""}) == (
            "claude-fable-5"
        )
        assert fc.effective_frontier_model({"frontier_provider": "codex", "frontier_model": ""}) == (
            "account default"
        )
        assert fc.effective_frontier_model({"frontier_provider": "grok", "frontier_model": ""}) == (
            "grok-4.5"
        )
        assert fc.effective_frontier_model({"frontier_provider": "gemini", "frontier_model": ""}) == (
            "gemini-3.5-flash"
        )
        assert fc.effective_frontier_model(
            {"frontier_provider": "codex", "frontier_model": "gpt-5.6-sol"}
        ) == "gpt-5.6-sol"

        assert fc.build_frontier_command(
            {"frontier_provider": "claude", "frontier_model": ""}
        ) == ["claude", "-p", "--model", "claude-fable-5"]
        assert fc.build_frontier_command(
            {"frontier_provider": "codex", "frontier_model": ""}
        ) == ["codex", "exec", "-"]
        assert fc.build_frontier_command(
            {"frontier_provider": "codex", "frontier_model": "gpt-5.6-terra"}
        ) == ["codex", "exec", "--model", "gpt-5.6-terra", "-"]
        assert fc.build_frontier_command(
            {"frontier_provider": "grok", "frontier_model": ""}
        ) == ["grok", "--model", "grok-4.5", "--prompt-file", "{prompt_file}"]
        assert fc.build_frontier_command(
            {"frontier_provider": "gemini", "frontier_model": ""}
        ) == ["gemini", "--model", "gemini-3.5-flash", "--prompt", ""]
    finally:
        _restore("FRONTIER_ADVISOR_CMD", old_adv)
        os.environ["FRONTIER_ADVISOR_CMD"] = "echo"


def test_ask_frontier_reports_effective_frontier_model() -> None:
    """ask_frontier.model follows effective_frontier_model, not a hard-wired Fable ID."""
    sid = "contract-ask-frontier-effective-model"
    fc.clear_state(sid)
    old_adv = _env("FRONTIER_ADVISOR_CMD", "echo")
    try:
        fc.write_state(
            sid,
            config={
                "frontier_provider": "codex",
                "frontier_model": "",
            },
        )
        result = frontier_advisor.ask_frontier("ping", session_id=sid, timeout=10)
        assert result["ok"] is True
        assert result["model"] == "account default"

        fc.write_state(
            sid,
            config={
                "frontier_provider": "gemini",
                "frontier_model": "gemini-2.5-pro",
            },
        )
        result = frontier_advisor.ask_frontier("ping", session_id=sid, timeout=10)
        assert result["ok"] is True
        assert result["model"] == "gemini-2.5-pro"
    finally:
        _restore("FRONTIER_ADVISOR_CMD", old_adv)
        os.environ["FRONTIER_ADVISOR_CMD"] = "echo"
        fc.clear_state(sid)


def test_config_refuses_generic_and_provider_model_flag_conflict() -> None:
    """--executor-model/--model must not mix with the selected executor's provider model flag."""
    sid = "contract-provider-model-flag-conflict"
    fc.clear_state(sid)
    try:
        proc = _run_dispatch(
            [
                "config",
                "--executor", "claude",
                "--executor-model", "claude-sonnet-5",
                "--claude-model", "claude-opus-4-8",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 2
        assert "either" in (proc.stderr or "").lower()
        assert "--claude-model" in (proc.stderr or "")

        # Empty provider pin is allowed and distinct from a missing flag.
        proc = _run_dispatch(
            ["config", "--executor", "claude", "--claude-model", ""],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, proc.stderr
        cfg = json.loads(proc.stdout)
        assert cfg["claude_model"] == ""
    finally:
        fc.clear_state(sid)


def test_dispatch_override_preserves_empty_frontier_model_pin() -> None:
    """An explicit empty --frontier-model clears a session pin during dispatch."""
    args = frontier_dispatch._build_parser().parse_args(["--frontier-model", "", "task"])
    assert frontier_dispatch._overrides(args)["frontier_model"] == ""


def test_availability_suggestion_is_path_only_and_non_mutating() -> None:
    """Doctor suggestions are deterministic PATH hints, never auth probes or config writes."""
    cfg = fc.defaults()
    before = dict(cfg)

    def grok_only(name: str) -> str | None:
        return "/mock/grok" if name == "grok" else None

    suggestion = frontier_dispatch.suggest_provider_availability(cfg, lookup=grok_only)
    assert suggestion is not None
    assert suggestion["executor"] == "grok"
    assert suggestion["executor_model"] == "grok-4.5"
    assert suggestion["frontier_provider"] == "grok"
    assert suggestion["frontier_model"] == "grok-4.5"
    assert suggestion["present_provider_clis"] == ["grok"]
    assert suggestion["missing_configured_clis"] == ["claude", "codex"]
    assert cfg == before

    def configured_present(name: str) -> str | None:
        return "/mock/" + name if name in {"codex", "claude"} else None

    assert frontier_dispatch.suggest_provider_availability(
        cfg, lookup=configured_present
    ) is None
    assert "not authentication or model entitlement" in frontier_dispatch.AVAILABILITY_NOTE


def test_profile_help_describes_host_led_orchestration() -> None:
    help_text = " ".join(frontier_dispatch._build_parser().format_help().split())
    assert "host-led verified orchestration" in help_text
    assert "never makes it the host lead" in help_text


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
            [
                "config", "--executor", "codex", "--executor-model", "",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"config with executor-model failed: {proc.stdout!r} {proc.stderr!r}"
        cfg = json.loads(proc.stdout)
        assert cfg["executor"] == "codex"
        assert cfg["codex_model"] == ""

        proc = _run_dispatch(
            ["--dry-run", "--executor", "claude", "--model", "claude-opus-4-8", "execute with Claude"],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"claude dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "claude"
        assert payload["mode"]["claude_model"] == "claude-opus-4-8"
        assert "claude -p --model claude-opus-4-8" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            [
                "--dry-run",
                "--executor", "claude",
                "--executor-model", "claude-sonnet-5",
                "execute with explicit executor model flag",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode == 0, f"executor-model dry-run failed: {proc.stdout!r} {proc.stderr!r}"
        payload = json.loads(proc.stdout)
        assert payload["mode"]["executor"] == "claude"
        assert payload["mode"]["claude_model"] == "claude-sonnet-5"
        assert "claude -p --model claude-sonnet-5" in payload["cards"][0]["summary"]

        proc = _run_dispatch(
            [
                "--dry-run",
                "--executor", "codex",
                "--model", "codex-model-legacy",
                "--executor-model", "codex-model-dual",
                "reject dual model args",
            ],
            extra_env={"FRONTIER_SESSION_ID": sid},
        )
        assert proc.returncode != 0
        assert "legacy" in (proc.stderr or "").lower() or "either" in (proc.stderr or "").lower()
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


def test_dispatch_task_count_hard_cap() -> None:
    """One dispatch cannot schedule unbounded provider tasks; overflow refuses without markers."""
    default_limit = frontier_dispatch.DEFAULT_MAX_TASKS_PER_DISPATCH
    ceiling = frontier_dispatch.MAX_TASKS_HARD_CEILING
    assert default_limit == 32
    assert ceiling == 64
    assert 1 <= default_limit <= ceiling

    help_text = " ".join(frontier_dispatch._build_parser().format_help().split())
    assert str(default_limit) in help_text
    assert "hard" in help_text.lower() or "cap" in help_text.lower()
    assert "informational" in help_text.lower() or "not enforced" in help_text.lower()

    # Unit: default and env clamp (no config schema).
    old_max = _env("FRONTIER_MAX_TASKS", None)
    try:
        assert frontier_dispatch.max_tasks_per_dispatch() == default_limit
        _env("FRONTIER_MAX_TASKS", "8")
        assert frontier_dispatch.max_tasks_per_dispatch() == 8
        _env("FRONTIER_MAX_TASKS", str(ceiling))
        assert frontier_dispatch.max_tasks_per_dispatch() == ceiling
        for bad in ("0", str(ceiling + 1), "nope", "-3"):
            _env("FRONTIER_MAX_TASKS", bad)
            try:
                frontier_dispatch.max_tasks_per_dispatch()
                raise AssertionError(f"expected ValueError for FRONTIER_MAX_TASKS={bad!r}")
            except ValueError as exc:
                assert "FRONTIER_MAX_TASKS" in str(exc)
    finally:
        _restore("FRONTIER_MAX_TASKS", old_max)

    sid = "contract-task-cap"
    fc.clear_state(sid)
    # Clear any inherited FRONTIER_MAX_TASKS for subprocess boundary tests.
    base_env = {"FRONTIER_SESSION_ID": sid}
    old_env_max = os.environ.pop("FRONTIER_MAX_TASKS", None)

    try:
        # Boundary: exactly default_limit positional tasks succeeds (dry-run; no provider).
        ok_tasks = [f"task-{i}" for i in range(default_limit)]
        proc = _run_dispatch(["--dry-run", *ok_tasks], extra_env=base_env)
        assert proc.returncode == 0, f"boundary dry-run failed: {proc.stderr!r} {proc.stdout!r}"
        payload = json.loads(proc.stdout)
        assert payload["count"] == default_limit

        # Overflow: default_limit + 1 refuses before mutation (non-dry-run).
        before = fc.read_state(sid)
        before_gen = int(before.get("dispatch_generation") or 0)
        before_active = list(before.get("active_dispatches") or [])
        runs_before = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
        over = [f"overflow-{i}" for i in range(default_limit + 1)]
        proc = _run_dispatch(over, extra_env=base_env)
        assert proc.returncode == 2, f"overflow should refuse; rc={proc.returncode} err={proc.stderr!r}"
        assert "dispatch refused" in proc.stderr
        assert "hard limit" in proc.stderr
        assert str(default_limit) in proc.stderr
        assert "dollar" in proc.stderr.lower() or "budget" in proc.stderr.lower()
        after = fc.read_state(sid)
        assert list(after.get("active_dispatches") or []) == before_active
        assert int(after.get("dispatch_generation") or 0) == before_gen
        runs_after = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
        assert runs_after == runs_before, "overflow must not create a dispatch run directory"

        # Fanout-file overflow (same cap as positional).
        with tempfile.TemporaryDirectory(prefix="frontier-fanout-") as td:
            fanout_path = Path(td) / "tasks.json"
            fanout_path.write_text(json.dumps([f"fan-{i}" for i in range(default_limit + 1)]), encoding="utf-8")
            proc = _run_dispatch(["--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr
            assert "hard limit" in proc.stderr
            after_fan = fc.read_state(sid)
            assert list(after_fan.get("active_dispatches") or []) == before_active
            assert int(after_fan.get("dispatch_generation") or 0) == before_gen

            # Combined positional + fanout count against the same limit.
            fanout_path.write_text(json.dumps([f"c-fan-{i}" for i in range(default_limit // 2 + 1)]),
                                   encoding="utf-8")
            pos = [f"c-pos-{i}" for i in range(default_limit // 2)]
            proc = _run_dispatch([*pos, "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 2
            assert "hard limit" in proc.stderr

            # Boundary via fanout exactly at limit.
            fanout_path.write_text(json.dumps([f"ok-fan-{i}" for i in range(default_limit)]),
                                   encoding="utf-8")
            proc = _run_dispatch(["--dry-run", "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 0, f"fanout boundary failed: {proc.stderr!r}"
            assert json.loads(proc.stdout)["count"] == default_limit

            # Malformed fanout: not a list.
            fanout_path.write_text(json.dumps({"task": "x"}), encoding="utf-8")
            proc = _run_dispatch(["--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr
            assert "JSON list" in proc.stderr or "list" in proc.stderr

            # Malformed fanout: invalid JSON.
            fanout_path.write_text("{not-json", encoding="utf-8")
            proc = _run_dispatch(["--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr

            # Malformed fanout: bad item type.
            fanout_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            proc = _run_dispatch(["--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr

            # Duplicate task strings are allowed (not a uniqueness constraint).
            fanout_path.write_text(json.dumps(["same", "same", "same"]), encoding="utf-8")
            proc = _run_dispatch(["--dry-run", "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 0, f"duplicate fanout should be allowed: {proc.stderr!r}"
            assert json.loads(proc.stdout)["count"] == 3

            # Object form + empty/whitespace filtered before counting.
            # Empty/whitespace remain intentionally filtered (present non-null task field).
            fanout_path.write_text(
                json.dumps(["keep", {"task": "also"}, "", "  ", {"task": ""}, {"task": "  "}]),
                encoding="utf-8",
            )
            proc = _run_dispatch(["--dry-run", "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 0
            assert json.loads(proc.stdout)["count"] == 2

            # Mixed fanout: missing task key with a valid item → refuse whole batch.
            # Non-dry-run + provider sentinel: no config/state/run-dir/provider side effects.
            sentinel = Path(td) / "provider_sentinel"
            provider_script = Path(td) / "provider_touch.py"
            provider_script.write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('called', encoding='utf-8')\n",
                encoding="utf-8",
            )
            provider_cmd = f"{sys.executable} {provider_script}"
            if sentinel.exists():
                sentinel.unlink()
            mixed_missing = [
                {"task": "valid-keep"},
                {"note": "no task key"},
            ]
            fanout_path.write_text(json.dumps(mixed_missing), encoding="utf-8")
            state_before = fc.read_state(sid)
            before_gen = int(state_before.get("dispatch_generation") or 0)
            before_active = list(state_before.get("active_dispatches") or [])
            runs_before = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            proc = _run_dispatch(
                ["--fanout", str(fanout_path)],
                extra_env={**base_env, "FRONTIER_BODY_CMD": provider_cmd},
            )
            assert proc.returncode == 2, (
                f"missing task in mixed fanout must refuse; rc={proc.returncode} "
                f"err={proc.stderr!r} out={proc.stdout!r}"
            )
            assert "dispatch refused" in proc.stderr
            assert "non-null" in proc.stderr.lower() or "task" in proc.stderr.lower()
            assert not sentinel.exists(), "provider sentinel must not run on malformed fanout"
            state_after = fc.read_state(sid)
            assert list(state_after.get("active_dispatches") or []) == before_active
            assert int(state_after.get("dispatch_generation") or 0) == before_gen
            runs_after = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            assert runs_after == runs_before, "malformed fanout must not create a run directory"

            # Mixed fanout: null task with a valid item → same fail-closed refuse.
            if sentinel.exists():
                sentinel.unlink()
            mixed_null = [
                "string-valid",
                {"task": None},
                {"task": "would-have-run"},
            ]
            fanout_path.write_text(json.dumps(mixed_null), encoding="utf-8")
            state_before = fc.read_state(sid)
            before_gen = int(state_before.get("dispatch_generation") or 0)
            before_active = list(state_before.get("active_dispatches") or [])
            runs_before = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            proc = _run_dispatch(
                ["--fanout", str(fanout_path)],
                extra_env={**base_env, "FRONTIER_BODY_CMD": provider_cmd},
            )
            assert proc.returncode == 2, (
                f"null task in mixed fanout must refuse; rc={proc.returncode} "
                f"err={proc.stderr!r} out={proc.stdout!r}"
            )
            assert "dispatch refused" in proc.stderr
            assert "non-null" in proc.stderr.lower() or "task" in proc.stderr.lower()
            assert not sentinel.exists(), "provider sentinel must not run on null-task fanout"
            state_after = fc.read_state(sid)
            assert list(state_after.get("active_dispatches") or []) == before_active
            assert int(state_after.get("dispatch_generation") or 0) == before_gen
            runs_after = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            assert runs_after == runs_before, "null-task fanout must not create a run directory"

            # Unit: _collect_dispatch_tasks raises before any caller can dispatch partial lists.
            from types import SimpleNamespace
            for bad_payload in (
                [{"task": "ok"}, {}],
                [{"task": "ok"}, {"task": None}],
            ):
                fanout_path.write_text(json.dumps(bad_payload), encoding="utf-8")
                try:
                    frontier_dispatch._collect_dispatch_tasks(
                        SimpleNamespace(tasks=[], fanout=str(fanout_path))
                    )
                    raise AssertionError(f"expected ValueError for {bad_payload!r}")
                except ValueError as exc:
                    msg = str(exc).lower()
                    assert "task" in msg
                    assert "non-null" in msg or "must include" in msg

        # Explicit lower per-invocation limit via FRONTIER_MAX_TASKS.
        tight = {**base_env, "FRONTIER_MAX_TASKS": "2"}
        proc = _run_dispatch(["--dry-run", "a", "b"], extra_env=tight)
        assert proc.returncode == 0
        proc = _run_dispatch(["a", "b", "c"], extra_env=tight)
        assert proc.returncode == 2
        assert "hard limit of 2" in proc.stderr
        after_tight = fc.read_state(sid)
        assert list(after_tight.get("active_dispatches") or []) == []
        assert int(after_tight.get("dispatch_generation") or 0) == before_gen

        # Invalid FRONTIER_MAX_TASKS refuses without markers.
        proc = _run_dispatch(["--dry-run", "only"], extra_env={**base_env, "FRONTIER_MAX_TASKS": "999"})
        assert proc.returncode == 2
        assert "dispatch refused" in proc.stderr
        assert "FRONTIER_MAX_TASKS" in proc.stderr
    finally:
        if old_env_max is None:
            os.environ.pop("FRONTIER_MAX_TASKS", None)
        else:
            os.environ["FRONTIER_MAX_TASKS"] = old_env_max
        fc.clear_state(sid)



def test_dispatch_fanout_bounded_reader() -> None:
    """Fanout path is fail-closed: non-regular/oversized/bad UTF-8 refuse without hanging or side effects."""
    import os
    import threading
    from types import SimpleNamespace

    max_b = frontier_dispatch.MAX_FANOUT_FILE_BYTES
    assert isinstance(max_b, int) and max_b > 0
    assert max_b <= 4 * 1024 * 1024  # conservative named bound

    sid = "contract-fanout-bounded"
    fc.clear_state(sid)
    base_env = {"FRONTIER_SESSION_ID": sid}
    old_env_max = os.environ.pop("FRONTIER_MAX_TASKS", None)

    def _assert_value_error_quick(path: Path, *, label: str, timeout_s: float = 2.0) -> None:
        """Call the unit reader under an outer timeout so tests never hang on FIFO/special."""
        box: dict = {}

        def target() -> None:
            try:
                frontier_dispatch._read_fanout_file_text(path)
                box["err"] = None
            except Exception as exc:  # noqa: BLE001 — capture for parent thread
                box["err"] = exc

        thr = threading.Thread(target=target, daemon=True)
        thr.start()
        thr.join(timeout=timeout_s)
        assert not thr.is_alive(), f"{label}: reader hung past {timeout_s}s on {path}"
        assert isinstance(box.get("err"), ValueError), (
            f"{label}: expected ValueError, got {box.get('err')!r}"
        )

    try:
        with tempfile.TemporaryDirectory(prefix="frontier-fanout-bound-") as td:
            td_path = Path(td)
            fanout_path = td_path / "tasks.json"

            # --- happy path still works ---
            fanout_path.write_text(json.dumps(["ok-a", "ok-b"]), encoding="utf-8")
            text = frontier_dispatch._read_fanout_file_text(fanout_path)
            assert json.loads(text) == ["ok-a", "ok-b"]
            proc = _run_dispatch(["--dry-run", "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout)["count"] == 2

            # --- FIFO: nonblocking open + fstat refuse; must not hang ---
            fifo_path = td_path / "fanout.fifo"
            os.mkfifo(fifo_path)
            _assert_value_error_quick(fifo_path, label="FIFO unit")
            # Subprocess path with outer timeout (tests themselves never hang).
            proc = _run_dispatch(["--fanout", str(fifo_path)], extra_env=base_env)
            assert proc.returncode == 2, f"FIFO must refuse; rc={proc.returncode} err={proc.stderr!r}"
            assert "dispatch refused" in proc.stderr

            # --- symlink → special (/dev/zero): refuse without unbounded read ---
            zero = Path("/dev/zero")
            if zero.exists():
                link_zero = td_path / "to-zero"
                link_zero.symlink_to(zero)
                _assert_value_error_quick(link_zero, label="symlink-/dev/zero unit")
                proc = _run_dispatch(["--fanout", str(link_zero)], extra_env=base_env)
                assert proc.returncode == 2
                assert "dispatch refused" in proc.stderr
            else:
                # Fallback special target when /dev/zero is absent.
                null = Path("/dev/null")
                assert null.exists(), "need /dev/zero or /dev/null for special-file test"
                link_null = td_path / "to-null"
                link_null.symlink_to(null)
                _assert_value_error_quick(link_null, label="symlink-/dev/null unit")
                proc = _run_dispatch(["--fanout", str(link_null)], extra_env=base_env)
                assert proc.returncode == 2
                assert "dispatch refused" in proc.stderr

            # --- symlink → regular file may still work ---
            real = td_path / "real.json"
            real.write_text(json.dumps(["via-link"]), encoding="utf-8")
            link_reg = td_path / "to-real.json"
            link_reg.symlink_to(real)
            text = frontier_dispatch._read_fanout_file_text(link_reg)
            assert json.loads(text) == ["via-link"]
            proc = _run_dispatch(["--dry-run", "--fanout", str(link_reg)], extra_env=base_env)
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout)["count"] == 1

            # --- oversized regular file (st_size and/or limit+1 growth path) ---
            over = td_path / "over.json"
            # Write max+1 bytes of 'x' so JSON is invalid but size check fails first.
            over.write_bytes(b"x" * (max_b + 1))
            _assert_value_error_quick(over, label="oversized unit")
            proc = _run_dispatch(["--fanout", str(over)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr

            # --- invalid UTF-8 ---
            bad_utf = td_path / "bad-utf.json"
            bad_utf.write_bytes(b'["ok", \xff\xfe]')
            _assert_value_error_quick(bad_utf, label="invalid-utf8 unit")
            try:
                frontier_dispatch._collect_dispatch_tasks(
                    SimpleNamespace(tasks=[], fanout=str(bad_utf))
                )
                raise AssertionError("expected ValueError for invalid UTF-8")
            except ValueError as exc:
                assert "utf-8" in str(exc).lower() or "utf8" in str(exc).lower() or "cannot" in str(exc).lower()
            proc = _run_dispatch(["--fanout", str(bad_utf)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr

            # --- unreadable input (missing path) ---
            missing = td_path / "no-such-fanout.json"
            _assert_value_error_quick(missing, label="missing unit")
            proc = _run_dispatch(["--fanout", str(missing)], extra_env=base_env)
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr

            # Unreadable via mode bits when possible (skip if root / no effect).
            locked = td_path / "locked.json"
            locked.write_text(json.dumps(["secret"]), encoding="utf-8")
            locked.chmod(0o000)
            try:
                try:
                    with open(locked, "rb"):
                        can_read = True
                except OSError:
                    can_read = False
                if not can_read:
                    _assert_value_error_quick(locked, label="mode-000 unit")
                    proc = _run_dispatch(["--fanout", str(locked)], extra_env=base_env)
                    assert proc.returncode == 2
                    assert "dispatch refused" in proc.stderr
            finally:
                locked.chmod(0o600)

            # --- no-side-effect / provider sentinel on bounded-reader failure ---
            sentinel = td_path / "provider_sentinel"
            provider_script = td_path / "provider_touch.py"
            provider_script.write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('called', encoding='utf-8')\n",
                encoding="utf-8",
            )
            provider_cmd = f"{sys.executable} {provider_script}"
            if sentinel.exists():
                sentinel.unlink()
            # Use FIFO so failure is definitely in the bounded reader, not JSON parse.
            bad_fifo = td_path / "side-effect.fifo"
            os.mkfifo(bad_fifo)
            state_before = fc.read_state(sid)
            before_gen = int(state_before.get("dispatch_generation") or 0)
            before_active = list(state_before.get("active_dispatches") or [])
            runs_before = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            proc = _run_dispatch(
                ["--fanout", str(bad_fifo)],
                extra_env={**base_env, "FRONTIER_BODY_CMD": provider_cmd},
            )
            assert proc.returncode == 2
            assert "dispatch refused" in proc.stderr
            assert not sentinel.exists(), "provider sentinel must not run on non-regular fanout"
            state_after = fc.read_state(sid)
            assert list(state_after.get("active_dispatches") or []) == before_active
            assert int(state_after.get("dispatch_generation") or 0) == before_gen
            runs_after = set(fc.RUNS_DIR.glob("frontier-*")) if fc.RUNS_DIR.is_dir() else set()
            assert runs_after == runs_before, "non-regular fanout must not create a run directory"

            # Preserve empty-filtering + mixed missing/null fail-closed semantics.
            fanout_path.write_text(
                json.dumps(["keep", "", "  ", {"task": "also"}, {"task": ""}]),
                encoding="utf-8",
            )
            proc = _run_dispatch(["--dry-run", "--fanout", str(fanout_path)], extra_env=base_env)
            assert proc.returncode == 0
            assert json.loads(proc.stdout)["count"] == 2

            fanout_path.write_text(json.dumps([{"task": "ok"}, {"task": None}]), encoding="utf-8")
            if sentinel.exists():
                sentinel.unlink()
            proc = _run_dispatch(
                ["--fanout", str(fanout_path)],
                extra_env={**base_env, "FRONTIER_BODY_CMD": provider_cmd},
            )
            assert proc.returncode == 2
            assert not sentinel.exists()
    finally:
        if old_env_max is None:
            os.environ.pop("FRONTIER_MAX_TASKS", None)
        else:
            os.environ["FRONTIER_MAX_TASKS"] = old_env_max
        fc.clear_state(sid)



def main() -> int:
    tests = [
        test_resolve_config_precedence,
        test_build_codex_command_fast_swaps_effort,
        test_empty_fast_model_environment_selects_account_default,
        test_cli_can_restore_fast_model_inheritance,
        test_build_body_command_executor,
        test_advisor_prompt_uses_selected_claude_model,
        test_effective_frontier_model_is_not_hard_wired_to_fable,
        test_ask_frontier_reports_effective_frontier_model,
        test_config_refuses_generic_and_provider_model_flag_conflict,
        test_dispatch_override_preserves_empty_frontier_model_pin,
        test_availability_suggestion_is_path_only_and_non_mutating,
        test_profile_help_describes_host_led_orchestration,
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
        test_dispatch_task_count_hard_cap,
        test_dispatch_fanout_bounded_reader,
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
