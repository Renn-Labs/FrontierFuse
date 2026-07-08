#!/usr/bin/env python3
"""fable_advisor_mcp.py — stdio MCP server exposing `ask_fable`.

This is the advisor-mode primitive: it lets a Codex/Sonnet/Opus (or any) executor main loop consult Fable 5
ON-DEMAND. The executor runs every turn and does the work; it calls `ask_fable` only when guidance
materially helps, so most tokens stay at the cheaper executor rate (the ClaudeDevs advisor pattern).

Register with an executor harness, e.g. Codex:
  codex mcp add fable-advisor -- python3 /abs/path/fable_advisor_mcp.py

Minimal JSON-RPC 2.0 over newline-delimited stdio. stdlib-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fable_advisor

PROTO = "2025-06-18"
INSTRUCTIONS = (
    "Consult Fable 5 (the on-demand ADVISOR) for planning, hard design decisions, architecture "
    "tradeoffs, and independent verification. YOU (the selected lead/executor) run the main loop and do all the "
    "work — call ask_fable only when guidance materially helps, keeping most tokens at the cheaper "
    "executor rate. Fable advises; it does not execute."
)
TOOLS = [{
    "name": "ask_fable",
    "description": ("Consult Fable 5 (on-demand advisor) for concise, actionable guidance to the "
                    "executor: planning, hard decisions, architecture tradeoffs, or independent "
                    "review. Not for doing the work — you remain the executor."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "a focused question or decision point"},
            "context": {"type": "string", "description": "minimal decision-relevant context (paste summaries, not transcripts)"},
        },
        "required": ["question"],
    },
}]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _ask(args: dict) -> str:
    r = fable_advisor.ask_fable(args.get("question", ""), context=args.get("context", ""))
    return r.get("advice") or f"(advisor unavailable: {r.get('note')})"


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": {"protocolVersion": PROTO, "capabilities": {"tools": {}},
                              "instructions": INSTRUCTIONS,
                              "serverInfo": {"name": "fable-advisor", "version": "0.1.0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") != "ask_fable":
                _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool"}})
                continue
            try:
                text = _ask(params.get("arguments", {}))
                _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as e:
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not found"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
