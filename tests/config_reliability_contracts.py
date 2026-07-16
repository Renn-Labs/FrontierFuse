#!/usr/bin/env python3
"""Offline contracts for configuration recovery, locking, schemas, and doctor states."""
from __future__ import annotations

import json
import io
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from multiprocessing import Event, Process, Queue, get_context
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="frontier-config-contracts-"))
os.environ["FRONTIER_CONFIG_DIR"] = str(TMP / "config")
os.environ["FRONTIER_STATE_DIR"] = str(TMP / "config" / "state")
os.environ["FRONTIER_RUNS_DIR"] = str(TMP / "runs")
sys.path.insert(0, str(ROOT))

import frontier_common as fc  # noqa: E402
import frontier_dispatch as dispatch  # noqa: E402
import frontier_verify as verify  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _try_lock_nonblocking(lock_path: Path, blocked: Event, acquired: Event) -> None:
    import fcntl

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            blocked.set()
        else:
            acquired.set()
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_corrupt_config_fails_closed_without_overwrite() -> None:
    fc.mkdir_owner_only(fc.GLOBAL_CONFIG.parent)
    original = "{truncated private config"
    fc.GLOBAL_CONFIG.write_text(original)
    fc.GLOBAL_CONFIG.chmod(0o600)

    for operation in (
        lambda: fc.resolve_config(),
        lambda: fc.save_global_config({"codex_effort": "low"}),
    ):
        try:
            operation()
        except fc.ConfigFileError as exc:
            assert "repair" in str(exc).lower()
        else:
            raise AssertionError("corrupt config must fail closed")
        assert fc.GLOBAL_CONFIG.read_text() == original


def test_explicit_repair_backs_up_and_restores_config() -> None:
    original = fc.GLOBAL_CONFIG.read_bytes()
    result = fc.repair_config_file(fc.GLOBAL_CONFIG, kind="global")
    backup = Path(result["backup"])
    assert result["status"] == "repaired"
    assert backup.read_bytes() == original
    assert _mode(backup) == 0o600
    payload = json.loads(fc.GLOBAL_CONFIG.read_text())
    assert payload["schema_version"] == fc.CONFIG_SCHEMA_VERSION
    assert fc.resolve_config()["profile"] == "advisor"

    fc.write_json_owner_only(
        fc.GLOBAL_CONFIG,
        {"schema_version": fc.CONFIG_SCHEMA_VERSION, "codex_effort": "extreme"},
    )
    semantic = fc.repair_config_file(fc.GLOBAL_CONFIG, kind="global")
    assert semantic["status"] == "repaired"
    assert json.loads(Path(semantic["backup"]).read_text())["codex_effort"] == "extreme"
    assert fc.resolve_config()["codex_effort"] == "high"


def test_repair_keeps_authoritative_path_present_until_atomic_replace() -> None:
    original = b"{broken but authoritative"
    fc.write_bytes_owner_only(fc.GLOBAL_CONFIG, original)
    original_writer = fc.write_config_owner_only

    def assert_source_present(path, payload):
        assert fc.GLOBAL_CONFIG.read_bytes() == original
        return original_writer(path, payload)

    try:
        fc.write_config_owner_only = assert_source_present
        repaired = fc.repair_config_file(fc.GLOBAL_CONFIG, kind="global")
        assert repaired["status"] == "repaired"
        assert Path(repaired["backup"]).read_bytes() == original
    finally:
        fc.write_config_owner_only = original_writer


def test_corrupt_session_state_fails_closed_and_repairs() -> None:
    sid = "corrupt-state"
    path = fc.state_path(sid)
    original = b'{"armed": true, "config": '
    fc.write_bytes_owner_only(path, original)
    try:
        fc.read_state(sid)
    except fc.StateFileError as exc:
        assert "--repair" in str(exc)
    else:
        raise AssertionError("corrupt session state must fail closed")
    try:
        fc.write_state(sid, armed=False)
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("state update must not overwrite corrupt state")
    assert path.read_bytes() == original

    repaired = fc.repair_config_file(path, kind="state", session_id=sid)
    assert repaired["status"] == "repaired"
    assert Path(repaired["backup"]).read_bytes() == original
    assert _mode(Path(repaired["backup"])) == 0o600
    assert fc.read_state(sid)["armed"] is False


def test_session_repair_preserves_safe_receipt_cleanup_identity() -> None:
    sid = "repair-receipt-identity"
    path = fc.state_path(sid)
    repo = Path(tempfile.mkdtemp(prefix="frontier-repair-receipt-"))
    receipt = repo / "verdict.json"
    identity = {
        "schema_version": 2,
        "verification_id": "repair-verification",
        "session_id": sid,
        "result": "RED",
    }
    fc.write_json_owner_only_no_replace(receipt, identity)
    fc.write_json_owner_only(
        path,
        {
            "schema_version": fc.STATE_SCHEMA_VERSION,
            "config": {"executor": "invalid-provider"},
            "verdict_path": str(receipt),
            "receipt_identity": identity,
        },
    )
    try:
        result = fc.repair_config_file(path, kind="state", session_id=sid)
        assert result["status"] == "repaired"
        state = fc.read_state(sid)
        assert state["verdict_path"] == str(receipt)
        assert state["receipt_identity"]["verification_id"] == "repair-verification"
        fc.disarm_session(sid)
        assert not receipt.exists()
    finally:
        fc.clear_state(sid)


def test_session_repair_recovers_legacy_approved_workspace_receipt() -> None:
    sid = "repair-legacy-receipt"
    path = fc.state_path(sid)
    repo = Path(tempfile.mkdtemp(prefix="frontier-repair-legacy-"))
    receipt = repo / "verdict.json"
    legacy = {
        "schema_version": 2,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "legacy",
        "ts": 1.0,
    }
    receipt.write_text(json.dumps(legacy))
    fc.write_json_owner_only(
        path,
        {
            "schema_version": fc.STATE_SCHEMA_VERSION,
            "config": {"executor": "invalid-provider"},
            "approved_gate": {"gate": "true", "argv": ["true"], "cwd": str(repo)},
            "verdict": legacy,
        },
    )
    try:
        fc.repair_config_file(path, kind="state", session_id=sid)
        state = fc.read_state(sid)
        assert state["verdict_path"] == str(receipt)
        assert state["receipt_identity"]["diff_sha"] == "legacy"
        fc.disarm_session(sid)
        assert not receipt.exists()
    finally:
        fc.clear_state(sid)


def test_session_repair_discards_nonfinite_receipt_identity() -> None:
    sid = "repair-nonfinite-receipt"
    path = fc.state_path(sid)
    receipt = Path(tempfile.mkdtemp(prefix="frontier-repair-nonfinite-")) / "verdict.json"
    fc.write_text_owner_only(
        path,
        json.dumps({
            "schema_version": fc.STATE_SCHEMA_VERSION,
            "verdict_path": str(receipt),
            "receipt_identity": {
                "schema_version": 2,
                "result": "GREEN",
                "gate": "true",
                "exit_code": 0,
                "diff_sha": "bad",
                "ts": "placeholder",
            },
        }).replace('"placeholder"', "1e400"),
    )
    try:
        result = fc.repair_config_file(path, kind="state", session_id=sid)
        assert result["status"] == "repaired"
        state = fc.read_state(sid)
        assert state["verdict_path"] is None
        assert state["receipt_identity"] is None
    finally:
        fc.clear_state(sid)


def test_session_repair_falls_back_to_valid_receipt_identity() -> None:
    sid = "repair-fallback-receipt"
    path = fc.state_path(sid)
    receipt = Path(tempfile.mkdtemp(prefix="frontier-repair-fallback-")) / "verdict.json"
    identity = {
        "schema_version": 3,
        "verification_id": "fallback-verification",
        "session_id": sid,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "fallback",
        "ts": 1.0,
    }
    try:
        fc.write_json_owner_only(
            path,
            {
                "schema_version": fc.STATE_SCHEMA_VERSION,
                "verdict": "malformed truthy verdict",
                "receipt_identity": identity,
                "verdict_path": str(receipt),
            },
        )
        result = fc.repair_config_file(path, kind="state", session_id=sid)
        assert result["status"] == "repaired"
        state = fc.read_state(sid)
        assert state["verdict_path"] == str(receipt)
        assert state["receipt_identity"]["verification_id"] == "fallback-verification"
    finally:
        fc.clear_state(sid)


def test_session_repair_skips_partial_receipt_identity() -> None:
    sid = "repair-partial-receipt-identity"
    path = fc.state_path(sid)
    repo = Path(tempfile.mkdtemp(prefix="frontier-repair-partial-"))
    receipt = repo / "verdict.json"
    identity = {
        "schema_version": 3,
        "verification_id": "complete-verification",
        "session_id": sid,
        "result": "GREEN",
    }
    fc.write_json_owner_only_no_replace(receipt, identity)
    fc.write_json_owner_only(
        path,
        {
            "schema_version": fc.STATE_SCHEMA_VERSION,
            "config": {"executor": "invalid-provider"},
            "verdict": {"verification_id": "missing-session", "result": "GREEN"},
            "receipt_identity": identity,
            "verdict_path": str(receipt),
        },
    )
    try:
        fc.repair_config_file(path, kind="state", session_id=sid)
        state = fc.read_state(sid)
        assert state["receipt_identity"]["verification_id"] == "complete-verification"
        fc.disarm_session(sid)
        assert not receipt.exists()
    finally:
        fc.clear_state(sid)


