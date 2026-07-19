#!/usr/bin/env python3
"""frontier_verify.py — deterministic gate runner for FrontierFuse orchestrator mode.

The brain (Fable) never closes a loop on a prose "GREEN". It must run a real EXTERNAL gate
(tests / build / lint / repro) through this module. The gate's exit code — not a model's
opinion — decides GREEN/RED, and only when a versioned workspace snapshot remains stable
across the gate. The verdict is written to verdict.json and into the session state so the
Stop hook can enforce it.

stdlib-only, Python 3.10+, importable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontier_common as fc

GATE_TIMEOUT = int(os.environ.get("FRONTIER_GATE_TIMEOUT", "600"))

# Versioned snapshot / verdict schema for 0.2.6 snapshot-bound verification.
SNAPSHOT_VERSION = 1
VERDICT_SCHEMA_VERSION = 2  # v1 = legacy make_verdict fields only; v2 = snapshot-bound
MAX_UNTRACKED_FILES = int(os.environ.get("FRONTIER_SNAPSHOT_MAX_UNTRACKED", "200"))
MAX_UNTRACKED_BYTES = int(os.environ.get("FRONTIER_SNAPSHOT_MAX_FILE_BYTES", str(1_048_576)))
# Metadata (path+size+mtime) for untracked files beyond the full-content hash cap.
MAX_UNTRACKED_META = int(os.environ.get("FRONTIER_SNAPSHOT_MAX_UNTRACKED_META", "5000"))
_OVERSIZED_SAMPLE = 65536
# Verifier-owned artifacts written into cwd after the final snapshot is taken; including them
# would make every GREEN immediately stale on the next Stop recompute.
_SNAPSHOT_IGNORE_UNTRACKED = frozenset(
    {
        "verdict.json",
    }
)

# Keys persisted into session state (must include snapshot fields for the Stop hook).
_STATE_VERDICT_KEYS = (
    "result",
    "gate",
    "exit_code",
    "diff_sha",
    "paths",
    "ts",
    "after_dispatch_ts",
    "dispatch_generation",
    "schema_version",
    "gate_argv",
    "gate_mode",
    "unsafe",
    "unsafe_reason",
    "snapshot_stable",
    "workspace_supported",
    "pre_gate_snapshot",
    "verified_snapshot",
    "final_snapshot",
    "verification_id",
    "session_id",
)

_STATE_SNAPSHOT_KEYS = (
    "version",
    "workspace_root",
    "git_worktree",
    "snapshot_complete",
    "snapshot_id",
    "gate_identity",
    "gate_argv",
    "gate_mode",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return out.returncode, out.stdout or "", out.stderr or ""
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def _git_ok(args: list[str], cwd: str) -> str:
    rc, stdout, _ = _git(args, cwd)
    return stdout if rc == 0 else ""


def _is_git_repo(cwd: str) -> bool:
    rc, stdout, _ = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0 and stdout.strip() == "true"


def is_git_worktree(cwd: str) -> bool:
    """Return whether *cwd* is inside a Git worktree suitable for hardened closure."""
    try:
        return _is_git_repo(str(Path(cwd).resolve()))
    except OSError:
        return False


def _file_stat_meta(path: Path) -> dict[str, int]:
    try:
        st = path.stat()
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        return {"size": int(st.st_size), "mtime_ns": mtime_ns}
    except OSError:
        return {"size": -1, "mtime_ns": 0}


def _file_sha256(path: Path) -> str:
    """Content fingerprint with a size bound.

    Small files: full sha256. Oversized files: size + mtime + head/tail samples so
    same-size content rewrites still change the snapshot.
    """
    try:
        st = path.stat()
    except OSError:
        return "missing"
    size = int(st.st_size)
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    if size <= MAX_UNTRACKED_BYTES:
        try:
            return _sha256_bytes(path.read_bytes())
        except OSError:
            return "unreadable"
    try:
        with path.open("rb") as fh:
            head = fh.read(_OVERSIZED_SAMPLE)
            if size > _OVERSIZED_SAMPLE:
                fh.seek(max(0, size - _OVERSIZED_SAMPLE))
                tail = fh.read(_OVERSIZED_SAMPLE)
            else:
                tail = b""
        return "oversized:" + _sha256_text(
            json.dumps(
                {
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "head": _sha256_bytes(head),
                    "tail": _sha256_bytes(tail),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except OSError:
        return f"oversized-unreadable:{size}:{mtime_ns}"


def _config_sha(session_id: str) -> str:
    cfg = fc.resolve_config(session_id=session_id)
    payload = {k: cfg.get(k) for k in sorted(fc.CONFIG_KEYS)}
    return _sha256_text(json.dumps(payload, sort_keys=True, default=str))


def _gate_identity(gate_argv: list[str] | None, gate_mode: str) -> str:
    payload = {
        "mode": gate_mode or "argv",
        "argv": list(gate_argv or []),
    }
    return _sha256_text(json.dumps(payload, sort_keys=True))


def capture_workspace_snapshot(
    cwd: str,
    session_id: str = "default",
    gate_argv: list[str] | None = None,
    gate_mode: str = "argv",
) -> dict[str, Any]:
    """Versioned workspace snapshot for snapshot-bound GREEN.

    Covers: resolved workspace root, HEAD, index tree, unstaged/staged diff hashes,
    bounded untracked file hashes, effective configuration hash, and verifier argv/identity.
    """
    root = str(Path(cwd).resolve())
    gate_argv = list(gate_argv or [])
    gate_mode = gate_mode or "argv"

    head = ""
    index_tree = ""
    unstaged_diff = ""
    staged_diff = ""
    untracked_entries: list[dict[str, str]] = []
    changed_paths: list[str] = []
    git_worktree = is_git_worktree(root)
    snapshot_complete = True

    if git_worktree:
        head = _git_ok(["rev-parse", "HEAD"], root).strip()
        # Index tree OID (contents of the index as a tree object).
        index_tree = _git_ok(["write-tree"], root).strip()
        unstaged_diff = _git_ok(["diff", "--no-color"], root)
        staged_diff = _git_ok(["diff", "--cached", "--no-color"], root)
        names = _git_ok(["diff", "--name-only"], root)
        staged_names = _git_ok(["diff", "--cached", "--name-only"], root)
        paths: list[str] = []
        for block in (names, staged_names):
            for ln in block.splitlines():
                p = ln.strip()
                if p and p not in paths:
                    paths.append(p)
        changed_paths = paths

        untracked_raw = _git_ok(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            root,
        )
        untracked_paths = [
            p
            for p in untracked_raw.split("\0")
            if p
            and Path(p).name not in _SNAPSHOT_IGNORE_UNTRACKED
            and p not in _SNAPSHOT_IGNORE_UNTRACKED
        ]
        untracked_paths.sort()
        # Full content fingerprints for the first N files.
        bounded = untracked_paths[:MAX_UNTRACKED_FILES]
        for rel in bounded:
            abs_path = Path(root) / rel
            untracked_entries.append({"path": rel, "sha256": _file_sha256(abs_path)})
        # Metadata fingerprints for files beyond the content-hash cap (and up to META cap)
        # so content/size/mtime changes outside the full-hash window still invalidate GREEN.
        if len(untracked_paths) > MAX_UNTRACKED_FILES:
            overflow = untracked_paths[MAX_UNTRACKED_FILES:MAX_UNTRACKED_META]
            overflow_meta: list[dict[str, object]] = []
            for rel in overflow:
                meta = _file_stat_meta(Path(root) / rel)
                overflow_meta.append(
                    {"path": rel, "size": meta["size"], "mtime_ns": meta["mtime_ns"]}
                )
            remainder = max(0, len(untracked_paths) - MAX_UNTRACKED_META)
            untracked_entries.append(
                {
                    "path": f"__overflow_meta__:{len(overflow)}",
                    "sha256": _sha256_text(
                        json.dumps(overflow_meta, sort_keys=True, separators=(",", ":"))
                    ),
                }
            )
            if remainder:
                # Paths alone cannot prove unchanged content. Refuse hardened GREEN when the
                # bounded snapshot cannot cover every non-ignored untracked file.
                snapshot_complete = False
                untracked_entries.append(
                    {
                        "path": f"__truncated_paths__:{remainder}",
                        "sha256": _sha256_text(
                            "\n".join(untracked_paths[MAX_UNTRACKED_META:])
                        ),
                    }
                )

    untracked_sha = _sha256_text(
        json.dumps(untracked_entries, sort_keys=True, separators=(",", ":"))
    )
    unstaged_diff_sha = _sha256_text(unstaged_diff) if unstaged_diff else ""
    staged_diff_sha = _sha256_text(staged_diff) if staged_diff else ""
    # Composite for legacy diff_sha consumers: both trees of dirt + untracked set.
    composite = _sha256_text(
        json.dumps(
            {
                "unstaged": unstaged_diff_sha,
                "staged": staged_diff_sha,
                "untracked": untracked_sha,
                "head": head,
                "index_tree": index_tree,
            },
            sort_keys=True,
        )
    )

    snap: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "workspace_root": root,
        "git_worktree": git_worktree,
        "snapshot_complete": snapshot_complete,
        "head": head,
        "index_tree": index_tree,
        "unstaged_diff_sha": unstaged_diff_sha,
        "staged_diff_sha": staged_diff_sha,
        "untracked": untracked_entries,
        "untracked_sha": untracked_sha,
        "config_sha": _config_sha(session_id),
        "gate_argv": gate_argv,
        "gate_mode": gate_mode,
        "gate_identity": _gate_identity(gate_argv, gate_mode),
        "diff_sha": composite,
        "paths": changed_paths,
    }
    snap["snapshot_id"] = _snapshot_id(snap)
    return snap


def _snapshot_id(snap: dict[str, Any]) -> str:
    """Stable identity over the security-relevant snapshot fields."""
    payload = {
        "version": snap.get("version"),
        "workspace_root": snap.get("workspace_root"),
        "git_worktree": snap.get("git_worktree"),
        "snapshot_complete": snap.get("snapshot_complete"),
        "head": snap.get("head"),
        "index_tree": snap.get("index_tree"),
        "unstaged_diff_sha": snap.get("unstaged_diff_sha"),
        "staged_diff_sha": snap.get("staged_diff_sha"),
        "untracked_sha": snap.get("untracked_sha"),
        "config_sha": snap.get("config_sha"),
        "gate_identity": snap.get("gate_identity"),
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def snapshots_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    id_a = a.get("snapshot_id") or _snapshot_id(a)
    id_b = b.get("snapshot_id") or _snapshot_id(b)
    return id_a == id_b and bool(id_a)


def _state_verdict_for_persistence(verdict: dict[str, Any]) -> dict[str, Any]:
    """Keep only the snapshot identity and gate fields required by the Stop verifier."""
    persisted = {
        key: verdict[key]
        for key in _STATE_VERDICT_KEYS
        if key in verdict and key not in {"paths", "pre_gate_snapshot", "verified_snapshot", "final_snapshot"}
    }
    persisted["paths"] = []
    for key in ("pre_gate_snapshot", "verified_snapshot", "final_snapshot"):
        snapshot = verdict.get(key)
        persisted[key] = (
            {field: snapshot[field] for field in _STATE_SNAPSHOT_KEYS if field in snapshot}
            if isinstance(snapshot, dict)
            else None
        )
    return persisted


def _diff_fingerprint(cwd: str) -> tuple[str, list[str]]:
    """Best-effort legacy fingerprint (unstaged diff only). Prefer capture_workspace_snapshot."""
    snap = capture_workspace_snapshot(cwd, session_id="default", gate_argv=[], gate_mode="argv")
    return snap.get("unstaged_diff_sha", "") or "", list(snap.get("paths") or [])


def parse_gate_argv(gate: str) -> list[str]:
    """Parse one simple argv-style gate command; reject shell syntax and empty input."""
    return fc.parse_gate_argv(gate)


def _write_owner_only(path: Path, text: str) -> None:
    """Write text with owner-only permissions (0600)."""
    path = Path(path)
    data = text.encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _run_gate_argv(argv: list[str], cwd: str) -> tuple[int, str, str]:
    """Run an argv gate with shared byte-capped capture + descendant containment."""
    try:
        max_bytes = fc.gate_capture_max_bytes()
    except ValueError as exc:
        return 2, "", f"invalid gate capture limit: {exc}"
    try:
        rc, stdout, stderr = fc.run_bounded_subprocess(
            argv,
            timeout=GATE_TIMEOUT,
            cwd=cwd,
            shell=False,
            max_stdout_bytes=max_bytes,
            max_stderr_bytes=max_bytes,
            start_new_session=True,
        )
        return rc, stdout or "", stderr or ""
    except FileNotFoundError:
        return 127, "", f"gate executable not found: {argv[0]!r}"
    except subprocess.TimeoutExpired:
        return 124, "", f"gate timed out after {GATE_TIMEOUT}s"
    except fc.ContainmentError as exc:
        # Fail closed: never present a successful gate when descendants may survive.
        return 125, "", f"gate containment failed: {exc}"
    except OSError as exc:
        return 127, "", f"gate exec failed: {exc}"


def _run_gate_legacy_shell(gate: str, cwd: str) -> tuple[int, str, str]:
    """Explicitly named unsafe compatibility path (shell=True).

    Still uses the shared bounded capture primitive so stdout/stderr cannot grow
    without limit and timeout kills the process group.
    """
    try:
        max_bytes = fc.gate_capture_max_bytes()
    except ValueError as exc:
        return 2, "", f"invalid gate capture limit: {exc}"
    try:
        rc, stdout, stderr = fc.run_bounded_subprocess(
            gate,
            timeout=GATE_TIMEOUT,
            cwd=cwd,
            shell=True,
            max_stdout_bytes=max_bytes,
            max_stderr_bytes=max_bytes,
            start_new_session=True,
        )
        return rc, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"gate timed out after {GATE_TIMEOUT}s"
    except fc.ContainmentError as exc:
        return 125, "", f"gate containment failed: {exc}"
    except OSError as exc:
        return 127, "", f"gate shell exec failed: {exc}"


def is_snapshot_bound_verdict(verdict: dict | None) -> bool:
    """True when a verdict carries the 0.2.6+ snapshot schema (not legacy-only)."""
    if not isinstance(verdict, dict):
        return False
    schema = verdict.get("schema_version")
    if type(schema) is not int or schema < VERDICT_SCHEMA_VERSION:
        return False
    return isinstance(verdict.get("verified_snapshot"), dict) or isinstance(
        verdict.get("final_snapshot"), dict
    )


def verdict_is_snapshot_fresh_green(
    verdict: dict | None,
    last_dispatch_ts: float,
    dispatch_generation: int,
    session_id: str = "default",
    cwd: str | None = None,
    approved_gate: dict | None = None,
) -> bool:
    """Stronger Stop-gate check: GREEN + fresh ts + snapshot-bound + stable + not unsafe.

    Legacy verdicts (schema_version < 2 / no verified_snapshot) remain readable but never
    satisfy this gate. The caller must supply the arm-time approved argv/cwd. Recomputes the
    current workspace snapshot and requires its identity and gate binding to match.
    """
    if not isinstance(verdict, dict):
        return False
    if verdict.get("result") != "GREEN":
        return False
    if not isinstance(session_id, str) or verdict.get("session_id") != session_id:
        return False
    if type(dispatch_generation) is not int or dispatch_generation < 0:
        return False
    if verdict.get("dispatch_generation") != dispatch_generation:
        return False
    try:
        verdict_ts = float(verdict.get("ts", 0) or 0)
        dispatch_ts = float(last_dispatch_ts or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(verdict_ts) or not math.isfinite(dispatch_ts):
        return False
    if verdict.get("unsafe") is True:
        return False
    if not is_snapshot_bound_verdict(verdict):
        return False
    if verdict.get("snapshot_stable") is not True:
        return False
    if verdict.get("workspace_supported") is not True:
        return False
    try:
        if int(verdict.get("exit_code", 1)) != 0:
            return False
    except (TypeError, ValueError):
        return False

    recorded = verdict.get("verified_snapshot") or verdict.get("final_snapshot")
    if not isinstance(recorded, dict):
        return False
    if recorded.get("git_worktree") is not True or recorded.get("snapshot_complete") is not True:
        return False
    if not isinstance(approved_gate, dict):
        return False
    approved_argv = approved_gate.get("argv")
    approved_cwd = approved_gate.get("cwd")
    if not isinstance(approved_argv, list) or not approved_argv or not isinstance(approved_cwd, str):
        return False
    try:
        approved_root = str(Path(approved_cwd).resolve())
    except OSError:
        return False

    root = cwd or approved_root
    try:
        root = str(Path(root).resolve())
    except OSError:
        root = str(root)
    if root != approved_root:
        return False
    try:
        recorded_root = str(Path(str(recorded.get("workspace_root") or "")).resolve())
    except OSError:
        return False
    if recorded_root != approved_root:
        return False

    gate_mode = str(verdict.get("gate_mode") or recorded.get("gate_mode") or "argv")
    gate_argv = verdict.get("gate_argv")
    if not isinstance(gate_argv, list):
        gate_argv = recorded.get("gate_argv") if isinstance(recorded.get("gate_argv"), list) else []
        if not gate_argv and isinstance(verdict.get("gate"), str):
            try:
                gate_argv = parse_gate_argv(verdict["gate"]) if gate_mode == "argv" else []
            except ValueError:
                gate_argv = []
    if gate_mode != "argv" or list(gate_argv or []) != list(approved_argv):
        return False
    if recorded.get("gate_identity") != _gate_identity(list(approved_argv), "argv"):
        return False

    current = capture_workspace_snapshot(
        approved_root,
        session_id=session_id,
        gate_argv=list(approved_argv),
        gate_mode="argv",
    )
    return snapshots_equal(recorded, current)


def run_gate(
    gate: str,
    session_id: str = "default",
    cwd: str = ".",
    *,
    legacy_shell: bool = False,
) -> dict:
    """Run the acceptance command, capture its exit code, stamp a snapshot-bound verdict.

    Default execution is argv (shell=False) via shlex.split. The explicitly named
    ``legacy_shell=True`` path uses shell=True and marks the verdict unsafe so it can
    never close a hardened Stop gate.
    """
    cwd = str(Path(cwd).resolve())
    verification_id = f"verify-{os.getpid()}-{time.time_ns()}"
    start_state = fc.mark_verification_started(
        session_id,
        verification_id,
        Path(cwd, "verdict.json"),
    )
    try:
        return _run_active_gate(
            gate,
            session_id=session_id,
            cwd=cwd,
            legacy_shell=legacy_shell,
            verification_id=verification_id,
            start_state=start_state,
        )
    finally:
        fc.abandon_verification(session_id, verification_id)


def _run_active_gate(
    gate: str,
    session_id: str,
    cwd: str,
    *,
    legacy_shell: bool,
    verification_id: str,
    start_state: dict,
) -> dict:
    cwd = str(Path(cwd).resolve())
    gate = gate if isinstance(gate, str) else str(gate)
    start_state_revision = start_state.get("state_revision", 0)
    verified_generation = start_state.get("dispatch_generation", 0)
    started_with_active_dispatch = bool(start_state.get("active_dispatches"))

    unsafe = bool(legacy_shell)
    unsafe_reason = "legacy_shell=True (shell=True compatibility path)" if unsafe else ""
    gate_mode = "legacy_shell" if unsafe else "argv"
    gate_argv: list[str] | None

    if unsafe:
        gate_argv = None  # not a true argv vector under shell
        identity_argv: list[str] = ["__legacy_shell__", gate]
    else:
        try:
            gate_argv = parse_gate_argv(gate)
        except ValueError:
            # Empty / unparseable gate: treat as hard failure without shell fallback.
            gate_argv = []
            identity_argv = []
            exit_code, stdout, stderr = 127, "", "empty or unparseable gate command"
            with fc.advisory_lock(fc.config_lock_path(fc.GLOBAL_CONFIG)):
                pre = capture_workspace_snapshot(
                    cwd, session_id=session_id, gate_argv=identity_argv, gate_mode=gate_mode
                )
                final = capture_workspace_snapshot(
                    cwd, session_id=session_id, gate_argv=identity_argv, gate_mode=gate_mode
                )
                return _finalize_verdict(
                    gate=gate,
                    gate_argv=gate_argv,
                    gate_mode=gate_mode,
                    unsafe=False,
                    unsafe_reason="",
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    session_id=session_id,
                    cwd=cwd,
                    pre=pre,
                    final=final,
                    verified_generation=verified_generation,
                    started_with_active_dispatch=started_with_active_dispatch,
                    expected_state_revision=start_state_revision,
                    verification_id=verification_id,
                )
        identity_argv = list(gate_argv)

    with fc.advisory_lock(fc.config_lock_path(fc.GLOBAL_CONFIG)):
        pre = capture_workspace_snapshot(
            cwd, session_id=session_id, gate_argv=identity_argv, gate_mode=gate_mode
        )

    if unsafe:
        exit_code, stdout, stderr = _run_gate_legacy_shell(gate, cwd)
    else:
        assert gate_argv is not None
        exit_code, stdout, stderr = _run_gate_argv(gate_argv, cwd)

    with fc.advisory_lock(fc.config_lock_path(fc.GLOBAL_CONFIG)):
        final = capture_workspace_snapshot(
            cwd, session_id=session_id, gate_argv=identity_argv, gate_mode=gate_mode
        )

        return _finalize_verdict(
            gate=gate,
            gate_argv=gate_argv if gate_argv is not None else identity_argv,
            gate_mode=gate_mode,
            unsafe=unsafe,
            unsafe_reason=unsafe_reason,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            session_id=session_id,
            cwd=cwd,
            pre=pre,
            final=final,
            verified_generation=verified_generation,
            started_with_active_dispatch=started_with_active_dispatch,
            expected_state_revision=start_state_revision,
            verification_id=verification_id,
        )


def _finalize_verdict(
    *,
    gate: str,
    gate_argv: list[str] | None,
    gate_mode: str,
    unsafe: bool,
    unsafe_reason: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    session_id: str,
    cwd: str,
    pre: dict[str, Any],
    final: dict[str, Any],
    verified_generation: int,
    started_with_active_dispatch: bool,
    expected_state_revision: int,
    verification_id: str,
) -> dict:
    stable = snapshots_equal(pre, final)
    workspace_supported = (
        pre.get("git_worktree") is True
        and final.get("git_worktree") is True
        and pre.get("snapshot_complete") is True
        and final.get("snapshot_complete") is True
    )
    state = fc.read_state(session_id)
    after = float(state.get("last_dispatch_ts", 0.0))
    generation_unchanged = state.get("dispatch_generation") == verified_generation
    state_unchanged = state.get("state_revision", 0) == expected_state_revision
    no_active_dispatch = not started_with_active_dispatch and not state.get("active_dispatches")
    sole_verifier = state.get("active_verifications") == [verification_id]

    # GREEN requires exit zero, a stable verified snapshot, and no overlapping dispatch generation.
    # Unsafe legacy-shell runs may still stamp result=GREEN when exit==0 and stable, but they
    # carry unsafe=True so the hardened Stop gate always rejects them.
    if (
        int(exit_code) == 0
        and stable
        and workspace_supported
        and generation_unchanged
        and state_unchanged
        and no_active_dispatch
        and sole_verifier
    ):
        result = "GREEN"
    else:
        result = "RED"

    # Preserve legacy keys via make_verdict then overlay snapshot-bound fields / corrected result.
    base = fc.make_verdict(
        gate,
        exit_code,
        final.get("diff_sha", "") or final.get("unstaged_diff_sha", ""),
        list(final.get("paths") or []),
        ts=time.time(),
        after_dispatch_ts=after,
    )
    verdict = dict(base)
    verdict["result"] = result
    verdict["schema_version"] = VERDICT_SCHEMA_VERSION
    verdict["dispatch_generation"] = verified_generation
    verdict["verification_id"] = verification_id
    verdict["session_id"] = session_id
    verdict["gate_argv"] = list(gate_argv or [])
    verdict["gate_mode"] = gate_mode
    verdict["unsafe"] = bool(unsafe)
    verdict["unsafe_reason"] = unsafe_reason or ""
    verdict["snapshot_stable"] = bool(stable)
    verdict["workspace_supported"] = bool(workspace_supported)
    verdict["pre_gate_snapshot"] = pre
    verdict["final_snapshot"] = final
    # verified_snapshot is set only for a stable, supported workspace (including unsafe
    # legacy-shell, which Stop still rejects via the unsafe marker).
    verdict["verified_snapshot"] = final if result == "GREEN" else None
    verdict["stdout_tail"] = (stdout or "")[-2000:]
    verdict["stderr_tail"] = (stderr or "")[-1000:]

    state_verdict = _state_verdict_for_persistence(verdict)
    artifact_verdict = fc.compact_verdict_receipt(verdict)
    persisted, _current = fc.finish_verification(
        session_id,
        verification_id,
        expected_state_revision,
        verified_generation,
        state_verdict,
        Path(cwd, "verdict.json"),
        artifact_verdict,
    )
    if not persisted:
        verdict["result"] = "RED"
        verdict["verified_snapshot"] = None
        concurrency_error = "session state changed before verdict persistence"
        verdict["stderr_tail"] = "\n".join(
            part for part in (verdict["stderr_tail"], concurrency_error) if part
        )[-1000:]
    return verdict


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a deterministic acceptance gate and stamp a snapshot-bound verdict."
    )
    ap.add_argument("--gate", required=True, help='acceptance command, e.g. "pytest -q"')
    ap.add_argument("--session", default=os.environ.get("FRONTIER_SESSION_ID", "default"))
    ap.add_argument("--cwd", default=".")
    ap.add_argument(
        "--legacy-shell",
        action="store_true",
        help=(
            "UNSAFE compatibility path: run --gate via shell=True. Verdict is marked unsafe "
            "and cannot satisfy the hardened Stop gate."
        ),
    )
    args = ap.parse_args(argv)
    env_legacy = str(os.environ.get("FRONTIER_VERIFY_LEGACY_SHELL") or "").strip().lower()
    legacy = bool(args.legacy_shell) or env_legacy in {"1", "true", "yes", "on", "y"}
    v = run_gate(args.gate, session_id=args.session, cwd=args.cwd, legacy_shell=legacy)
    print(
        json.dumps(
            {
                k: v[k]
                for k in (
                    "result",
                    "gate",
                    "exit_code",
                    "diff_sha",
                    "paths",
                    "ts",
                    "schema_version",
                    "unsafe",
                    "snapshot_stable",
                    "workspace_supported",
                )
                if k in v
            },
            indent=2,
        )
    )
    return 0 if v["result"] == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
