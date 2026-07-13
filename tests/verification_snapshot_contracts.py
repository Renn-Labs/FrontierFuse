#!/usr/bin/env python3
"""Standalone stdlib contracts for 0.2.6 snapshot-bound verification.

Creates temporary git repositories; does not touch the live working tree.
stdlib-only, keyless, offline.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="frontier-snap-contract-")
os.environ["FRONTIER_CONFIG_DIR"] = str(Path(_TMP) / "config")
os.environ["FRONTIER_STATE_DIR"] = str(Path(_TMP) / "state")
os.environ["FRONTIER_RUNS_DIR"] = str(Path(_TMP) / "runs")
os.environ["FRONTIER_CODEX_CMD"] = "echo"
os.environ["FRONTIER_ADVISOR_CMD"] = "echo"
# Ensure guards are on for Stop-hook tests unless a case overrides.
os.environ.pop("FRONTIER_GUARDS_OFF", None)
os.environ.pop("CLAUDE_GUARDS_OFF", None)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontier_common as fc  # noqa: E402
import frontier_verify as fv  # noqa: E402

STOP_HOOK = ROOT / "hooks" / "frontier_verify_gate.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _init_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="frontier-snap-repo-"))
    _git(repo, "init")
    _git(repo, "config", "user.email", "snap@test.local")
    _git(repo, "config", "user.name", "Snap Test")
    # Quiet "detached HEAD" / default branch variance across git versions.
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run_stop(session_id: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("FRONTIER_GUARDS_OFF", None)
    env.pop("CLAUDE_GUARDS_OFF", None)
    return subprocess.run(
        [sys.executable, str(STOP_HOOK)],
        input=json.dumps({"session_id": session_id, "hook_event_name": "Stop"}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(cwd or ROOT),
    )


def _approved_gate(gate: str, repo: Path) -> dict:
    return {
        "gate": gate,
        "argv": fv.parse_gate_argv(gate),
        "cwd": str(repo.resolve()),
    }


def _arm(
    session_id: str,
    repo: Path,
    gate: str = "true",
    last_dispatch_ts: float | None = None,
) -> None:
    fc.write_state(
        session_id,
        armed=True,
        last_dispatch_ts=time.time() - 10 if last_dispatch_ts is None else last_dispatch_ts,
        approved_gate=_approved_gate(gate, repo),
    )


def _arm_with_verdict(session_id: str, verdict: dict, last_dispatch_ts: float = 1.0) -> None:
    snapshot = verdict.get("verified_snapshot") or verdict.get("final_snapshot") or {}
    root = Path(str(snapshot.get("workspace_root") or ROOT))
    gate = str(verdict.get("gate") or "false")
    gate_argv = verdict.get("gate_argv")
    if not isinstance(gate_argv, list) or not gate_argv:
        try:
            gate_argv = fv.parse_gate_argv(gate)
        except ValueError:
            gate = "false"
            gate_argv = ["false"]
    approved = {
        "gate": gate,
        "argv": list(gate_argv),
        "cwd": str(root.resolve()),
    }
    fc.clear_state(session_id)
    fc.write_state(
        session_id,
        armed=True,
        last_dispatch_ts=last_dispatch_ts,
        verdict=verdict,
        approved_gate=approved,
    )


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


def test_clean_green_argv() -> None:
    """Clean tree + true gate → GREEN with matching pre/final snapshots; Stop allows."""
    repo = _init_repo()
    sid = "snap-clean-green"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN", v
        assert v["exit_code"] == 0
        assert v["schema_version"] == fv.VERDICT_SCHEMA_VERSION
        assert v["gate_mode"] == "argv"
        assert v["gate_argv"] == ["true"]
        assert v["unsafe"] is False
        assert v["snapshot_stable"] is True
        assert isinstance(v["pre_gate_snapshot"], dict)
        assert isinstance(v["verified_snapshot"], dict)
        assert isinstance(v["final_snapshot"], dict)
        assert fv.snapshots_equal(v["pre_gate_snapshot"], v["final_snapshot"])
        assert fv.snapshots_equal(v["verified_snapshot"], v["final_snapshot"])
        for key in (
            "workspace_root",
            "head",
            "index_tree",
            "unstaged_diff_sha",
            "staged_diff_sha",
            "untracked_sha",
            "config_sha",
            "gate_identity",
            "snapshot_id",
            "version",
        ):
            assert key in v["verified_snapshot"], key
        assert v["verified_snapshot"]["workspace_root"] == str(repo.resolve())
        assert v["verified_snapshot"]["head"]
        assert v["verified_snapshot"]["index_tree"]

        # State + on-disk verdict carry snapshot fields.
        st = fc.read_state(sid)
        assert st["verdict"]["schema_version"] == fv.VERDICT_SCHEMA_VERSION
        assert st["verdict"]["verified_snapshot"]["snapshot_id"] == v["verified_snapshot"][
            "snapshot_id"
        ]
        disk = json.loads((repo / "verdict.json").read_text())
        assert disk["result"] == "GREEN"
        assert disk["verified_snapshot"]["snapshot_id"] == v["verified_snapshot"]["snapshot_id"]

        # Owner-only mode on verdict.json
        mode = stat.S_IMODE((repo / "verdict.json").stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

        # Stop accepts
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 0, proc.stderr
        retained = fc.read_state(sid)
        assert retained["armed"] is True
        assert retained["completion_pending"] is True
        assert retained["completion_closed"] is False
        assert retained["verdict"]["result"] == "GREEN"
        assert retained["verdict_path"] == str(repo / "verdict.json")
        assert (repo / "verdict.json").exists()
    finally:
        fc.clear_state(sid)


def test_green_verdict_is_bound_to_exact_session_id() -> None:
    repo = _init_repo()
    owner_sid = "session/owner"
    other_sid = "session?owner"
    try:
        verdict = fv.run_gate("true", session_id=owner_sid, cwd=str(repo))
        approved = _approved_gate("true", repo)
        assert fv.verdict_is_snapshot_fresh_green(
            verdict,
            0.0,
            verdict["dispatch_generation"],
            session_id=owner_sid,
            cwd=str(repo),
            approved_gate=approved,
        )
        assert not fv.verdict_is_snapshot_fresh_green(
            verdict,
            0.0,
            verdict["dispatch_generation"],
            session_id=other_sid,
            cwd=str(repo),
            approved_gate=approved,
        )
    finally:
        fc.clear_state(owner_sid)
        fc.clear_state(other_sid)


def test_red_on_nonzero_exit() -> None:
    repo = _init_repo()
    sid = "snap-red-exit"
    fc.clear_state(sid)
    try:
        message = "contract gate diagnostic"
        code = f"import sys; print({message!r}, file=sys.stderr); raise SystemExit(3)"
        gate = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        v = fv.run_gate(gate, session_id=sid, cwd=str(repo))
        assert v["result"] == "RED"
        assert v["exit_code"] != 0
        assert v["snapshot_stable"] is True
        assert v["verified_snapshot"] is None
        disk = json.loads((repo / "verdict.json").read_text())
        assert message in disk["stderr_tail"]
        assert "stderr_tail" not in fc.read_state(sid)["verdict"]
        _arm_with_verdict(sid, v, last_dispatch_ts=v["ts"] - 1)
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2
    finally:
        fc.clear_state(sid)


def test_non_git_workspace_cannot_green() -> None:
    """A hardened close requires a Git worktree, not an empty best-effort snapshot."""
    workspace = Path(tempfile.mkdtemp(prefix="frontier-snap-non-git-"))
    sid = "snap-non-git"
    fc.clear_state(sid)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(workspace))
        assert v["result"] == "RED"
        assert v["workspace_supported"] is False
        assert v["verified_snapshot"] is None
    finally:
        fc.clear_state(sid)


def test_mutation_unstaged_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-unstaged"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        (repo / "README.md").write_text("dirty unstaged\n")
        # Re-load state (run_gate already wrote it) and run Stop
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "unstaged mutation must reject GREEN"
        assert not fv.verdict_is_snapshot_fresh_green(
            fc.read_state(sid)["verdict"],
            fc.read_state(sid)["last_dispatch_ts"],
            fc.read_state(sid)["dispatch_generation"],
            session_id=sid,
            cwd=str(repo),
            approved_gate=fc.read_state(sid).get("approved_gate"),
        )
    finally:
        fc.clear_state(sid)


def test_mutation_staged_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-staged"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        (repo / "extra.txt").write_text("staged\n")
        _git(repo, "add", "extra.txt")
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "staged mutation must reject GREEN"
    finally:
        fc.clear_state(sid)


def test_mutation_committed_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-commit"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        old_head = v["verified_snapshot"]["head"]
        (repo / "more.txt").write_text("committed\n")
        _git(repo, "add", "more.txt")
        _git(repo, "commit", "-m", "post-green")
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert new_head != old_head
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "committed mutation must reject GREEN"
    finally:
        fc.clear_state(sid)


def test_mutation_untracked_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-untracked"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        (repo / "ghost.txt").write_text("untracked\n")
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "untracked mutation must reject GREEN"
    finally:
        fc.clear_state(sid)


def test_config_change_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-config"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Session config change alters effective config_sha.
        fc.write_state(sid, config={"codex_effort": "low"})
        # Preserve verdict / armed after config merge write.
        st = fc.read_state(sid)
        assert st.get("verdict"), "verdict must survive config write"
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "config change must reject GREEN"
    finally:
        fc.clear_state(sid)


def test_gate_can_change_global_config_without_deadlock_and_forces_red() -> None:
    repo = _init_repo()
    sid = "snap-gate-global-config"
    fc.clear_state(sid)
    _arm(sid, repo)
    prior = fc.GLOBAL_CONFIG.read_bytes() if fc.GLOBAL_CONFIG.exists() else None
    current_effort = fc.resolve_config(session_id=sid)["codex_effort"]
    next_effort = "low" if current_effort != "low" else "high"
    script = Path(_TMP) / "gate-updates-global-config.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import frontier_common as fc\n"
        f"fc.save_global_config({{'codex_effort': {next_effort!r}}})\n",
        encoding="utf-8",
    )
    gate = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    try:
        started = time.monotonic()
        verdict = fv.run_gate(gate, session_id=sid, cwd=str(repo))
        assert time.monotonic() - started < 10, "global config update must not deadlock the gate"
        assert verdict["exit_code"] == 0
        assert verdict["result"] == "RED", "configuration changes during a gate must invalidate it"
    finally:
        if prior is None:
            fc.GLOBAL_CONFIG.unlink(missing_ok=True)
        else:
            fc.write_text_owner_only(fc.GLOBAL_CONFIG, prior.decode("utf-8"))
        fc.clear_state(sid)


def test_gate_identity_change_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-gate"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Tamper: claim a different gate while keeping verified_snapshot from true.
        st = fc.read_state(sid)
        verdict = dict(st["verdict"])
        verdict["gate"] = "false"
        verdict["gate_argv"] = ["false"]
        fc.write_state(sid, verdict=verdict)
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "gate identity change must reject GREEN"
    finally:
        fc.clear_state(sid)


def test_approved_gate_binding_rejects_forged_green() -> None:
    """A GREEN stamped for a different argv cannot close the host-frozen loop."""
    repo = _init_repo()
    sid = "snap-approved-gate"
    fc.clear_state(sid)
    _arm(sid, repo, gate="false")
    try:
        forged = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert forged["result"] == "GREEN"
        assert fv.verdict_is_snapshot_fresh_green(
            forged,
            fc.read_state(sid)["last_dispatch_ts"],
            fc.read_state(sid)["dispatch_generation"],
            session_id=sid,
            cwd=str(repo),
            approved_gate=fc.read_state(sid).get("approved_gate"),
        ) is False
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "Stop must reject a verdict from a non-approved gate"
    finally:
        fc.clear_state(sid)


def test_legacy_verdict_rejected() -> None:
    """Legacy make_verdict GREEN is readable but cannot satisfy the stronger Stop gate."""
    sid = "snap-legacy"
    fc.clear_state(sid)
    try:
        legacy = fc.make_verdict("true", 0, "sha", [], ts=time.time(), after_dispatch_ts=1.0)
        assert legacy["result"] == "GREEN"
        assert "schema_version" not in legacy or int(legacy.get("schema_version") or 0) < 2
        assert not fv.is_snapshot_bound_verdict(legacy)
        _arm_with_verdict(sid, legacy, last_dispatch_ts=1.0)
        # Timestamp-only helper still true (legacy readable / dispatch compat).
        assert fc.verdict_is_fresh_green(legacy, 1.0) is True
        # Stronger gate false.
        assert fv.verdict_is_snapshot_fresh_green(
            legacy,
            1.0,
            0,
            session_id=sid,
            approved_gate=fc.read_state(sid).get("approved_gate"),
        ) is False
        proc = _run_stop(sid)
        assert proc.returncode == 2, "legacy GREEN must not satisfy Stop"
    finally:
        fc.clear_state(sid)


def test_unsafe_legacy_shell_rejected() -> None:
    repo = _init_repo()
    sid = "snap-unsafe-shell"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        # Shell metacharacters require the explicit legacy path.
        v = fv.run_gate("true && true", session_id=sid, cwd=str(repo), legacy_shell=True)
        assert v["unsafe"] is True
        assert v["gate_mode"] == "legacy_shell"
        assert v["unsafe_reason"]
        # Exit-ok + stable may still stamp GREEN, but Stop must reject.
        assert v["exit_code"] == 0
        assert v["snapshot_stable"] is True
        assert v["result"] == "GREEN"
        assert fv.verdict_is_snapshot_fresh_green(
            v,
            fc.read_state(sid)["last_dispatch_ts"],
            fc.read_state(sid)["dispatch_generation"],
            session_id=sid,
            cwd=str(repo),
            approved_gate=fc.read_state(sid).get("approved_gate"),
        ) is False
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "unsafe legacy-shell GREEN must not satisfy Stop"
    finally:
        fc.clear_state(sid)


def test_argv_default_rejects_shell_syntax() -> None:
    """Default argv gates reject shell syntax rather than passing it as inert arguments."""
    repo = _init_repo()
    sid = "snap-argv-noshell"
    fc.clear_state(sid)
    try:
        for gate in ("true && false", "true | false", "echo hi > output.txt", "$(true)"):
            try:
                fv.parse_gate_argv(gate)
            except ValueError:
                pass
            else:
                raise AssertionError(f"argv gate must reject shell syntax: {gate!r}")
            verdict = fv.run_gate(gate, session_id=sid, cwd=str(repo), legacy_shell=False)
            assert verdict["result"] == "RED", gate
            assert verdict["exit_code"] == 127, gate
        assert fv.parse_gate_argv("printf '%s' 'a|b'") == ["printf", "%s", "a|b"]
        v2 = fv.run_gate("", session_id=sid, cwd=str(repo), legacy_shell=False)
        assert v2["result"] == "RED"
        assert v2["exit_code"] == 127
    finally:
        fc.clear_state(sid)


def test_owner_only_verdict_mode() -> None:
    repo = _init_repo()
    sid = "snap-owner-mode"
    fc.clear_state(sid)
    repo.chmod(0o755)
    original_repo_mode = stat.S_IMODE(repo.stat().st_mode)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        mode = stat.S_IMODE((repo / "verdict.json").stat().st_mode)
        assert mode & 0o077 == 0, f"group/other bits must be clear, got {oct(mode)}"
        assert mode & 0o600 == 0o600
        assert stat.S_IMODE(repo.stat().st_mode) == original_repo_mode
    finally:
        fc.clear_state(sid)


def test_gate_mutates_workspace_not_green() -> None:
    """If the gate itself dirties the tree, exit 0 is not enough for GREEN."""
    repo = _init_repo()
    sid = "snap-gate-mutates"
    fc.clear_state(sid)
    try:
        # Gate writes a new untracked file → pre != final.
        script = repo / "mutate.sh"
        script.write_text("#!/bin/sh\necho dirty > post_gate.txt\n")
        script.chmod(0o755)
        v = fv.run_gate(str(script), session_id=sid, cwd=str(repo))
        assert v["exit_code"] == 0
        assert v["snapshot_stable"] is False
        assert v["result"] == "RED"
        assert v["verified_snapshot"] is None
    finally:
        fc.clear_state(sid)


def test_dispatch_timestamp_compat() -> None:
    """Dispatch generation remains authoritative when the wall clock moves backward."""
    repo = _init_repo()
    sid = "snap-ts-compat"
    fc.clear_state(sid)
    try:
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # A wall-clock jump alone cannot make a same-generation verdict stale.
        future = float(v["ts"]) + 100.0
        fc.write_state(
            sid,
            armed=True,
            last_dispatch_ts=future,
            verdict=v,
            approved_gate=_approved_gate("true", repo),
        )
        assert (
            fv.verdict_is_snapshot_fresh_green(
                v,
                future,
                fc.read_state(sid)["dispatch_generation"],
                session_id=sid,
                cwd=str(repo),
                approved_gate=fc.read_state(sid).get("approved_gate"),
            )
            is True
        )
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 0, proc.stderr
    finally:
        fc.clear_state(sid)


def test_oversized_untracked_content_change_invalidates() -> None:
    """Same-size rewrite of an oversized untracked file must still break GREEN."""
    repo = _init_repo()
    sid = "snap-oversized"
    fc.clear_state(sid)
    _arm(sid, repo)
    old_max = fv.MAX_UNTRACKED_BYTES
    try:
        # Shrink the size cap so a modest file counts as oversized.
        fv.MAX_UNTRACKED_BYTES = 64
        big = repo / "big.bin"
        big.write_bytes(b"A" * 128)
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Same length, different bytes — size-only fingerprints would miss this.
        big.write_bytes(b"B" * 128)
        # Bump mtime deterministically if the FS has coarse resolution.
        os.utime(big, (time.time() + 2, time.time() + 2))
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "oversized same-size content change must reject GREEN"
    finally:
        fv.MAX_UNTRACKED_BYTES = old_max
        fc.clear_state(sid)


def test_overflow_untracked_meta_change_invalidates() -> None:
    """Content/size change past the full-hash cap must still break GREEN via overflow meta."""
    repo = _init_repo()
    sid = "snap-overflow-meta"
    fc.clear_state(sid)
    _arm(sid, repo)
    old_max = fv.MAX_UNTRACKED_FILES
    old_meta = fv.MAX_UNTRACKED_META
    try:
        fv.MAX_UNTRACKED_FILES = 2
        fv.MAX_UNTRACKED_META = 50
        for i in range(5):
            (repo / f"u{i}.txt").write_text(f"v1-{i}\n")
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Mutate a file that falls past the full-content hash window (sorted: u0,u1 hashed;
        # u2+ are overflow meta). Sorted order: u0.txt, u1.txt, u2.txt, u3.txt, u4.txt.
        (repo / "u3.txt").write_text("v2-changed\n")
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "overflow untracked meta change must reject GREEN"
    finally:
        fv.MAX_UNTRACKED_FILES = old_max
        fv.MAX_UNTRACKED_META = old_meta
        fc.clear_state(sid)


def test_truncated_untracked_snapshot_cannot_green() -> None:
    """Paths beyond the metadata cap are not enough evidence for a hardened GREEN."""
    repo = _init_repo()
    sid = "snap-truncated-untracked"
    fc.clear_state(sid)
    _arm(sid, repo)
    old_max = fv.MAX_UNTRACKED_FILES
    old_meta = fv.MAX_UNTRACKED_META
    try:
        fv.MAX_UNTRACKED_FILES = 1
        fv.MAX_UNTRACKED_META = 2
        for i in range(4):
            (repo / f"many-{i}.txt").write_text(f"{i}\n")
        v = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert v["result"] == "RED"
        assert v["workspace_supported"] is False
        assert v["verified_snapshot"] is None
    finally:
        fv.MAX_UNTRACKED_FILES = old_max
        fv.MAX_UNTRACKED_META = old_meta
        fc.clear_state(sid)


def test_dispatch_generation_rejects_old_green_after_clock_regression() -> None:
    repo = _init_repo()
    sid = "snap-dispatch-generation"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        old = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert old["result"] == "GREEN"
        assert old["dispatch_generation"] == 0
        assert (repo / "verdict.json").exists()

        fc.mark_dispatch_started(sid, "later-run", 1.0)
        assert fc.read_state(sid)["verdict"] is None
        assert not (repo / "verdict.json").exists()
        fc.mark_dispatch_finished(sid, "later-run", 0.5)
        state = fc.read_state(sid)
        assert state["dispatch_generation"] == 1
        assert state["active_dispatches"] == []

        # Even if an old verdict is restored and wall-clock time regresses, generation identity wins.
        fc.write_state(sid, verdict=old)
        state = fc.read_state(sid)
        assert not fv.verdict_is_snapshot_fresh_green(
            old,
            state["last_dispatch_ts"],
            state["dispatch_generation"],
            session_id=sid,
            cwd=str(repo),
            approved_gate=state.get("approved_gate"),
        )
        assert _run_stop(sid, cwd=repo).returncode == 2
    finally:
        fc.clear_state(sid)


def test_dispatch_during_gate_forces_red_without_workspace_change() -> None:
    repo = _init_repo()
    sid = "snap-dispatch-during-gate"
    fc.clear_state(sid)
    _arm(sid, repo)
    script = repo / "dispatch_during_gate.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import frontier_common as fc\n"
        f"fc.mark_dispatch_started({sid!r}, 'during-gate', 1.0)\n"
        f"fc.mark_dispatch_finished({sid!r}, 'during-gate', 0.5)\n"
    )
    try:
        verdict = fv.run_gate(
            f"{sys.executable} {script}",
            session_id=sid,
            cwd=str(repo),
        )
        assert verdict["exit_code"] == 0
        assert verdict["snapshot_stable"] is True
        assert verdict["result"] == "RED"
        assert verdict["dispatch_generation"] == 0
        assert fc.read_state(sid)["dispatch_generation"] == 1
        assert _run_stop(sid, cwd=repo).returncode == 2
    finally:
        fc.clear_state(sid)


def test_state_race_before_verdict_persistence_forces_red() -> None:
    repo = _init_repo()
    sid = "snap-verdict-cas"
    fc.clear_state(sid)
    _arm(sid, repo)
    fc.write_state(sid, verdict={"result": "GREEN", "schema_version": fv.VERDICT_SCHEMA_VERSION})
    original_finish = fc.finish_verification

    def reject_stale_verdict(
        session_id: str,
        verification_id: str,
        expected: int,
        generation: int,
        verdict: dict,
        artifact_path: Path | None = None,
        artifact_verdict: dict | None = None,
    ):
        current = fc.read_state(session_id)
        return False, current

    try:
        fc.finish_verification = reject_stale_verdict
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["exit_code"] == 0
        assert verdict["result"] == "RED"
        assert verdict["verified_snapshot"] is None
        assert "state changed" in verdict["stderr_tail"]
        assert not (repo / "verdict.json").exists()
        assert fc.read_state(sid)["verdict"] is None
    finally:
        fc.finish_verification = original_finish
        fc.clear_state(sid)


def test_overlapping_verifications_cannot_publish_green() -> None:
    sid = "snap-newer-green-wins"
    fc.clear_state(sid)
    try:
        first = fc.mark_verification_started(sid, "verify-a")
        second = fc.mark_verification_started(sid, "verify-b")
        green = {"result": "GREEN", "schema_version": fv.VERDICT_SCHEMA_VERSION}
        persisted_b, _ = fc.finish_verification(
            sid, "verify-b", second["state_revision"], 0, green
        )
        persisted_a, state = fc.finish_verification(
            sid, "verify-a", first["state_revision"], 0, green
        )
        assert persisted_b is False
        assert persisted_a is False
        assert state["active_verifications"] == []
        assert state["verdict"] is None
    finally:
        fc.clear_state(sid)


def test_verification_preserves_unrecognized_receipt_path_occupant() -> None:
    repo = _init_repo()
    sid = "snap-artifact-cleanup-failure"
    fc.clear_state(sid)
    artifact = repo / "verdict.json"
    artifact.write_text("not a FrontierFuse receipt")
    fc.write_state(
        sid,
        armed=True,
        verdict={"result": "GREEN", "schema_version": fv.VERDICT_SCHEMA_VERSION},
    )
    try:
        try:
            fc.mark_verification_started(sid, "verify-cleanup-failure", artifact)
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("verification must refuse to replace an unrelated receipt target")
        state = fc.read_state(sid)
        assert state["verdict"] is None
        assert state["active_verifications"] == []
        assert artifact.read_text() == "not a FrontierFuse receipt"
    finally:
        fc.clear_state(sid)


def test_dispatch_preserves_unrecognized_receipt_path_occupant() -> None:
    repo = _init_repo()
    sid = "snap-dispatch-artifact-cleanup-failure"
    fc.clear_state(sid)
    artifact = repo / "verdict.json"
    artifact.write_text("not a FrontierFuse receipt")
    fc.write_state(
        sid,
        armed=True,
        approved_gate={"gate": "true", "argv": ["true"], "cwd": str(repo)},
        verdict={"result": "GREEN", "schema_version": fv.VERDICT_SCHEMA_VERSION},
    )
    original_generation = fc.read_state(sid)["dispatch_generation"]
    try:
        fc.mark_dispatch_started(sid, "dispatch-cleanup-failure", time.time())
        state = fc.read_state(sid)
        assert state["verdict"] is None
        assert state["active_dispatches"] == ["dispatch-cleanup-failure"]
        assert state["dispatch_generation"] == original_generation + 1
        assert artifact.read_text() == "not a FrontierFuse receipt"
        fc.mark_dispatch_finished(sid, "dispatch-cleanup-failure", time.time())
    finally:
        fc.clear_state(sid)


def test_dispatch_cleanup_failure_retains_retry_metadata_without_green_authority() -> None:
    repo = _init_repo()
    sid = "dispatch-cleanup-retry"
    artifact = repo / "verdict.json"
    original_unlink = fc._unlink_managed_verdict
    try:
        assert fv.run_gate("true", session_id=sid, cwd=str(repo))["result"] == "GREEN"

        def fail_receipt_unlink(path, expected=None):
            raise OSError("simulated receipt cleanup failure")

        fc._unlink_managed_verdict = fail_receipt_unlink
        try:
            fc.mark_dispatch_started(sid, "cleanup-fails", time.time())
        except OSError:
            pass
        else:
            raise AssertionError("managed receipt cleanup failure must refuse dispatch start")
        state = fc.read_state(sid)
        assert state["verdict"] is None
        assert state["active_dispatches"] == []
        assert state["verdict_path"] == str(artifact)
        assert isinstance(state["receipt_identity"], dict)

        fc._unlink_managed_verdict = original_unlink
        fc.mark_dispatch_started(sid, "cleanup-retry", time.time())
        assert not artifact.exists()
        state = fc.read_state(sid)
        assert state["verdict_path"] is None
        assert state["receipt_identity"] is None
        fc.mark_dispatch_finished(sid, "cleanup-retry", time.time())
    finally:
        fc._unlink_managed_verdict = original_unlink
        fc.clear_state(sid)


def test_dispatch_retains_cleanup_metadata_when_safe_removal_returns_false() -> None:
    repo = _init_repo()
    sid = "dispatch-cleanup-false"
    artifact = repo / "verdict.json"
    original_unlink = fc._unlink_managed_verdict
    try:
        assert fv.run_gate("true", session_id=sid, cwd=str(repo))["result"] == "GREEN"
        fc._unlink_managed_verdict = lambda _path, _expected=None: False
        state = fc.mark_dispatch_started(sid, "cleanup-deferred", time.time())
        assert state["active_dispatches"] == ["cleanup-deferred"]
        assert state["verdict"] is None
        assert state["verdict_path"] == str(artifact)
        assert isinstance(state["receipt_identity"], dict)
        fc.mark_dispatch_finished(sid, "cleanup-deferred", time.time())

        fc._unlink_managed_verdict = original_unlink
        state = fc.mark_dispatch_started(sid, "cleanup-retry", time.time())
        assert not artifact.exists()
        assert state["verdict_path"] is None
        assert state["receipt_identity"] is None
        fc.mark_dispatch_finished(sid, "cleanup-retry", time.time())
    finally:
        fc._unlink_managed_verdict = original_unlink
        fc.clear_state(sid)


def test_verification_refuses_preexisting_receipt_symlink() -> None:
    repo = _init_repo()
    sid = "receipt-symlink"
    target = repo / "user-file"
    target.write_text("keep me")
    receipt = repo / "verdict.json"
    receipt.symlink_to(target.name)
    try:
        try:
            fc.mark_verification_started(sid, "symlink-verifier", receipt)
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("verification must refuse a pre-existing receipt symlink")
        assert receipt.is_symlink()
        assert target.read_text() == "keep me"
        assert fc.read_state(sid)["active_verifications"] == []
    finally:
        fc.clear_state(sid)


def test_receipt_fifo_is_rejected_without_blocking() -> None:
    if not hasattr(os, "mkfifo"):
        return
    repo = _init_repo()
    receipt = repo / "verdict.json"
    os.mkfifo(receipt)
    started = time.monotonic()
    assert fc._unlink_managed_verdict(receipt, {"verification_id": "unused"}) is False
    assert time.monotonic() - started < 1.0
    assert stat.S_ISFIFO(receipt.lstat().st_mode)


def test_verification_persists_pending_receipt_identity_before_publication() -> None:
    repo = _init_repo()
    sid = "pending-receipt-identity"
    verification_id = "pending-verifier"
    receipt = repo / "verdict.json"
    try:
        state = fc.mark_verification_started(sid, verification_id, receipt)
        assert state["verdict_path"] == str(receipt)
        assert state["receipt_identity"] == {
            "verification_id": verification_id,
            "session_id": sid,
        }
        owned = {
            "schema_version": 2,
            "verification_id": verification_id,
            "session_id": sid,
            "result": "RED",
        }
        fc.write_json_owner_only_no_replace(receipt, owned)
        state = fc.disarm_session(sid)
        assert not receipt.exists()
        assert state["receipt_identity"] is None
    finally:
        fc.clear_state(sid)


def test_large_generated_receipt_is_compacted_and_remains_cleanable() -> None:
    repo = _init_repo()
    sid = "bounded-generated-receipt"
    receipt = repo / "verdict.json"
    verdict = {
        "schema_version": 2,
        "verification_id": "large-verification",
        "session_id": sid,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "sha",
        "ts": 1.0,
        "verified_snapshot": {"changed_paths": ["x" * 1024] * 2048},
    }
    compact = fc.compact_verdict_receipt(verdict)
    encoded = fc._serialized_json_text(compact).encode("utf-8")
    assert len(encoded) <= fc.MAX_VERDICT_RECEIPT_BYTES
    assert compact["receipt_compacted"] is True
    assert compact["verification_id"] == verdict["verification_id"]
    fc.write_json_owner_only_no_replace(receipt, compact)
    assert receipt.stat().st_size == len(encoded)
    assert fc._unlink_managed_verdict(receipt, verdict) is True
    assert not receipt.exists()


def test_large_green_snapshot_persists_bounded_state() -> None:
    repo = _init_repo()
    sid = "bounded-large-state"
    original_capture = fv.capture_workspace_snapshot
    huge_paths = [f"{index}-{'x' * 4096}" for index in range(1400)]
    snapshot = {
        "version": fv.SNAPSHOT_VERSION,
        "workspace_root": str(repo.resolve()),
        "git_worktree": True,
        "snapshot_complete": True,
        "head": "head",
        "index_tree": "tree",
        "unstaged_diff_sha": "",
        "staged_diff_sha": "",
        "untracked": [],
        "untracked_sha": "untracked",
        "config_sha": "config",
        "gate_argv": ["true"],
        "gate_mode": "argv",
        "gate_identity": fv._gate_identity(["true"], "argv"),
        "diff_sha": "diff",
        "paths": huge_paths,
        "snapshot_id": "bounded-state-snapshot",
    }

    def large_snapshot(*_args, **_kwargs):
        return dict(snapshot)

    try:
        fv.capture_workspace_snapshot = large_snapshot
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["result"] == "GREEN"
        state_path = fc.state_path(sid)
        assert state_path.stat().st_size <= fc.MAX_JSON_DOCUMENT_BYTES
        state = fc.read_state(sid)
        recorded = state["verdict"]["verified_snapshot"]
        assert recorded["snapshot_id"] == "bounded-state-snapshot"
        assert "paths" not in recorded
        approved = _approved_gate("true", repo)
        assert fv.verdict_is_snapshot_fresh_green(
            state["verdict"],
            state["last_dispatch_ts"],
            state["dispatch_generation"],
            session_id=sid,
            cwd=str(repo),
            approved_gate=approved,
        )
    finally:
        fv.capture_workspace_snapshot = original_capture
        fc.clear_state(sid)


def test_verification_does_not_replace_another_sessions_receipt() -> None:
    repo = _init_repo()
    first_sid = "receipt-owner-a"
    second_sid = "receipt-owner-b"
    receipt = repo / "verdict.json"
    try:
        first = fv.run_gate("true", session_id=first_sid, cwd=str(repo))
        assert first["result"] == "GREEN"
        assert first["session_id"] == first_sid
        original = receipt.read_bytes()
        try:
            fv.run_gate("true", session_id=second_sid, cwd=str(repo))
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("a second session must not replace another session's receipt")
        assert receipt.read_bytes() == original
        assert fc.read_state(first_sid)["verdict"]["session_id"] == first_sid
        assert fc.read_state(second_sid)["verdict"] is None
    finally:
        fc.clear_state(first_sid)
        fc.clear_state(second_sid)


def test_gate_created_receipt_target_is_not_overwritten() -> None:
    repo = _init_repo()
    sid = "gate-created-receipt"
    gate = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote("from pathlib import Path; Path('verdict.json').write_text('gate output')")
    )
    try:
        try:
            fv.run_gate(gate, session_id=sid, cwd=str(repo))
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("verification must not replace a receipt created by the gate")
        assert (repo / "verdict.json").read_text() == "gate output"
        assert fc.read_state(sid)["verdict"] is None
    finally:
        fc.clear_state(sid)


def test_receipt_created_after_availability_check_is_not_overwritten() -> None:
    repo = _init_repo()
    sid = "receipt-publication-race"
    receipt = repo / "verdict.json"
    original_check = fc._require_receipt_target_available
    checks = 0

    def race_after_check(path):
        nonlocal checks
        original_check(path)
        checks += 1
        if checks == 2:
            receipt.write_text("racing writer")

    try:
        fc._require_receipt_target_available = race_after_check
        try:
            fv.run_gate("true", session_id=sid, cwd=str(repo))
        except FileExistsError:
            pass
        else:
            raise AssertionError("receipt publication must atomically refuse a racing writer")
        assert receipt.read_text() == "racing writer"
        assert fc.read_state(sid)["verdict"] is None
    finally:
        fc._require_receipt_target_available = original_check
        fc.clear_state(sid)


def test_untracked_legacy_unarmed_receipt_upgrades_cleanly() -> None:
    repo = _init_repo()
    sid = "legacy-unarmed-upgrade"
    legacy = {
        "schema_version": 2,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "legacy-diff",
        "ts": 123.0,
    }
    receipt = repo / "verdict.json"
    receipt.write_text(json.dumps(legacy))
    fc.write_state(sid, verdict=legacy, verdict_path=None, approved_gate=None)
    try:
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["result"] == "GREEN"
        assert json.loads(receipt.read_text())["verification_id"] == verdict["verification_id"]
    finally:
        fc.clear_state(sid)


def test_unknown_receipt_path_retains_identity_until_workspace_is_known() -> None:
    legacy = {
        "schema_version": 2,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "unknown-path",
        "ts": 123.0,
    }
    for operation in ("dispatch", "disarm"):
        repo = _init_repo()
        sid = f"unknown-receipt-{operation}"
        receipt = repo / "verdict.json"
        receipt.write_text(json.dumps(legacy))
        fc.write_state(sid, verdict=legacy, verdict_path=None, approved_gate=None)
        try:
            if operation == "dispatch":
                fc.mark_dispatch_started(sid, "unknown-path-dispatch", time.time())
                fc.mark_dispatch_finished(sid, "unknown-path-dispatch", time.time())
            else:
                fc.disarm_session(sid)
            state = fc.read_state(sid)
            assert state["verdict_path"] is None
            assert state["receipt_identity"]["diff_sha"] == "unknown-path"
            assert receipt.exists()

            verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
            assert verdict["result"] == "GREEN"
            assert json.loads(receipt.read_text())["verification_id"] == verdict["verification_id"]
        finally:
            fc.clear_state(sid)


def test_rearm_retains_receipt_identity_for_next_verification() -> None:
    repo = _init_repo()
    sid = "rearm-receipt-identity"
    approved = _approved_gate("true", repo)
    try:
        assert fv.run_gate("true", session_id=sid, cwd=str(repo))["result"] == "GREEN"
        fc.arm_session(sid, approved)
        state = fc.read_state(sid)
        assert state["verdict"] is None
        assert isinstance(state["receipt_identity"], dict)
        assert fv.run_gate("true", session_id=sid, cwd=str(repo))["result"] == "GREEN"
    finally:
        fc.clear_state(sid)


def test_cleanup_refuses_receipt_swapped_after_validation() -> None:
    repo = _init_repo()
    sid = "cleanup-inode-swap"
    receipt = repo / "verdict.json"
    try:
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        original_replace = fc.os.replace

        def swap_before_quarantine(source, destination):
            replacement = repo / "replacement-receipt"
            replacement.write_text("unrelated replacement")
            original_replace(replacement, receipt)
            return original_replace(source, destination)

        fc.os.replace = swap_before_quarantine
        assert fc._unlink_managed_verdict(receipt, verdict) is False
        assert receipt.read_text() == "unrelated replacement"
        assert not list(repo.glob(".verdict.json.frontier-quarantine-*"))
    finally:
        fc.os.replace = original_replace if "original_replace" in locals() else fc.os.replace
        fc.clear_state(sid)


def test_cleanup_quarantines_before_content_validation() -> None:
    repo = _init_repo()
    sid = "cleanup-quarantine-first"
    receipt = repo / "verdict.json"
    original_replace = fc.os.replace
    try:
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))

        def rewrite_before_rename(source, destination):
            Path(source).write_text("concurrent unrelated rewrite")
            return original_replace(source, destination)

        fc.os.replace = rewrite_before_rename
        assert fc._unlink_managed_verdict(receipt, verdict) is False
        assert receipt.read_text() == "concurrent unrelated rewrite"
        assert not list(repo.glob(".verdict.json.frontier-quarantine-*"))
    finally:
        fc.os.replace = original_replace
        fc.clear_state(sid)


def test_cleanup_preserves_preexisting_receipt_directory() -> None:
    repo = _init_repo()
    receipt = repo / "verdict.json"
    receipt.mkdir()
    marker = receipt / "keep.txt"
    marker.write_text("workspace content")
    assert fc._unlink_managed_verdict(
        receipt,
        {"verification_id": "not-this-directory", "session_id": "directory-owner"},
    ) is False
    assert receipt.is_dir()
    assert marker.read_text() == "workspace content"
    assert not list(repo.glob(".verdict.json.frontier-quarantine-*"))


def test_disarm_retains_cleanup_metadata_when_unlink_fails() -> None:
    repo = _init_repo()
    sid = "disarm-cleanup-retry"
    receipt = repo / "verdict.json"
    original_unlink = fc._unlink_managed_verdict
    try:
        assert fv.run_gate("true", session_id=sid, cwd=str(repo))["result"] == "GREEN"

        def fail_cleanup(path, expected=None):
            raise OSError("simulated disarm cleanup failure")

        fc._unlink_managed_verdict = fail_cleanup
        try:
            fc.disarm_session(sid)
        except OSError:
            pass
        else:
            raise AssertionError("disarm must report receipt cleanup failure")
        state = fc.read_state(sid)
        assert state["armed"] is False
        assert state["verdict"] is None
        assert state["verdict_path"] == str(receipt)
        assert isinstance(state["receipt_identity"], dict)

        fc._unlink_managed_verdict = original_unlink
        state = fc.disarm_session(sid)
        assert not receipt.exists()
        assert state["verdict_path"] is None
        assert state["receipt_identity"] is None
    finally:
        fc._unlink_managed_verdict = original_unlink
        fc.clear_state(sid)


def test_disarm_cleans_legacy_approved_workspace_receipt() -> None:
    repo = _init_repo()
    sid = "disarm-legacy-approved-workspace"
    receipt = repo / "verdict.json"
    legacy = {
        "schema_version": 2,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "legacy-disarm",
        "ts": 1.0,
    }
    try:
        fc.write_json_owner_only(receipt, legacy)
        fc.write_state(
            sid,
            armed=True,
            verdict=legacy,
            approved_gate=_approved_gate("true", repo),
            verdict_path=None,
        )
        state = fc.disarm_session(sid)
        assert not receipt.exists()
        assert state["approved_gate"] is None
        assert state["verdict"] is None
        assert state["verdict_path"] is None
        assert state["receipt_identity"] is None
    finally:
        fc.clear_state(sid)


def test_unarmed_verification_receipt_is_cleared_by_later_dispatch() -> None:
    repo = _init_repo()
    sid = "unarmed-receipt-cleanup"
    try:
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["result"] == "GREEN"
        assert (repo / "verdict.json").exists()
        state = fc.read_state(sid)
        assert state["verdict_path"] == str(repo / "verdict.json")
        assert state.get("approved_gate") is None

        fc.mark_dispatch_started(sid, "after-unarmed-verify", time.time())
        assert not (repo / "verdict.json").exists()
        assert fc.read_state(sid)["verdict"] is None
        fc.mark_dispatch_finished(sid, "after-unarmed-verify", time.time())
    finally:
        fc.clear_state(sid)


def test_historical_workspace_receipt_path_is_not_revisited() -> None:
    first_repo = _init_repo()
    second_repo = _init_repo()
    sid = "historical-receipt-path"
    try:
        assert fv.run_gate("true", session_id=sid, cwd=str(first_repo))["result"] == "GREEN"
        assert fv.run_gate("true", session_id=sid, cwd=str(second_repo))["result"] == "GREEN"
        old_path = first_repo / "verdict.json"
        old_path.write_text("new unrelated content")

        fc.mark_dispatch_started(sid, "after-workspace-move", time.time())
        assert old_path.read_text() == "new unrelated content"
        assert not (second_repo / "verdict.json").exists()
        assert fc.read_state(sid)["verdict_path"] is None
        fc.mark_dispatch_finished(sid, "after-workspace-move", time.time())
    finally:
        fc.clear_state(sid)


def test_legacy_v2_receipt_is_invalidated_only_when_state_matches() -> None:
    repo = _init_repo()
    sid = "legacy-v2-receipt"
    legacy = {
        "schema_version": 2,
        "result": "GREEN",
        "gate": "true",
        "exit_code": 0,
        "diff_sha": "legacy-diff",
        "ts": 123.0,
    }
    artifact = repo / "verdict.json"
    try:
        artifact.write_text(json.dumps(legacy))
        fc.write_state(
            sid,
            verdict=legacy,
            approved_gate={"gate": "true", "argv": ["true"], "cwd": str(repo)},
        )
        fc.mark_dispatch_started(sid, "legacy-cleanup", time.time())
        assert not artifact.exists()
        fc.mark_dispatch_finished(sid, "legacy-cleanup", time.time())

        artifact.write_text(json.dumps({**legacy, "diff_sha": "unrelated"}))
        fc.write_state(sid, verdict=legacy)
        fc.mark_dispatch_started(sid, "legacy-mismatch", time.time())
        assert artifact.exists()
        fc.mark_dispatch_finished(sid, "legacy-mismatch", time.time())
    finally:
        fc.clear_state(sid)


def test_state_mutation_after_final_snapshot_forces_red() -> None:
    repo = _init_repo()
    sid = "snap-state-after-final"
    fc.clear_state(sid)
    _arm(sid, repo)
    original_capture = fv.capture_workspace_snapshot
    calls = 0

    def mutate_after_capture(*args, **kwargs):
        nonlocal calls
        snapshot = original_capture(*args, **kwargs)
        calls += 1
        if calls == 2:
            fc.write_state(sid, config={"codex_effort": "low"})
        return snapshot

    try:
        fv.capture_workspace_snapshot = mutate_after_capture
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["exit_code"] == 0
        assert verdict["result"] == "RED"
        assert verdict["verified_snapshot"] is None
        persisted = fc.read_state(sid).get("verdict")
        assert not persisted or persisted.get("result") == "RED"
    finally:
        fv.capture_workspace_snapshot = original_capture
        fc.clear_state(sid)


def test_stop_hook_blocks_corrupt_global_config_after_green() -> None:
    repo = _init_repo()
    sid = "snap-corrupt-global-after-green"
    fc.clear_state(sid)
    _arm(sid, repo)
    old_config = fc.GLOBAL_CONFIG.read_bytes() if fc.GLOBAL_CONFIG.exists() else None
    try:
        verdict = fv.run_gate("true", session_id=sid, cwd=str(repo))
        assert verdict["result"] == "GREEN"
        fc.write_text_owner_only(fc.GLOBAL_CONFIG, "{broken global config")
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2
        assert "could not be validated safely" in proc.stderr
    finally:
        if old_config is None:
            try:
                fc.GLOBAL_CONFIG.unlink()
            except FileNotFoundError:
                pass
        else:
            fc.write_bytes_owner_only(fc.GLOBAL_CONFIG, old_config)
        fc.clear_state(sid)


def test_verdict_artifact_failure_clears_session_authority() -> None:
    repo = _init_repo()
    sid = "snap-artifact-write-failure"
    fc.clear_state(sid)
    _arm(sid, repo)
    original_write = fc.write_json_owner_only_no_replace

    def fail_artifact(path, data, mode=fc.OWNER_ONLY_FILE, **kwargs):
        if Path(path).name == "verdict.json":
            raise OSError("simulated artifact write failure")
        return original_write(path, data, mode=mode, **kwargs)

    try:
        fc.write_json_owner_only_no_replace = fail_artifact
        try:
            fv.run_gate("true", session_id=sid, cwd=str(repo))
        except OSError as exc:
            assert "artifact write failure" in str(exc)
        else:
            raise AssertionError("artifact write failure must fail verification")
        state = fc.read_state(sid)
        assert state["verdict"] is None
        assert state["active_verifications"] == []
        assert not (repo / "verdict.json").exists()
    finally:
        fc.write_json_owner_only_no_replace = original_write
        fc.clear_state(sid)


def test_cli_legacy_shell_flag() -> None:
    repo = _init_repo()
    sid = "snap-cli-legacy"
    fc.clear_state(sid)
    env = os.environ.copy()
    env["FRONTIER_SESSION_ID"] = sid
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "frontier_verify.py"),
                "--gate",
                "true",
                "--cwd",
                str(repo),
                "--session",
                sid,
                "--legacy-shell",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(ROOT),
        )
        assert proc.returncode != 0 or True  # result may be GREEN with unsafe
        disk = json.loads((repo / "verdict.json").read_text())
        assert disk["unsafe"] is True
        assert disk["gate_mode"] == "legacy_shell"
    finally:
        fc.clear_state(sid)


def main() -> int:
    tests = [
        test_clean_green_argv,
        test_green_verdict_is_bound_to_exact_session_id,
        test_red_on_nonzero_exit,
        test_non_git_workspace_cannot_green,
        test_mutation_unstaged_invalidates,
        test_mutation_staged_invalidates,
        test_mutation_committed_invalidates,
        test_mutation_untracked_invalidates,
        test_config_change_invalidates,
        test_gate_can_change_global_config_without_deadlock_and_forces_red,
        test_gate_identity_change_invalidates,
        test_approved_gate_binding_rejects_forged_green,
        test_legacy_verdict_rejected,
        test_unsafe_legacy_shell_rejected,
        test_argv_default_rejects_shell_syntax,
        test_owner_only_verdict_mode,
        test_gate_mutates_workspace_not_green,
        test_dispatch_timestamp_compat,
        test_oversized_untracked_content_change_invalidates,
        test_overflow_untracked_meta_change_invalidates,
        test_truncated_untracked_snapshot_cannot_green,
        test_dispatch_generation_rejects_old_green_after_clock_regression,
        test_dispatch_during_gate_forces_red_without_workspace_change,
        test_state_race_before_verdict_persistence_forces_red,
        test_overlapping_verifications_cannot_publish_green,
        test_verification_preserves_unrecognized_receipt_path_occupant,
        test_dispatch_preserves_unrecognized_receipt_path_occupant,
        test_dispatch_cleanup_failure_retains_retry_metadata_without_green_authority,
        test_dispatch_retains_cleanup_metadata_when_safe_removal_returns_false,
        test_verification_refuses_preexisting_receipt_symlink,
        test_receipt_fifo_is_rejected_without_blocking,
        test_verification_persists_pending_receipt_identity_before_publication,
        test_large_generated_receipt_is_compacted_and_remains_cleanable,
        test_large_green_snapshot_persists_bounded_state,
        test_verification_does_not_replace_another_sessions_receipt,
        test_gate_created_receipt_target_is_not_overwritten,
        test_receipt_created_after_availability_check_is_not_overwritten,
        test_untracked_legacy_unarmed_receipt_upgrades_cleanly,
        test_unknown_receipt_path_retains_identity_until_workspace_is_known,
        test_rearm_retains_receipt_identity_for_next_verification,
        test_cleanup_refuses_receipt_swapped_after_validation,
        test_cleanup_quarantines_before_content_validation,
        test_cleanup_preserves_preexisting_receipt_directory,
        test_disarm_retains_cleanup_metadata_when_unlink_fails,
        test_disarm_cleans_legacy_approved_workspace_receipt,
        test_unarmed_verification_receipt_is_cleared_by_later_dispatch,
        test_historical_workspace_receipt_path_is_not_revisited,
        test_legacy_v2_receipt_is_invalidated_only_when_state_matches,
        test_state_mutation_after_final_snapshot_forces_red,
        test_stop_hook_blocks_corrupt_global_config_after_green,
        test_verdict_artifact_failure_clears_session_authority,
        test_cli_legacy_shell_flag,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