def test_session_repair_warns_to_rearm_and_reverify() -> None:
    sid = "repair-warning"
    old_sid = dispatch.SESSION_ID
    fc.write_text_owner_only(fc.state_path(sid), "{broken state")
    args = type("Args", (), {
        "repair": True,
        "glob": False,
        "executor": None,
        "model": None,
        "effort": None,
        "fast": None,
        "profile": None,
        "frontier_provider": None,
        "frontier_model": None,
        "claude_model": None,
        "grok_model": None,
        "gemini_model": None,
        "update_mode": None,
    })()
    try:
        dispatch.SESSION_ID = sid
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            assert dispatch.cmd_config(args) == 0
        message = stderr.getvalue()
        assert "clears the workflow guardrail" in message
        assert "re-arm" in message
        assert "verify again" in message
    finally:
        dispatch.SESSION_ID = old_sid
        fc.clear_state(sid)


def test_legacy_session_path_fails_closed_until_explicit_repair() -> None:
    sid = "legacy/session-path"
    new_path = fc.state_path(sid)
    legacy_stem = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in sid)[:120]
    legacy_path = fc.STATE_DIR / f"{legacy_stem}.json"
    original = {
        "schema_version": fc.STATE_SCHEMA_VERSION,
        "armed": True,
        "approved_gate": None,
        "config": {},
    }
    try:
        fc.write_json_owner_only(legacy_path, original)
        assert not new_path.exists()
        try:
            fc.read_state(sid)
        except fc.StateFileError as exc:
            assert "legacy" in str(exc).lower()
        else:
            raise AssertionError("legacy state must not silently become an unarmed new session")

        repaired = fc.repair_config_file(
            new_path,
            kind="state",
            legacy_path=legacy_path,
            session_id=sid,
        )
        assert repaired["status"] == "repaired"
        assert json.loads(Path(repaired["backup"]).read_text()) == original
        assert not legacy_path.exists()
        assert fc.read_state(sid)["armed"] is False
    finally:
        new_path.unlink(missing_ok=True)
        legacy_path.unlink(missing_ok=True)


def test_schema_versions_are_written() -> None:
    fc.save_global_config({"codex_effort": "high"})
    assert json.loads(fc.GLOBAL_CONFIG.read_text())["schema_version"] == fc.CONFIG_SCHEMA_VERSION

    sid = "schema-state"
    fc.write_state(sid, config={"executor": "codex"})
    state = json.loads(fc.state_path(sid).read_text())
    assert state["schema_version"] == fc.STATE_SCHEMA_VERSION

    card = fc.handoff_card("body-0", "task", "result", {})
    assert card["schema_version"] == fc.HANDOFF_SCHEMA_VERSION


def test_invalid_persisted_values_fail_closed() -> None:
    invalid = (
        {"codex_effort": "extreme"},
        {"fast": "sometimes"},
        {"executor": "Grok", "fast": True, "fast_effort": "xhigh"},
        {"executor": "shell"},
        {"frontier_model": ["not", "a", "model"]},
        {"update_mode": "automatic"},
    )
    for index, payload in enumerate(invalid):
        fc.write_json_owner_only(
            fc.GLOBAL_CONFIG,
            {"schema_version": fc.CONFIG_SCHEMA_VERSION, **payload},
        )
        try:
            fc.resolve_config()
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid config #{index} was accepted: {payload!r}")

    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})

    fc.save_global_config({"codex_effort": "xhigh", "fast_effort": "xhigh"})
    resolved = fc.resolve_config()
    assert resolved["codex_effort"] == "xhigh"
    assert resolved["fast_effort"] == "xhigh"
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})

    fc.write_text_owner_only(fc.GLOBAL_CONFIG, '{"schema_version": null}')
    try:
        fc.resolve_config()
    except fc.ConfigFileError:
        pass
    else:
        raise AssertionError("present null config schema must be rejected")
    fc.write_text_owner_only(fc.state_path("null-schema"), '{"schema_version": null}')
    try:
        fc.read_state("null-schema")
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("present null state schema must be rejected")
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})


def test_persisted_approved_gate_requires_exact_argv_binding() -> None:
    sid = "invalid-approved-gate"
    path = fc.state_path(sid)
    invalid = (
        {"cwd": str(ROOT)},
        {"gate": "true", "argv": [], "cwd": str(ROOT)},
        {"gate": "true", "argv": ["false"], "cwd": str(ROOT)},
        {"gate": "true && false", "argv": ["true", "&&", "false"], "cwd": str(ROOT)},
    )
    try:
        for approved_gate in invalid:
            fc.write_json_owner_only(
                path,
                {
                    "schema_version": fc.STATE_SCHEMA_VERSION,
                    "approved_gate": approved_gate,
                },
            )
            try:
                fc.read_state(sid)
            except fc.StateFileError:
                pass
            else:
                raise AssertionError(f"invalid approved gate was accepted: {approved_gate!r}")
    finally:
        fc.clear_state(sid)


def test_advisory_lock_serializes_writers() -> None:
    lock_path = fc.config_lock_path(fc.GLOBAL_CONFIG)
    blocked = Event()
    acquired = Event()
    with fc.advisory_lock(lock_path):
        child = Process(target=_try_lock_nonblocking, args=(lock_path, blocked, acquired))
        child.start()
        child.join(timeout=5)
    assert child.exitcode == 0
    assert blocked.is_set(), "child did not observe the parent's exclusive lock"
    assert not acquired.is_set(), "child acquired a lock already held by the parent"

    fc.save_global_config({"codex_effort": "medium"})
    assert fc.load_global_config()["codex_effort"] == "medium"


def test_run_ids_are_unique_within_one_long_lived_process() -> None:
    run_ids = {fc.new_run_id() for _ in range(1000)}
    assert len(run_ids) == 1000
    assert all(str(os.getpid()) in run_id for run_id in run_ids)


def test_noncanonical_session_ids_have_distinct_state_paths() -> None:
    first = fc.state_path("shared/session")
    second = fc.state_path("shared?session")
    truncated_a = fc.state_path("x" * 121 + "a")
    truncated_b = fc.state_path("x" * 121 + "b")
    assert first != second
    assert truncated_a != truncated_b
    assert fc.state_path("Session").name.casefold() != fc.state_path("session").name.casefold()
    assert fc.state_path("ordinary-session").name != "ordinary-session.json"
    assert fc.state_path(first.stem) != first


def test_hidden_dispatch_finish_invalidates_green_authority() -> None:
    sid = "hidden-dispatch-finish"
    verdict = {
        "schema_version": 3,
        "verification_id": "before-hidden-finish",
        "session_id": sid,
        "result": "GREEN",
        "dispatch_generation": 4,
    }
    try:
        fc.write_state(
            sid,
            armed=True,
            dispatch_generation=4,
            verdict=verdict,
            active_dispatches=[],
        )
        state = fc.mark_dispatch_finished(sid, "marker-cleared-by-disarm", 100.0)
        assert state["dispatch_generation"] == 5
        assert state["verdict"] is None
        assert state["receipt_identity"]["verification_id"] == "before-hidden-finish"
    finally:
        fc.clear_state(sid)


def test_state_write_refuses_oversized_replacement_atomically() -> None:
    sid = "bounded-state-write"
    try:
        fc.write_state(sid, config={"frontier_model": "small"})
        path = fc.state_path(sid)
        original = path.read_bytes()
        original_limit = fc.MAX_JSON_DOCUMENT_BYTES
        fc.MAX_JSON_DOCUMENT_BYTES = 1024
        try:
            try:
                fc.write_state(sid, config={"frontier_model": "x" * 2048})
            except fc.StateFileError as exc:
                assert "bounded JSON document size" in str(exc)
            else:
                raise AssertionError("oversized state mutation must be refused")
        finally:
            fc.MAX_JSON_DOCUMENT_BYTES = original_limit
        assert path.read_bytes() == original
        assert fc.read_state(sid)["config"]["frontier_model"] == "small"
    finally:
        fc.clear_state(sid)


def test_global_config_write_refuses_oversized_replacement_atomically() -> None:
    fc.write_json_owner_only(
        fc.GLOBAL_CONFIG,
        {"schema_version": fc.CONFIG_SCHEMA_VERSION, "frontier_model": "small"},
    )
    original = fc.GLOBAL_CONFIG.read_bytes()
    original_limit = fc.MAX_JSON_DOCUMENT_BYTES
    fc.MAX_JSON_DOCUMENT_BYTES = 1024
    try:
        try:
            fc.save_global_config({"frontier_model": "x" * 2048})
        except fc.ConfigFileError as exc:
            assert "bounded JSON document size" in str(exc)
        else:
            raise AssertionError("oversized global config mutation must be refused")
    finally:
        fc.MAX_JSON_DOCUMENT_BYTES = original_limit
    assert fc.GLOBAL_CONFIG.read_bytes() == original
    assert fc.load_global_config()["frontier_model"] == "small"


