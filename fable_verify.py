#!/usr/bin/env python3
"""fable_verify.py — deterministic gate runner for FableFuse orchestrator mode.

The brain (Fable) never closes a loop on a prose "GREEN". It must run a real EXTERNAL gate
(tests / build / lint / repro) through this module. The gate's exit code — not a model's
opinion — decides GREEN/RED. The verdict is written to verdict.json and into the session
state so the Stop hook can enforce it.

stdlib-only, Python 3.10+, importable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fable_common as fc

GATE_TIMEOUT = int(os.environ.get("FABLE_GATE_TIMEOUT", "600"))


def _git(args: list[str], cwd: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _diff_fingerprint(cwd: str) -> tuple[str, list[str]]:
    """Best-effort: sha256 of the working-tree diff + list of changed paths.
    Empty (``""``, ``[]``) when cwd is not a git repo — the verdict is still valid."""
    diff = _git(["diff", "--no-color"], cwd)
    names = _git(["diff", "--name-only"], cwd)
    paths = [ln.strip() for ln in names.splitlines() if ln.strip()]
    sha = hashlib.sha256(diff.encode()).hexdigest() if diff else ""
    return sha, paths


def run_gate(gate: str, session_id: str = "default", cwd: str = ".") -> dict:
    """Run the acceptance command, capture its exit code, stamp a deterministic verdict."""
    cwd = str(Path(cwd).resolve())
    try:
        proc = subprocess.run(gate, shell=True, cwd=cwd, capture_output=True, text=True,
                              timeout=GATE_TIMEOUT)
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 124, "", f"gate timed out after {GATE_TIMEOUT}s"

    diff_sha, paths = _diff_fingerprint(cwd)
    after = float(fc.read_state(session_id).get("last_dispatch_ts", 0.0))
    verdict = fc.make_verdict(gate, exit_code, diff_sha, paths, ts=time.time(), after_dispatch_ts=after)
    verdict["stdout_tail"] = (stdout or "")[-2000:]
    verdict["stderr_tail"] = (stderr or "")[-1000:]

    verdict_path = Path(cwd, "verdict.json")
    verdict_path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    try:
        verdict_path.chmod(0o600)  # may contain gate stdout/stderr (test output, secrets, etc.)
    except OSError:
        pass
    fc.write_state(session_id, verdict={k: verdict[k] for k in
                                        ("result", "gate", "exit_code", "diff_sha", "paths",
                                         "ts", "after_dispatch_ts")})
    return verdict


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a deterministic acceptance gate and stamp a verdict.")
    ap.add_argument("--gate", required=True, help='acceptance command, e.g. "pytest -q"')
    ap.add_argument("--session", default=os.environ.get("FABLE_SESSION_ID", "default"))
    ap.add_argument("--cwd", default=".")
    args = ap.parse_args(argv)
    v = run_gate(args.gate, session_id=args.session, cwd=args.cwd)
    print(json.dumps({k: v[k] for k in ("result", "gate", "exit_code", "diff_sha", "paths", "ts")},
                     indent=2))
    return 0 if v["result"] == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
