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


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if fc.guards_off():
        sys.exit(0)
    if data.get("stop_hook_active"):
        sys.exit(0)  # already nudged once this stop cycle — don't loop forever
    sid = data.get("session_id") or "default"
    st = fc.read_state(sid)
    if not st.get("armed"):
        sys.exit(0)

    verdict = st.get("verdict")
    last_ts = st.get("last_dispatch_ts", 0)
    approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else None
    cwd = approved.get("cwd") if isinstance(approved, dict) else None

    if fv.verdict_is_snapshot_fresh_green(
        verdict,
        last_ts,
        session_id=sid,
        cwd=cwd,
        approved_gate=approved,
    ):
        sys.exit(0)

    sys.stderr.write(MSG)
    sys.exit(2)


if __name__ == "__main__":
    main()