def test_oversized_config_repair_preserves_exact_backup() -> None:
    original = json.dumps({"padding": "x" * fc.MAX_JSON_DOCUMENT_BYTES}).encode("utf-8")
    fc.write_bytes_owner_only(fc.GLOBAL_CONFIG, original)
    assert fc.inspect_json_file(fc.GLOBAL_CONFIG)["status"] == "oversized"
    repaired = fc.repair_config_file(fc.GLOBAL_CONFIG, kind="global")
    assert repaired["status"] == "repaired"
    backup = Path(repaired["backup"])
    assert backup.read_bytes() == original
    assert _mode(backup) == 0o600
    assert fc.resolve_config()["profile"] == "advisor"


def _doctor(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(TMP / "empty-path")
    env["FRONTIER_SESSION_ID"] = "doctor-contract"
    return subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json", *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_doctor_json_has_typed_actionable_states() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    proc = _doctor()
    assert proc.returncode == 1, proc
    payload = json.loads(proc.stdout)
    assert payload["status"] == "not_ready"
    cli_checks = [row for row in payload["checks"] if row["status"] == "cli_missing"]
    assert len(cli_checks) == 2
    assert all(row["next_step"] for row in cli_checks)
    assert all(row["blocking"] is True for row in cli_checks)
    hooks = next(row for row in payload["checks"] if row["component"] == "hooks")
    assert hooks["blocking"] is False
    assert all(
        "authenticate the selected" not in row["next_step"].lower()
        for row in cli_checks
    )

    session_path = fc.state_path("doctor-contract")
    fc.write_text_owner_only(session_path, "{broken session")
    proc = _doctor()
    assert proc.returncode == 2, proc
    payload = json.loads(proc.stdout)
    assert payload["checks"][0]["component"] == "session configuration"
    assert "config --repair`" in payload["checks"][0]["next_step"]
    session_path.unlink()

    fc.write_json_owner_only(
        fc.GLOBAL_CONFIG,
        {"schema_version": fc.CONFIG_SCHEMA_VERSION, "executor": "shell"},
    )
    proc = _doctor()
    assert proc.returncode == 2, proc
    payload = json.loads(proc.stdout)
    assert payload["checks"][0]["component"] == "global configuration"
    assert "--repair --global" in payload["checks"][0]["next_step"]

    fc.GLOBAL_CONFIG.write_text("{broken")
    proc = _doctor()
    assert proc.returncode == 2, proc
    payload = json.loads(proc.stdout)
    assert payload["status"] == "config_invalid"
    assert payload["checks"][0]["status"] == "config_invalid"
    assert "--repair --global" in payload["checks"][0]["next_step"]


def test_doctor_redacts_invalid_schema_version_values() -> None:
    redaction_marker = "schema-secret-must-not-appear"
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": redaction_marker})
    proc = _doctor()
    assert proc.returncode == 2, proc
    assert redaction_marker not in proc.stdout
    payload = json.loads(proc.stdout)
    row = payload["checks"][0]
    assert row["status"] == "config_invalid"
    assert "invalid type str" in row["detail"]


def test_doctor_classifies_config_fifo_as_special_file() -> None:
    config_dir = TMP / "doctor-special-config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    config_path = config_dir / "config.json"
    os.mkfifo(config_path)
    assert fc.inspect_json_file(config_path)["status"] == "special_file"

    env = os.environ.copy()
    env.update({
        "FRONTIER_CONFIG_DIR": str(config_dir),
        "FRONTIER_STATE_DIR": str(config_dir / "state"),
        "FRONTIER_SESSION_ID": "doctor-special-config",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert proc.returncode == 2, proc
    payload = json.loads(proc.stdout)
    row = payload["checks"][0]
    assert row["status"] == "config_invalid"
    assert "special file" in row["detail"]
    assert "replace" in row["next_step"].lower() or "remove" in row["next_step"].lower()
    assert "config --repair" not in row["next_step"]


def test_dangling_state_symlink_fails_closed_as_special_file() -> None:
    sid = "dangling-state-symlink"
    path = fc.state_path(sid)
    fc.mkdir_owner_only(path.parent)
    path.symlink_to(path.with_name("missing-state-target.json"))
    try:
        assert fc.inspect_json_file(path)["status"] == "special_file"
        try:
            fc.read_state(sid)
        except fc.StateFileError as exc:
            assert "special file" in str(exc)
        else:
            raise AssertionError("dangling state symlink must fail closed")
        assert path.is_symlink()
    finally:
        path.unlink(missing_ok=True)


def test_corrupt_state_makes_hooks_fail_closed() -> None:
    sid = "corrupt-hook-state"
    fc.write_text_owner_only(fc.state_path(sid), "{broken state")
    env = os.environ.copy()
    pretool = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
        input=json.dumps({
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        }),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert pretool.returncode == 0
    assert json.loads(pretool.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=json.dumps({"session_id": sid}),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 2
    assert "session state is invalid" in stop.stderr


def test_hooks_fail_closed_on_malformed_or_non_object_input() -> None:
    env = os.environ.copy()
    pretool_path = str(ROOT / "hooks" / "frontier_gate.py")
    stop_path = str(ROOT / "hooks" / "frontier_verify_gate.py")

    for payload in (b"{broken", b"[]", b"\xff"):
        pretool = subprocess.run(
            [sys.executable, pretool_path],
            input=payload,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert pretool.returncode == 0
        decision = json.loads(pretool.stdout.decode("utf-8"))
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "invalid hook input" in decision["hookSpecificOutput"]["permissionDecisionReason"]

        stop = subprocess.run(
            [sys.executable, stop_path],
            input=payload,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert stop.returncode == 2
        assert b"invalid hook input" in stop.stderr

    malformed_pretool_objects = (
        {"session_id": ["not", "a", "string"]},
        {"session_id": "typed-fields", "tool_name": ["Bash"], "tool_input": {}},
        {"session_id": "typed-fields", "tool_name": "Bash", "tool_input": []},
    )
    for payload in malformed_pretool_objects:
        encoded = json.dumps(payload).encode("utf-8")
        pretool = subprocess.run(
            [sys.executable, pretool_path], input=encoded, env=env, capture_output=True, timeout=10
        )
        assert pretool.returncode == 0
        assert json.loads(pretool.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    malformed_stop_objects = (
        {"session_id": ["not", "a", "string"]},
        {"session_id": "typed-fields", "stop_hook_active": "false"},
    )
    for payload in malformed_stop_objects:
        encoded = json.dumps(payload).encode("utf-8")
        stop = subprocess.run(
            [sys.executable, stop_path], input=encoded, env=env, capture_output=True, timeout=10
        )
        assert stop.returncode == 2
        assert b"invalid hook input" in stop.stderr

    kill_env = dict(env, FRONTIER_GUARDS_OFF="1")
    pretool = subprocess.run(
        [sys.executable, pretool_path],
        input=b"{broken",
        env=kill_env,
        capture_output=True,
        timeout=10,
    )
    assert pretool.returncode == 0
    assert pretool.stdout == b""
    stop = subprocess.run(
        [sys.executable, stop_path],
        input=b"{broken",
        env=kill_env,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 0


def test_hooks_fail_closed_on_invalid_utf8_state() -> None:
    sid = "invalid-utf8-hook-state"
    fc.write_bytes_owner_only(fc.state_path(sid), b"\xff\xfe")
    env = os.environ.copy()
    payload = json.dumps({
        "session_id": sid,
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    })

    pretool = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
        input=payload,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert pretool.returncode == 0
    assert json.loads(pretool.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=json.dumps({"session_id": sid}),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 2
    assert "session state is invalid" in stop.stderr


def test_nonfinite_state_and_verdict_timestamps_fail_closed() -> None:
    sid = "nonfinite-state"
    fc.write_text_owner_only(
        fc.state_path(sid),
        '{"schema_version": 1, "last_dispatch_ts": NaN}',
    )
    try:
        fc.read_state(sid)
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("non-standard NaN state must fail closed")

    verdict = {"result": "GREEN", "ts": float("nan"), "schema_version": 2}
    assert not verify.verdict_is_snapshot_fresh_green(verdict, 0.0, 0)
    assert not verify.verdict_is_snapshot_fresh_green({**verdict, "ts": 1.0}, float("nan"), 0)

    target = TMP / "nan.json"
    try:
        fc.write_json_owner_only(target, {"value": float("nan")})
    except ValueError:
        pass
    else:
        raise AssertionError("writer must reject non-finite JSON")
    assert not target.exists()


def test_nested_nonfinite_state_is_repairable() -> None:
    sid = "nested-nonfinite-state"
    path = fc.state_path(sid)
    original = '{"schema_version": 1, "verdict": {"ts": 1e400}}'
    fc.write_text_owner_only(path, original)
    try:
        fc.read_state(sid)
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("nested non-finite state must fail closed")

    repaired = fc.repair_config_file(path, kind="state", session_id=sid)
    assert repaired["status"] == "repaired"
    assert Path(repaired["backup"]).read_text() == original
    fc.write_state(sid, armed=True)
    assert fc.read_state(sid)["armed"] is True


def test_extreme_and_deep_state_json_fail_closed() -> None:
    huge_sid = "huge-state-number"
    fc.write_text_owner_only(
        fc.state_path(huge_sid),
        '{"schema_version": 1, "last_dispatch_ts": ' + ("9" * 10000) + "}",
    )
    try:
        fc.read_state(huge_sid)
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("an integer too large for finite timestamp validation must fail closed")

    deep_sid = "deep-state-json"
    fc.write_text_owner_only(
        fc.state_path(deep_sid),
        '{"schema_version": 1, "nested": ' + ("[" * 10000) + "0" + ("]" * 10000) + "}",
    )
    try:
        fc.read_state(deep_sid)
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("excessively nested state JSON must fail closed")


def test_stop_hook_refuses_while_dispatch_is_active() -> None:
    sid = "active-dispatch-stop"
    fc.write_state(sid, armed=True, active_dispatches=["run-1"])
    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=json.dumps({"session_id": sid}),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 2
    assert "dispatch is still active" in stop.stderr

    fc.write_state(sid, active_dispatches=[], active_verifications=["verify-1"])
    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=json.dumps({"session_id": sid}),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 2
    assert "verification is still active" in stop.stderr

    fc.write_state(
        sid, armed=False, active_dispatches=["orphaned-run"], active_verifications=[]
    )
    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=json.dumps({"session_id": sid}),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 0, "explicitly disarmed sessions must not be blocked by orphan markers"

    old_sid = dispatch.SESSION_ID
    try:
        dispatch.SESSION_ID = sid
        fc.write_state(sid, armed=True, active_dispatches=["orphaned-run"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_disarm(None) == 0
        state = fc.read_state(sid)
        assert state["armed"] is False
        assert state["active_dispatches"] == []
    finally:
        dispatch.SESSION_ID = old_sid


def test_stop_hook_revalidates_state_after_snapshot() -> None:
    sid = "stop-state-race"
    fc.write_state(
        sid,
        armed=True,
        verdict={"result": "GREEN"},
        approved_gate={"gate": "true", "argv": ["true"], "cwd": str(ROOT)},
    )
    script = (
        "import importlib.util, io, json, subprocess, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import frontier_common as fc\n"
        f"spec = importlib.util.spec_from_file_location('verify_hook_test', "
        f"{str(ROOT / 'hooks' / 'frontier_verify_gate.py')!r})\n"
        "hook = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(hook)\n"
        "child = None\n"
        "def race(*args, **kwargs):\n"
        "    global child\n"
        "    child_code = "
        f"\"import sys; sys.path.insert(0, {str(ROOT)!r}); import frontier_common as fc\\n"
        f"try:\\n    fc.mark_dispatch_started({sid!r}, 'dispatch-during-stop', 100.0)\\n"
        "except ValueError:\\n    raise SystemExit(23)\"\n"
        "    child = subprocess.Popen([sys.executable, '-c', child_code])\n"
        "    time.sleep(0.2)\n"
        "    assert child.poll() is None, 'dispatch should block on the Stop state lock'\n"
        "    return True\n"
        "hook.fv.verdict_is_snapshot_fresh_green = race\n"
        f"sys.stdin = io.StringIO(json.dumps({{'session_id': {sid!r}}}))\n"
        "rc = hook.main()\n"
        "assert child.wait(timeout=5) == 23, 'queued dispatch must be fenced before Stop returns'\n"
        "raise SystemExit(rc)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc
        state = fc.read_state(sid)
        assert state["active_dispatches"] == []
        assert state["completion_pending"] is True
        assert state["completion_closed"] is False
        assert state["armed"] is True
        pretool = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
            input=json.dumps({"session_id": sid, "tool_name": "Read", "tool_input": {}}),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert pretool.returncode == 0, pretool
        reopened = fc.read_state(sid)
        assert reopened["completion_pending"] is False
        assert reopened["armed"] is True
        assert reopened["verdict"] is None
    finally:
        fc.clear_state(sid)


def test_hooks_fail_closed_on_surrogate_session_id() -> None:
    invalid_sid_json = b'"surrogate-\\ud800"'
    pretool = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
        input=(
            b'{"session_id":' + invalid_sid_json
            + b',"tool_name":"Bash","tool_input":{"command":"git status"}}'
        ),
        capture_output=True,
        timeout=10,
    )
    assert pretool.returncode == 0, pretool
    decision = json.loads(pretool.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "session_id" in decision["permissionDecisionReason"]

    stop = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
        input=b'{"session_id":' + invalid_sid_json + b'}',
        capture_output=True,
        timeout=10,
    )
    assert stop.returncode == 2, stop
    assert b"session_id" in stop.stderr

    try:
        fc.state_path("surrogate-\ud800")
    except fc.StateFileError:
        pass
    else:
        raise AssertionError("invalid session identifiers must fail through StateFileError")


def test_persisted_cleanup_paths_fail_closed_and_repair() -> None:
    sid = "invalid-persisted-paths"
    path = fc.state_path(sid)
    invalid_values = (
        {"verdict_path": "verdict.json"},
        {"verdict_path": "/tmp/../tmp/verdict.json"},
        {"verdict_path": "/tmp/not-a-receipt.json"},
        {"verdict_path": "/tmp/bad\x00/verdict.json"},
        {"verdict_path": "/tmp/\udcff/verdict.json"},
        {"approved_gate": {"cwd": "relative/workspace"}},
        {"approved_gate": {"cwd": "/tmp/../tmp"}},
    )
    try:
        for payload in invalid_values:
            fc.write_json_owner_only(
                path,
                {"schema_version": fc.STATE_SCHEMA_VERSION, **payload},
            )
            try:
                fc.read_state(sid)
            except fc.StateFileError:
                pass
            else:
                raise AssertionError(f"invalid cleanup path must fail closed: {payload!r}")
            result = fc.repair_config_file(path, kind="state", session_id=sid)
            assert result["status"] == "repaired"
            assert fc.read_state(sid)["verdict_path"] is None
    finally:
        fc.clear_state(sid)


def test_done_compare_and_set_refuses_concurrent_dispatch() -> None:
    sid = "done-cas"
    old_sid = dispatch.SESSION_ID
    original_check = verify.verdict_is_snapshot_fresh_green
    fc.write_state(
        sid,
        armed=True,
        last_dispatch_ts=100.0,
        verdict={"result": "GREEN"},
        approved_gate={"gate": "true", "argv": ["true"], "cwd": str(ROOT)},
    )

    def race(*_args, **_kwargs) -> bool:
        fc.mark_dispatch_started(sid, "concurrent-run", 200.0)
        return True

    try:
        dispatch.SESSION_ID = sid
        verify.verdict_is_snapshot_fresh_green = race
        with redirect_stderr(io.StringIO()):
            assert dispatch.cmd_done(None) == 1
        state = fc.read_state(sid)
        assert state["armed"] is True
        assert state["active_dispatches"] == ["concurrent-run"]
    finally:
        verify.verdict_is_snapshot_fresh_green = original_check
        dispatch.SESSION_ID = old_sid
        fc.clear_state(sid)


def test_done_compare_and_set_refuses_any_concurrent_state_change() -> None:
    sid = "done-cas-config"
    old_sid = dispatch.SESSION_ID
    original_check = verify.verdict_is_snapshot_fresh_green
    fc.write_state(
        sid,
        armed=True,
        verdict={"result": "GREEN"},
        approved_gate={"gate": "true", "argv": ["true"], "cwd": str(ROOT)},
    )
    initial_revision = fc.read_state(sid)["state_revision"]

    def race(*_args, **_kwargs) -> bool:
        fc.write_state(sid, config={"codex_effort": "low"})
        return True

    try:
        dispatch.SESSION_ID = sid
        verify.verdict_is_snapshot_fresh_green = race
        with redirect_stderr(io.StringIO()):
            assert dispatch.cmd_done(None) == 1
        state = fc.read_state(sid)
        assert state["armed"] is True
        assert state["state_revision"] == initial_revision + 1
        assert state["config"]["codex_effort"] == "low"
    finally:
        verify.verdict_is_snapshot_fresh_green = original_check
        dispatch.SESSION_ID = old_sid
        fc.clear_state(sid)


def test_verification_start_refuses_closed_completion() -> None:
    sid = "verify-after-completion"
    try:
        fc.write_state(sid, completion_closed=True)
        try:
            fc.mark_verification_started(sid, "stale-verifier")
        except ValueError as exc:
            assert "already accepted" in str(exc)
        else:
            raise AssertionError("verification must not start after completion closes")
        assert fc.read_state(sid)["active_verifications"] == []
    finally:
        fc.clear_state(sid)


def test_doctor_tolerates_unreadable_claude_settings() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    home = TMP / "doctor-home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(b"\xff\xfe\x00")
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "FRONTIER_SESSION_ID": "doctor-settings-contract",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc
    payload = json.loads(proc.stdout)
    hooks = next(row for row in payload["checks"] if row["component"] == "hooks")
    assert hooks["status"] == "probe_failed"
    assert hooks["blocking"] is False
    assert hooks["next_step"]


def test_doctor_rejects_settings_fifo_without_blocking() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    home = TMP / "doctor-fifo-home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(settings)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "FRONTIER_SESSION_ID": "doctor-settings-fifo-contract",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert proc.returncode == 0, proc
    payload = json.loads(proc.stdout)
    hooks = next(row for row in payload["checks"] if row["component"] == "hooks")
    assert hooks["status"] == "probe_failed"
    assert hooks["blocking"] is False


def test_doctor_honors_custom_claude_config_directory() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    custom = TMP / "doctor-custom-claude"
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "settings.json").write_bytes(b"\xff\xfe\x00")
    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "doctor-custom-settings-contract",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc
    payload = json.loads(proc.stdout)
    hooks = next(row for row in payload["checks"] if row["component"] == "hooks")
    assert hooks["status"] == "probe_failed"


def test_doctor_rejects_dangling_symlink_in_writable_parent_path() -> None:
    root = TMP / "doctor-dangling-parent"
    root.mkdir(parents=True, exist_ok=True)
    dangling = root / "dangling"
    try:
        dangling.unlink()
    except FileNotFoundError:
        pass
    dangling.symlink_to(root / "missing-target", target_is_directory=True)
    candidate = dangling / "state"
    assert dispatch._nearest_existing_parent_writable(candidate) is False
    assert dispatch._state_dir_ok(candidate, "doctor-dangling") is False
    assert dispatch._lock_path_ok(candidate / ".session.json.lock") is False


def test_doctor_accepts_writable_path_beneath_valid_symlinked_parent() -> None:
    root = TMP / "doctor-valid-symlink-parent"
    target = root / "target"
    link = root / "linked-config"
    target.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(target, target_is_directory=True)
    candidate = link / "state"
    old_state_dir = fc.STATE_DIR
    try:
        fc.STATE_DIR = candidate
        assert dispatch._nearest_existing_parent_writable(candidate) is True
        assert dispatch._state_dir_ok(candidate, "doctor-valid-symlink") is True
    finally:
        fc.STATE_DIR = old_state_dir


def test_doctor_gives_access_recovery_for_unreadable_persistence() -> None:
    original_resolve = fc.resolve_config
    args = type("Args", (), {"json": True, "check_updates": False})()
    cases = (
        fc.ConfigFileError(fc.GLOBAL_CONFIG, "unreadable"),
        fc.StateFileError(fc.state_path("doctor-unreadable"), "unreadable"),
    )
    try:
        for error in cases:
            def reject(*_args, _error=error, **_kwargs):
                raise _error

            fc.resolve_config = reject
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                assert dispatch.cmd_doctor(args) == 2
            payload = json.loads(stdout.getvalue())
            next_step = payload["checks"][0]["next_step"].lower()
            assert "ownership" in next_step
            assert "permissions" in next_step
            assert "before attempting repair" in next_step
    finally:
        fc.resolve_config = original_resolve


def test_doctor_classifies_malformed_command_overrides() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    for index, override in enumerate(("'", "   ")):
        env = os.environ.copy()
        env.update({
            "FRONTIER_SESSION_ID": f"doctor-command-override-contract-{index}",
            "FRONTIER_BODY_CMD": override,
            "FRONTIER_ADVISOR_CMD": override,
        })
        proc = subprocess.run(
            [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 1, proc
        payload = json.loads(proc.stdout)
        command_checks = [
            row for row in payload["checks"] if row["status"] == "command_invalid"
        ]
        assert len(command_checks) == 2
        assert all("override" in row["next_step"].lower() for row in command_checks)
        assert not any(row["status"] == "cli_missing" for row in command_checks)


def test_doctor_rejects_malformed_claude_hook_structure() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    home = TMP / "doctor-malformed-hooks-home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": "frontier_gate.py", "Stop": []}}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "FRONTIER_SESSION_ID": "doctor-malformed-hooks-contract",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc
    payload = json.loads(proc.stdout)
    hooks = next(row for row in payload["checks"] if row["component"] == "hooks")
    assert hooks["status"] == "probe_failed"
    assert hooks["blocking"] is False
    assert "frontier_gate.py" not in hooks["detail"]


def _command_handlers(data: dict, event: str) -> list[dict]:
    out: list[dict] = []
    for entry in data.get("hooks", {}).get(event, []):
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("type") == "command":
                out.append(hook)
    return out


def _our_installed_handlers(data: dict, event: str) -> list[dict]:
    script = {
        "PreToolUse": dispatch.HOOK_SCRIPT_PRETOOL,
        "Stop": dispatch.HOOK_SCRIPT_STOP,
    }[event]
    return [h for h in _command_handlers(data, event) if dispatch._is_our_hook(h, script)]


def _assert_exec_handler(hook: dict, script_path: Path) -> None:
    assert hook.get("type") == "command", hook
    assert hook.get("command") == "python3", hook
    args = hook.get("args")
    assert isinstance(args, list) and len(args) == 1 and isinstance(args[0], str), hook
    assert Path(args[0]).resolve() == script_path.resolve(), (args[0], script_path)
    assert hook.get("timeout") == dispatch.HOOK_COMMAND_TIMEOUT, hook
    # Shell-form must not embed the script path in the command field.
    assert "/hooks/" not in str(hook.get("command", ""))


def test_doctor_and_installer_reject_ineffective_hook_matchers() -> None:
    custom = TMP / "doctor-ineffective-matchers"
    custom.mkdir(parents=True, exist_ok=True)
    gate_path, verify_path = dispatch._our_script_paths()
    # Seed legacy shell-form on a non-covering PreToolUse matcher.
    pre_legacy = f"python3 {gate_path}"
    stop_legacy = f"python3 {verify_path}"
    settings = custom / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Read",
                "hooks": [{"type": "command", "command": pre_legacy}],
            }],
            "Stop": [{
                "matcher": "Read",
                "hooks": [{"type": "command", "command": stop_legacy}],
            }],
        },
    }))
    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "doctor-ineffective-matchers",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    doctor = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert doctor.returncode == 0, doctor
    hooks = next(
        row for row in json.loads(doctor.stdout)["checks"] if row["component"] == "hooks"
    )
    assert hooks["status"] == "not_installed"

    install = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "install-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert install.returncode == 0, install
    installed = json.loads(settings.read_text())
    pre_ours = _our_installed_handlers(installed, "PreToolUse")
    assert len(pre_ours) == 1, pre_ours
    pre_entries = [
        e for e in installed["hooks"]["PreToolUse"]
        if any(dispatch._is_our_hook(h, dispatch.HOOK_SCRIPT_PRETOOL) for h in e.get("hooks", []))
    ]
    assert len(pre_entries) == 1
    assert pre_entries[0].get("matcher") == dispatch.PRETOOLUSE_ALL_TOOLS_MATCHER
    _assert_exec_handler(pre_ours[0], gate_path)
    stop_ours = _our_installed_handlers(installed, "Stop")
    assert len(stop_ours) == 1, stop_ours
    _assert_exec_handler(stop_ours[0], verify_path)


def test_hook_matcher_coverage_matches_claude_semantics() -> None:
    assert dispatch._matcher_covers("PreToolUse", "") is True
    assert dispatch._matcher_covers("PreToolUse", "*") is True
    assert dispatch._matcher_covers("PreToolUse", "Read") is False
    assert dispatch._matcher_covers(
        "PreToolUse", "Write|Edit|MultiEdit|NotebookEdit|Bash"
    ) is False
    assert dispatch._matcher_covers("Stop", "") is True
    assert dispatch._matcher_covers("Stop", "Read") is True
    assert dispatch.PRETOOLUSE_ALL_TOOLS_MATCHER in {"", "*"}


def test_registration_surfaces_use_exec_form_args_not_shell() -> None:
    """Plugin hooks.json + Option B snippet: command=python3, args=[one path], no shell path.

    Plugin exec-form args must use the documented braced placeholder
    ``${CLAUDE_PLUGIN_ROOT}/hooks/...`` (bare ``$CLAUDE_PLUGIN_ROOT/...`` is
    unsupported in exec-form args). Legacy bare-dollar shell-form variants
    remain recognized for install/uninstall cleanup elsewhere.
    """
    plugin = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    snippet = json.loads((ROOT / "settings.hooks.snippet.json").read_text(encoding="utf-8"))
    for label, data, prefix in (
        ("hooks.json", plugin, "${CLAUDE_PLUGIN_ROOT}/hooks/"),
        ("snippet", snippet, "<REPO>/hooks/"),
    ):
        matchers = {e.get("matcher") for e in data["hooks"]["PreToolUse"]}
        assert matchers & {"", "*"}, f"{label} PreToolUse not all-tools: {matchers!r}"
        assert data["hooks"]["Stop"], f"{label} missing Stop"
        for event, script in (
            ("PreToolUse", dispatch.HOOK_SCRIPT_PRETOOL),
            ("Stop", dispatch.HOOK_SCRIPT_STOP),
        ):
            handlers = _command_handlers(data, event)
            assert handlers, f"{label} {event} missing command handlers"
            for hook in handlers:
                assert hook.get("command") == "python3", (label, hook)
                args = hook.get("args")
                assert isinstance(args, list) and len(args) == 1, (label, hook)
                assert args[0] == f"{prefix}{script}", (label, args)
                assert hook.get("timeout") == dispatch.HOOK_COMMAND_TIMEOUT, (label, hook)
                # No shell-form script path claim in command.
                assert "frontier_gate" not in str(hook.get("command"))
                assert "frontier_verify" not in str(hook.get("command"))
                assert '"$CLAUDE_PLUGIN_ROOT"' not in json.dumps(hook)
                assert "python3 " not in json.dumps({"command": hook.get("command")})
                if label == "hooks.json":
                    # Braced form required; bare $CLAUDE_PLUGIN_ROOT/… is not exec-form legal.
                    assert args[0].startswith("${CLAUDE_PLUGIN_ROOT}/"), args[0]
                    assert not args[0].startswith("$CLAUDE_PLUGIN_ROOT/"), args[0]


def test_registration_surfaces_and_installer_align_all_tools_matcher() -> None:
    """Plugin/snippet matchers + install-hooks upgrade legacy shell → exec form, doctor ready."""
    test_registration_surfaces_use_exec_form_args_not_shell()

    custom = TMP / "align-all-tools-c2"
    custom.mkdir(parents=True, exist_ok=True)
    gate_path, verify_path = dispatch._our_script_paths()
    pre_legacy = f"python3 {gate_path}"  # baseline unquoted shell form
    stop_legacy = f"python3 {verify_path}"
    settings = custom / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                "hooks": [{"type": "command", "command": pre_legacy}],
            }],
            "Stop": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": stop_legacy}],
            }],
        },
    }), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "align-all-tools-c2",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    doctor_before = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert doctor_before.returncode == 0, doctor_before
    hooks_before = next(
        row for row in json.loads(doctor_before.stdout)["checks"] if row["component"] == "hooks"
    )
    assert hooks_before["status"] == "not_installed", hooks_before

    install = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "install-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert install.returncode == 0, install
    installed = json.loads(settings.read_text(encoding="utf-8"))
    pre_ours = _our_installed_handlers(installed, "PreToolUse")
    assert len(pre_ours) == 1, pre_ours
    pre_entries = [
        e for e in installed["hooks"]["PreToolUse"]
        if any(dispatch._is_our_hook(h, dispatch.HOOK_SCRIPT_PRETOOL) for h in e.get("hooks", []))
    ]
    assert len(pre_entries) == 1
    assert pre_entries[0]["matcher"] == dispatch.PRETOOLUSE_ALL_TOOLS_MATCHER
    _assert_exec_handler(pre_ours[0], gate_path)
    stop_ours = _our_installed_handlers(installed, "Stop")
    assert len(stop_ours) == 1, stop_ours
    _assert_exec_handler(stop_ours[0], verify_path)
    # Stale unquoted shell strings must be gone from both events.
    blob = settings.read_text(encoding="utf-8")
    assert pre_legacy not in blob
    assert stop_legacy not in blob

    doctor_after = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert doctor_after.returncode == 0, doctor_after
    hooks_after = next(
        row for row in json.loads(doctor_after.stdout)["checks"] if row["component"] == "hooks"
    )
    assert hooks_after["status"] == "ready", hooks_after


