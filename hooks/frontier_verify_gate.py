#!/usr/bin/env python3
"""frontier_verify_gate.py - Stop verifier guardrail for FrontierFuse orchestrator mode.

Inert unless the session is armed and guards are on. When armed, it blocks the session from
finishing until a fresh snapshot-bound GREEN verdict exists - one produced by the command the host
froze with `frontier-dispatch arm --gate "<command>"`, then run via `frontier-dispatch verify`,
stamped AFTER the last dispatch, with a stable workspace snapshot that still matches a live
recompute. A prose "GREEN" from the
brain, a legacy (pre-snapshot) verdict, or an unsafe legacy-shell verdict can never close the
loop. Blocks via exit code 2 (Claude Code Stop convention), honouring stop_hook_active to avoid
infinite loops.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frontier_common as fc
import frontier_verify as fv

MSG = (
    "FrontierFuse verifier guardrail: no fresh snapshot-bound GREEN verdict for the last dispatch. "
    "Run the host-frozen external gate with `frontier-dispatch verify`, then finish only when it "
    "passes (GREEN, stamped after the last dispatch, workspace snapshot "
    "stable and still matching). Legacy or unsafe shell verdicts cannot close the loop. "
    "Verify against the raw diff + gate stdout, not summary cards. "
    "Kill-switch: FRONTIER_GUARDS_OFF=1."
)


def _completion_allowed(sid: str) -> tuple[bool, str]:
    state_file = fc.state_path(sid)
    initial = fc.read_state(sid)
    if not initial.get("armed"):
        return True, ""
    with fc.advisory_lock(fc.config_lock_path(fc.GLOBAL_CONFIG)):
        with fc.advisory_lock(fc.config_lock_path(state_file)):
            st = fc.read_state(sid)
            if not st.get("armed"):
                return True, ""
            if st.get("active_dispatches"):
                return False, (
                    "FrontierFuse verifier guardrail: an executor dispatch is still active. Wait for "
                    "all dispatches to finish, then run `frontier-dispatch verify` again."
                )
            if st.get("active_verifications"):
                return False, (
                    "FrontierFuse verifier guardrail: verification is still active. Wait for it to "
                    "finish and publish a receipt before closing."
                )
            initial_revision = st.get("state_revision", 0)
            verdict = st.get("verdict")
            approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else None
            fresh_green = fv.verdict_is_snapshot_fresh_green(
                verdict,
                st.get("last_dispatch_ts", 0),
                st.get("dispatch_generation", 0),
                session_id=sid,
                cwd=approved.get("cwd") if isinstance(approved, dict) else None,
                approved_gate=approved,
            )
            latest = fc.read_state(sid)
            fresh_green = bool(
                fresh_green
                and latest.get("state_revision", 0) == initial_revision
                and latest.get("verdict") == verdict
                and not latest.get("active_dispatches")
                and not latest.get("active_verifications")
            )
            if fresh_green:
                fc.mark_completion_pending_locked(state_file, latest)
            return fresh_green, "" if fresh_green else MSG


def main() -> None:
    if fc.guards_off():
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.stderr.write("FrontierFuse verifier guardrail: invalid hook input; refusing to close.")
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write(
            "FrontierFuse verifier guardrail: invalid hook input; expected a JSON object."
        )
        sys.exit(2)
    stop_hook_active = data.get("stop_hook_active", False)
    if not isinstance(stop_hook_active, bool):
        sys.stderr.write(
            "FrontierFuse verifier guardrail: invalid hook input; stop_hook_active must be boolean."
        )
        sys.exit(2)
    if stop_hook_active:
        sys.exit(0)  # already nudged once this stop cycle — don't loop forever
    raw_sid = data.get("session_id")
    if raw_sid is not None and not fc.session_id_is_valid(raw_sid):
        sys.stderr.write(
            "FrontierFuse verifier guardrail: invalid hook input; session_id must be a string."
        )
        sys.exit(2)
    sid = raw_sid or "default"
    try:
        allowed, message = _completion_allowed(sid)
    except fc.StateFileError:
        sys.stderr.write(
            "FrontierFuse verifier guardrail: session state is invalid. Preserve it and run "
            "`frontier-dispatch config --repair` from the host before continuing."
        )
        sys.exit(2)
    except Exception:
        sys.stderr.write(
            "FrontierFuse verifier guardrail: completion could not be validated safely. Inspect "
            "configuration and session state from the host, repair if needed, then re-arm and "
            "verify again."
        )
        sys.exit(2)
    if allowed:
        sys.exit(0)

    sys.stderr.write(message)
    sys.exit(2)


if __name__ == "__main__":
    main()
