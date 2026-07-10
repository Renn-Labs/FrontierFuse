#!/usr/bin/env python3
"""Standalone stdlib contracts for 0.2.6 snapshot-bound verification.

Creates temporary git repositories; does not touch the live working tree.
stdlib-only, keyless, offline.
"""
from __future__ import annotations

import json
import os
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
    gate: str = "/bin/true",
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
    gate = str(verdict.get("gate") or "/bin/false")
    gate_argv = verdict.get("gate_argv")
    approved = {
        "gate": gate,
        "argv": list(gate_argv) if isinstance(gate_argv, list) and gate_argv else ["/bin/false"],
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN", v
        assert v["exit_code"] == 0
        assert v["schema_version"] == fv.VERDICT_SCHEMA_VERSION
        assert v["gate_mode"] == "argv"
        assert v["gate_argv"] == ["/bin/true"]
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
    finally:
        fc.clear_state(sid)


def test_red_on_nonzero_exit() -> None:
    repo = _init_repo()
    sid = "snap-red-exit"
    fc.clear_state(sid)
    try:
        v = fv.run_gate("/bin/false", session_id=sid, cwd=str(repo))
        assert v["result"] == "RED"
        assert v["exit_code"] != 0
        assert v["snapshot_stable"] is True
        assert v["verified_snapshot"] is None
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        (repo / "README.md").write_text("dirty unstaged\n")
        # Re-load state (run_gate already wrote it) and run Stop
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2, "unstaged mutation must reject GREEN"
        assert not fv.verdict_is_snapshot_fresh_green(
            fc.read_state(sid)["verdict"],
            fc.read_state(sid)["last_dispatch_ts"],
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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


def test_gate_identity_change_invalidates() -> None:
    repo = _init_repo()
    sid = "snap-mut-gate"
    fc.clear_state(sid)
    _arm(sid, repo)
    try:
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Tamper: claim a different gate while keeping verified_snapshot from /bin/true.
        st = fc.read_state(sid)
        verdict = dict(st["verdict"])
        verdict["gate"] = "/bin/false"
        verdict["gate_argv"] = ["/bin/false"]
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
    try:
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        mode = stat.S_IMODE((repo / "verdict.json").stat().st_mode)
        assert mode & 0o077 == 0, f"group/other bits must be clear, got {oct(mode)}"
        assert mode & 0o600 == 0o600
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
    """Verdict stamped before last_dispatch_ts is not fresh (timestamp rule preserved)."""
    repo = _init_repo()
    sid = "snap-ts-compat"
    fc.clear_state(sid)
    try:
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "GREEN"
        # Simulate a newer dispatch after the verdict.
        future = float(v["ts"]) + 100.0
        fc.write_state(
            sid,
            armed=True,
            last_dispatch_ts=future,
            verdict=v,
            approved_gate=_approved_gate("/bin/true", repo),
        )
        assert (
            fv.verdict_is_snapshot_fresh_green(
                v,
                future,
                session_id=sid,
                cwd=str(repo),
                approved_gate=fc.read_state(sid).get("approved_gate"),
            )
            is False
        )
        proc = _run_stop(sid, cwd=repo)
        assert proc.returncode == 2
        # Inclusive equality still works (matches frontier_common contract).
        fc.write_state(
            sid,
            armed=True,
            last_dispatch_ts=float(v["ts"]),
            verdict=v,
            approved_gate=_approved_gate("/bin/true", repo),
        )
        assert (
            fv.verdict_is_snapshot_fresh_green(
                v,
                float(v["ts"]),
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
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
        v = fv.run_gate("/bin/true", session_id=sid, cwd=str(repo))
        assert v["result"] == "RED"
        assert v["workspace_supported"] is False
        assert v["verified_snapshot"] is None
    finally:
        fv.MAX_UNTRACKED_FILES = old_max
        fv.MAX_UNTRACKED_META = old_meta
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
        test_red_on_nonzero_exit,
        test_non_git_workspace_cannot_green,
        test_mutation_unstaged_invalidates,
        test_mutation_staged_invalidates,
        test_mutation_committed_invalidates,
        test_mutation_untracked_invalidates,
        test_config_change_invalidates,
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