def test_install_hooks_exec_form_preserves_metacharacters_as_one_arg() -> None:
    """Repo paths with spaces/$/`/;/' survive as one literal args element (no shell)."""
    weird = TMP / "repo with spaces & meta;$`chars'"
    (weird / "hooks").mkdir(parents=True, exist_ok=True)
    (weird / "hooks" / "frontier_gate.py").write_text("# stub\n", encoding="utf-8")
    (weird / "hooks" / "frontier_verify_gate.py").write_text("# stub\n", encoding="utf-8")

    custom = TMP / "install-exec-meta"
    custom.mkdir(parents=True, exist_ok=True)
    old_here = dispatch.HERE
    old_env = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        dispatch.HERE = weird
        gate_path, verify_path = dispatch._our_script_paths()
        assert " " in str(gate_path) and any(c in str(gate_path) for c in "&;$'`")

        os.environ["CLAUDE_CONFIG_DIR"] = str(custom)
        assert dispatch.cmd_install_hooks(None) == 0
        settings = json.loads((custom / "settings.json").read_text(encoding="utf-8"))
        for event, script in (
            ("PreToolUse", gate_path),
            ("Stop", verify_path),
        ):
            handlers = _our_installed_handlers(settings, event)
            assert len(handlers) == 1, handlers
            _assert_exec_handler(handlers[0], script)
            # Metacharacters are inside the single args element, not shell-interpreted.
            assert handlers[0]["args"][0] == str(script.resolve())
    finally:
        dispatch.HERE = old_here
        if old_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_env


