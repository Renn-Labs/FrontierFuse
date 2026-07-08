#!/usr/bin/env python3
"""fable_verify_gate.py — Stop verifier gate for FableFuse orchestrator mode.

Inert unless the session is armed and guards are on. When armed, it blocks the session from
finishing until a fresh GREEN verdict exists — one produced by a real external gate
(`fable-dispatch verify --gate …`) and stamped AFTER the last dispatch. A prose "GREEN" from the
brain can never close the loop; only the deterministic verdict.json can. Blocks via exit code 2
(Claude Code Stop convention), honouring stop_hook_active to avoid infinite loops.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fable_common as fc

MSG = ("FableFuse verifier gate: no fresh GREEN verdict for the last dispatch. Run a real external "
       "gate — `fable-dispatch verify --gate \"<tests/build/lint/repro>\"` — and finish only when it "
       "passes (GREEN, stamped after the last dispatch). Verify against the raw diff + gate stdout, "
       "not summary cards. Kill-switch: FABLE_GUARDS_OFF=1.")


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
    if fc.verdict_is_fresh_green(st.get("verdict"), st.get("last_dispatch_ts", 0)):
        sys.exit(0)
    sys.stderr.write(MSG)
    sys.exit(2)


if __name__ == "__main__":
    main()