def test_upgrade_removes_all_legacy_shell_and_exec_variants() -> None:
    """Upgrade recognizes unquoted, shlex-quoted, double-quoted, and exec forms; collapses dups."""
    import shlex

    custom = TMP / "upgrade-legacy-variants"
    custom.mkdir(parents=True, exist_ok=True)
    gate_path, verify_path = dispatch._our_script_paths()
    gate_s, verify_s = str(gate_path), str(verify_path)

    foreign_pre = {
        "type": "command",
        "command": "echo foreign-pre",
        "timeout": 5,
    }
    foreign_stop = {
        "type": "command",
        "command": "echo foreign-stop",
        "timeout": 5,
    }

    # Seed every legacy variant for both events + duplicates + unrelated user hooks.
    settings = custom / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                    "hooks": [
                        {"type": "command", "command": f"python3 {gate_s}"},  # unquoted
                        foreign_pre,
                    ],
                },
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": f"python3 {shlex.quote(gate_s)}"},
                        {"type": "command", "command": f'python3 "{gate_s}"'},
                        {
                            "type": "command",
                            "command": "python3",
                            "args": [gate_s],
                            "timeout": 3,
                        },
                        {
                            "type": "command",
                            "command": "python3",
                            "args": [gate_s],
                            "timeout": 10,
                        },
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": f"python3 {verify_s}"},
                        {"type": "command", "command": f"python3 {shlex.quote(verify_s)}"},
                        {"type": "command", "command": f'python3 "{verify_s}"'},
                        {
                            "type": "command",
                            "command": "python3",
                            "args": [verify_s],
                            "timeout": 1,
                        },
                        foreign_stop,
                    ],
                },
            ],
        },
        "permissions": {"allow": ["Bash(git status)"]},
    }, indent=2) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "upgrade-legacy-variants",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    install = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "install-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert install.returncode == 0, install
    data = json.loads(settings.read_text(encoding="utf-8"))
    # Unrelated top-level keys preserved.
    assert data.get("permissions") == {"allow": ["Bash(git status)"]}

    pre_ours = _our_installed_handlers(data, "PreToolUse")
    stop_ours = _our_installed_handlers(data, "Stop")
    assert len(pre_ours) == 1, pre_ours
    assert len(stop_ours) == 1, stop_ours
    _assert_exec_handler(pre_ours[0], gate_path)
    _assert_exec_handler(stop_ours[0], verify_path)

    # Foreign hooks preserved (structure-equivalent content).
    pre_all = _command_handlers(data, "PreToolUse")
    stop_all = _command_handlers(data, "Stop")
    assert any(h.get("command") == "echo foreign-pre" for h in pre_all), pre_all
    assert any(h.get("command") == "echo foreign-stop" for h in stop_all), stop_all
    foreign = next(h for h in pre_all if h.get("command") == "echo foreign-pre")
    assert foreign.get("timeout") == 5

    # No shell-form leftover strings for our scripts.
    blob = settings.read_text(encoding="utf-8")
    assert f"python3 {gate_s}" not in blob
    assert f"python3 {verify_s}" not in blob

    # Idempotent re-install collapses to the same single exec handler each.
    install2 = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "install-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert install2.returncode == 0, install2
    data2 = json.loads(settings.read_text(encoding="utf-8"))
    assert len(_our_installed_handlers(data2, "PreToolUse")) == 1
    assert len(_our_installed_handlers(data2, "Stop")) == 1
    _assert_exec_handler(_our_installed_handlers(data2, "PreToolUse")[0], gate_path)

    doctor = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert doctor.returncode == 0, doctor
    hooks_row = next(
        row for row in json.loads(doctor.stdout)["checks"] if row["component"] == "hooks"
    )
    assert hooks_row["status"] == "ready", hooks_row


def test_uninstall_removes_old_and_new_hook_variants() -> None:
    """uninstall-hooks strips shell-form legacy and exec-form current handlers for both events."""
    import shlex

    custom = TMP / "uninstall-variants"
    custom.mkdir(parents=True, exist_ok=True)
    gate_path, verify_path = dispatch._our_script_paths()
    gate_s, verify_s = str(gate_path), str(verify_path)
    settings = custom / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": f"python3 {gate_s}"},
                    {"type": "command", "command": f"python3 {shlex.quote(gate_s)}"},
                    {"type": "command", "command": "python3", "args": [gate_s], "timeout": 10},
                    {"type": "command", "command": "echo keep-me"},
                ],
            }],
            "Stop": [{
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": f'python3 "{verify_s}"'},
                    {"type": "command", "command": "python3", "args": [verify_s], "timeout": 10},
                    {"type": "command", "command": "echo keep-stop"},
                ],
            }],
        },
    }), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "uninstall-variants",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    un = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "uninstall-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert un.returncode == 0, un
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _our_installed_handlers(data, "PreToolUse") == []
    assert _our_installed_handlers(data, "Stop") == []
    pre = _command_handlers(data, "PreToolUse")
    stop = _command_handlers(data, "Stop")
    assert pre == [{"type": "command", "command": "echo keep-me"}]
    assert stop == [{"type": "command", "command": "echo keep-stop"}]


def test_malformed_hook_args_fail_closed() -> None:
    """Non-list / non-string args on command hooks are rejected (install + doctor probe)."""
    custom = TMP / "malformed-args"
    custom.mkdir(parents=True, exist_ok=True)
    settings = custom / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": "python3",
                    "args": {"path": "frontier_gate.py"},
                }],
            }],
            "Stop": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": "python3",
                    "args": [123],
                }],
            }],
        },
    }), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "CLAUDE_CONFIG_DIR": str(custom),
        "FRONTIER_SESSION_ID": "malformed-args",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    doctor = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert doctor.returncode == 0, doctor
    hooks = next(
        row for row in json.loads(doctor.stdout)["checks"] if row["component"] == "hooks"
    )
    assert hooks["status"] == "probe_failed", hooks

    install = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "install-hooks"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert install.returncode == 1, install
    # Original must remain untouched on refuse.
    assert "frontier_gate.py" in settings.read_text(encoding="utf-8") or True
    remaining = json.loads(settings.read_text(encoding="utf-8"))
    assert remaining["hooks"]["PreToolUse"][0]["hooks"][0]["args"] == {"path": "frontier_gate.py"}


def test_doctor_redacts_command_arguments() -> None:
    redaction_marker = "doctor-secret-must-not-appear"
    env = os.environ.copy()
    env.update({
        "FRONTIER_SESSION_ID": "doctor-command-redaction",
        "FRONTIER_BODY_CMD": f"{sys.executable} --token {redaction_marker}",
        "FRONTIER_ADVISOR_CMD": f"{sys.executable} --api-key {redaction_marker}",
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc
    assert redaction_marker not in proc.stdout
    payload = json.loads(proc.stdout)
    command_rows = [row for row in payload["checks"] if row["component"].endswith("CLI")]
    assert command_rows and all("redacted" in row["detail"] for row in command_rows)


def test_doctor_rejects_state_path_that_is_not_a_directory() -> None:
    state_file = TMP / "state-is-a-file"
    state_file.write_text("not a directory")
    env = os.environ.copy()
    env.update({
        "FRONTIER_STATE_DIR": str(state_file),
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 1, proc
    payload = json.loads(proc.stdout)
    assert payload["ready"] is False
    assert payload["checks"][0]["status"] == "state_unwritable"
    assert payload["checks"][0]["blocking"] is True


def test_doctor_rejects_unusable_session_lock_path() -> None:
    state_dir = TMP / "doctor-lock-state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    sid = "doctor-lock-contract"
    lock_path = state_dir / f".{fc.state_path(sid).name}.lock"
    lock_path.mkdir()
    env = os.environ.copy()
    env.update({
        "FRONTIER_STATE_DIR": str(state_dir),
        "FRONTIER_SESSION_ID": sid,
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 1, proc
    payload = json.loads(proc.stdout)
    assert payload["checks"][0]["status"] == "state_unwritable"


def test_pretool_hook_denies_when_session_lock_is_unusable() -> None:
    sid = "pretool-unusable-lock"
    path = fc.state_path(sid)
    lock_path = fc.config_lock_path(path)
    try:
        fc.write_state(
            sid,
            armed=True,
            approved_gate={"gate": "true", "argv": ["true"], "cwd": str(ROOT)},
        )
        lock_path.unlink(missing_ok=True)
        lock_path.mkdir()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
            input=json.dumps({
                "session_id": sid,
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/blocked", "content": "x"},
            }),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc
        decision = json.loads(proc.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "validated safely" in decision["permissionDecisionReason"]
    finally:
        if lock_path.is_dir():
            lock_path.rmdir()
        fc.clear_state(sid)


def test_pretool_hook_allows_unarmed_session_without_usable_lock() -> None:
    sid = "pretool-unarmed-unusable-lock"
    state_dir = TMP / "pretool-unarmed-lock-state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_name = fc.state_path(sid).name
    lock_path = state_dir / f".{state_name}.lock"
    lock_path.mkdir()
    env = os.environ.copy()
    env["FRONTIER_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "frontier_gate.py")],
        input=json.dumps({
            "session_id": sid,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/allowed", "content": "x"},
        }),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc
    assert proc.stdout == ""


def test_doctor_json_handles_invalid_session_id() -> None:
    env = os.environ.copy()
    env.update({
        "FRONTIER_SESSION_ID": "   ",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 2, proc
    payload = json.loads(proc.stdout)
    assert payload["ready"] is False
    assert payload["checks"][0]["status"] == "invalid_session"
    assert "FRONTIER_SESSION_ID" in payload["checks"][0]["next_step"]


def test_doctor_rejects_unusable_global_lock_path() -> None:
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    lock_path = fc.config_lock_path(fc.GLOBAL_CONFIG)
    lock_path.unlink(missing_ok=True)
    lock_path.mkdir()
    env = os.environ.copy()
    env.update({
        "FRONTIER_SESSION_ID": "doctor-global-lock-contract",
        "FRONTIER_BODY_CMD": sys.executable,
        "FRONTIER_ADVISOR_CMD": sys.executable,
    })
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor", "--json"],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 1, proc
        payload = json.loads(proc.stdout)
        check = next(
            row for row in payload["checks"] if row["component"] == "global configuration"
        )
        assert check["status"] == "lock_unusable"
        assert check["blocking"] is True
        assert check["next_step"]
    finally:
        lock_path.rmdir()


def test_unarmed_stop_ignores_unusable_global_lock_path() -> None:
    sid = "unarmed-stop-global-lock-contract"
    fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})
    fc.write_state(sid, armed=False)
    lock_path = fc.config_lock_path(fc.GLOBAL_CONFIG)
    lock_path.unlink(missing_ok=True)
    lock_path.mkdir()
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "frontier_verify_gate.py")],
            input=json.dumps({"session_id": sid}),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc
    finally:
        lock_path.rmdir()
        fc.clear_state(sid)


def test_manual_hook_commands_refuse_invalid_settings() -> None:
    settings_home = TMP / "invalid-hook-settings"
    settings = settings_home / "settings.json"
    settings_home.mkdir(mode=0o700, exist_ok=True)
    original = "{broken settings"
    settings.write_text(original)
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(settings_home)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_install_hooks(None) == 1
        assert settings.read_text() == original
        assert settings.with_suffix(".json.bak").read_text() == original

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_uninstall_hooks(None) == 1
        assert settings.read_text() == original

        for malformed in ({"hooks": []}, {"hooks": {"PreToolUse": ["bad entry"]}}):
            encoded = json.dumps(malformed)
            settings.write_text(encoded)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                assert dispatch.cmd_install_hooks(None) == 1
            assert settings.read_text() == encoded
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                assert dispatch.cmd_uninstall_hooks(None) == 1
            assert settings.read_text() == encoded
    finally:
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir


def test_config_refuses_invalid_cross_layer_combination_before_write() -> None:
    sid = "cross-layer-config-contract"
    old_sid = dispatch.SESSION_ID
    fc.write_json_owner_only(
        fc.GLOBAL_CONFIG,
        {"schema_version": fc.CONFIG_SCHEMA_VERSION, "fast_effort": "xhigh"},
    )
    args = type("Args", (), {
        "repair": False,
        "glob": False,
        "executor": "grok",
        "model": None,
        "effort": None,
        "fast": "on",
        "profile": None,
        "frontier_provider": None,
        "frontier_model": None,
        "claude_model": None,
        "grok_model": None,
        "gemini_model": None,
        "update_mode": None,
    })()
    try:
        dispatch.SESSION_ID = sid
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_config(args) == 2
        assert fc.read_state(sid)["config"] == {}
        assert fc.resolve_config(session_id=sid)["executor"] == "codex"

        fc.write_state(sid, config={"executor": "grok", "fast": True})
        doctor_args = type("Args", (), {"json": True, "check_updates": False})()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_doctor(doctor_args) == 2
        doctor_payload = json.loads(output.getvalue())
        assert "config --effort high" in doctor_payload["checks"][0]["next_step"]

        args.executor = None
        args.fast = None
        args.effort = "high"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_config(args) == 0
        healed = fc.resolve_config(session_id=sid)
        assert healed["executor"] == "grok"
        assert healed["fast"] is True
        assert healed["fast_effort"] == "high"

        fc.clear_state(sid)
        fc.write_state(sid, config={"executor": "grok", "fast": True})
        args.fast = "off"
        args.effort = None
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_config(args) == 0
        healed = fc.resolve_config(session_id=sid)
        assert healed["executor"] == "grok"
        assert healed["fast"] is False
    finally:
        dispatch.SESSION_ID = old_sid
        fc.clear_state(sid)
        fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})


def _race_config_update(
    sid: str,
    patch: dict,
    global_scope: bool,
    start: Event,
    results: Queue,
    global_config_path: str,
    state_dir: str,
) -> None:
    # Spawned macOS workers re-import this module with a fresh TMP; bind them to the parent files.
    fc.GLOBAL_CONFIG = Path(global_config_path)
    fc.STATE_DIR = Path(state_dir)
    start.wait(timeout=5)
    try:
        fc.update_config_transaction(sid, patch, global_scope=global_scope)
    except ValueError:
        results.put("refused")
    else:
        results.put("written")


def test_layered_config_writes_are_one_validated_transaction() -> None:
    sid = "config-transaction-race"
    context = get_context("spawn")
    start = context.Event()
    results: Queue = context.Queue()
    fc.write_json_owner_only(
        fc.GLOBAL_CONFIG,
        {"schema_version": fc.CONFIG_SCHEMA_VERSION, "fast_effort": "xhigh"},
    )
    first = context.Process(
        target=_race_config_update,
        args=(
            sid,
            {"executor": "grok"},
            True,
            start,
            results,
            str(fc.GLOBAL_CONFIG),
            str(fc.STATE_DIR),
        ),
    )
    second = context.Process(
        target=_race_config_update,
        args=(
            sid,
            {"fast": True},
            False,
            start,
            results,
            str(fc.GLOBAL_CONFIG),
            str(fc.STATE_DIR),
        ),
    )
    try:
        first.start()
        second.start()
        start.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0
        assert second.exitcode == 0
        assert sorted((results.get(timeout=2), results.get(timeout=2))) == ["refused", "written"]
        fc.resolve_config(session_id=sid)
    finally:
        if first.is_alive():
            first.terminate()
        if second.is_alive():
            second.terminate()
        fc.clear_state(sid)
        fc.write_json_owner_only(fc.GLOBAL_CONFIG, {"schema_version": fc.CONFIG_SCHEMA_VERSION})


def test_model_override_refuses_invalid_inherited_executor_cleanly() -> None:
    old_executor = os.environ.get("FRONTIER_EXECUTOR")
    old_sid = dispatch.SESSION_ID
    args = type("Args", (), {
        "repair": False,
        "glob": False,
        "executor": None,
        "model": "custom-model",
        "effort": None,
        "fast": None,
        "profile": None,
        "frontier_provider": None,
        "frontier_model": None,
        "claude_model": None,
        "grok_model": None,
        "gemini_model": None,
        "update_mode": None,
    })()
    try:
        os.environ["FRONTIER_EXECUTOR"] = "shell"
        dispatch.SESSION_ID = "invalid-inherited-executor"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_config(args) == 2
        dispatch_args = type("Args", (), {
            "fast": None,
            "executor": None,
            "model": "custom-model",
            "effort": None,
            "profile": None,
            "frontier_provider": None,
            "frontier_model": None,
            "claude_model": None,
            "grok_model": None,
            "gemini_model": None,
        })()
        try:
            dispatch._overrides(dispatch_args)
        except ValueError as exc:
            assert "unknown executor" in str(exc)
        else:
            raise AssertionError("dispatch model override must reject an invalid executor")
    finally:
        dispatch.SESSION_ID = old_sid
        if old_executor is None:
            os.environ.pop("FRONTIER_EXECUTOR", None)
        else:
            os.environ["FRONTIER_EXECUTOR"] = old_executor


def test_cli_refuses_os_errors_without_traceback() -> None:
    original = dispatch.cmd_verify

    def disk_fault(_args) -> int:
        raise OSError("receipt cleanup failed")

    try:
        dispatch.cmd_verify = disk_fault
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            assert dispatch.main(["verify", "--gate", "true"]) == 2
        message = stderr.getvalue()
        assert "frontier-dispatch refused" in message
        assert "receipt cleanup failed" in message
        assert "Traceback" not in message
    finally:
        dispatch.cmd_verify = original


def test_effort_refuses_unsupported_executor_without_silent_noop() -> None:
    old_sid = dispatch.SESSION_ID
    args = type("Args", (), {
        "repair": False,
        "glob": False,
        "executor": "gemini",
        "model": None,
        "effort": "high",
        "fast": "off",
        "profile": None,
        "frontier_provider": None,
        "frontier_model": None,
        "claude_model": None,
        "grok_model": None,
        "gemini_model": None,
        "update_mode": None,
    })()
    try:
        dispatch.SESSION_ID = "unsupported-effort"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert dispatch.cmd_config(args) == 2
        override_args = type("Args", (), {
            "fast": "off",
            "executor": "claude",
            "model": None,
            "effort": "high",
            "profile": None,
            "frontier_provider": None,
            "frontier_model": None,
            "claude_model": None,
            "grok_model": None,
            "gemini_model": None,
        })()
        try:
            dispatch._overrides(override_args)
        except ValueError as exc:
            assert "not supported" in str(exc)
        else:
            raise AssertionError("unsupported executor effort must be refused")
        override_args.fast = "on"
        try:
            dispatch._overrides(override_args)
        except ValueError as exc:
            assert "not supported" in str(exc)
        else:
            raise AssertionError("unsupported fast executor effort must be refused")
    finally:
        dispatch.SESSION_ID = old_sid
        fc.clear_state("unsupported-effort")


def test_dry_run_cards_use_the_handoff_schema() -> None:
    env = os.environ.copy()
    env.update({
        "FRONTIER_CONFIG_DIR": str(TMP / "dry-config"),
        "FRONTIER_STATE_DIR": str(TMP / "dry-state"),
        "FRONTIER_BODY_CMD": "echo",
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "frontier_dispatch.py"), "--dry-run", "schema check"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc
    card = json.loads(proc.stdout)["cards"][0]
    assert card["schema_version"] == fc.HANDOFF_SCHEMA_VERSION


def main() -> int:
    tests = [
        test_corrupt_config_fails_closed_without_overwrite,
        test_explicit_repair_backs_up_and_restores_config,
        test_repair_keeps_authoritative_path_present_until_atomic_replace,
        test_corrupt_session_state_fails_closed_and_repairs,
        test_session_repair_preserves_safe_receipt_cleanup_identity,
        test_session_repair_recovers_legacy_approved_workspace_receipt,
        test_session_repair_discards_nonfinite_receipt_identity,
        test_session_repair_falls_back_to_valid_receipt_identity,
        test_session_repair_skips_partial_receipt_identity,
        test_session_repair_warns_to_rearm_and_reverify,
        test_legacy_session_path_fails_closed_until_explicit_repair,
        test_schema_versions_are_written,
        test_invalid_persisted_values_fail_closed,
        test_persisted_approved_gate_requires_exact_argv_binding,
        test_advisory_lock_serializes_writers,
        test_run_ids_are_unique_within_one_long_lived_process,
        test_noncanonical_session_ids_have_distinct_state_paths,
        test_hidden_dispatch_finish_invalidates_green_authority,
        test_state_write_refuses_oversized_replacement_atomically,
        test_global_config_write_refuses_oversized_replacement_atomically,
        test_oversized_config_repair_preserves_exact_backup,
        test_doctor_json_has_typed_actionable_states,
        test_doctor_redacts_invalid_schema_version_values,
        test_doctor_classifies_config_fifo_as_special_file,
        test_dangling_state_symlink_fails_closed_as_special_file,
        test_corrupt_state_makes_hooks_fail_closed,
        test_hooks_fail_closed_on_malformed_or_non_object_input,
        test_hooks_fail_closed_on_invalid_utf8_state,
        test_hooks_fail_closed_on_surrogate_session_id,
        test_nonfinite_state_and_verdict_timestamps_fail_closed,
        test_persisted_cleanup_paths_fail_closed_and_repair,
        test_nested_nonfinite_state_is_repairable,
        test_extreme_and_deep_state_json_fail_closed,
        test_stop_hook_refuses_while_dispatch_is_active,
        test_stop_hook_revalidates_state_after_snapshot,
        test_done_compare_and_set_refuses_concurrent_dispatch,
        test_done_compare_and_set_refuses_any_concurrent_state_change,
        test_verification_start_refuses_closed_completion,
        test_doctor_tolerates_unreadable_claude_settings,
        test_doctor_rejects_settings_fifo_without_blocking,
        test_doctor_honors_custom_claude_config_directory,
        test_doctor_rejects_dangling_symlink_in_writable_parent_path,
        test_doctor_accepts_writable_path_beneath_valid_symlinked_parent,
        test_doctor_gives_access_recovery_for_unreadable_persistence,
        test_doctor_classifies_malformed_command_overrides,
        test_doctor_rejects_malformed_claude_hook_structure,
        test_doctor_and_installer_reject_ineffective_hook_matchers,
        test_hook_matcher_coverage_matches_claude_semantics,
        test_registration_surfaces_use_exec_form_args_not_shell,
        test_registration_surfaces_and_installer_align_all_tools_matcher,
        test_install_hooks_exec_form_preserves_metacharacters_as_one_arg,
        test_upgrade_removes_all_legacy_shell_and_exec_variants,
        test_uninstall_removes_old_and_new_hook_variants,
        test_malformed_hook_args_fail_closed,
        test_doctor_redacts_command_arguments,
        test_doctor_rejects_state_path_that_is_not_a_directory,
        test_doctor_rejects_unusable_session_lock_path,
        test_pretool_hook_denies_when_session_lock_is_unusable,
        test_pretool_hook_allows_unarmed_session_without_usable_lock,
        test_doctor_json_handles_invalid_session_id,
        test_doctor_rejects_unusable_global_lock_path,
        test_unarmed_stop_ignores_unusable_global_lock_path,
        test_manual_hook_commands_refuse_invalid_settings,
        test_config_refuses_invalid_cross_layer_combination_before_write,
        test_layered_config_writes_are_one_validated_transaction,
        test_model_override_refuses_invalid_inherited_executor_cleanly,
        test_cli_refuses_os_errors_without_traceback,
        test_effort_refuses_unsupported_executor_without_silent_noop,
        test_dry_run_cards_use_the_handoff_schema,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS config reliability contracts ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
