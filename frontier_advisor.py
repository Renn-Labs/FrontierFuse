#!/usr/bin/env python3
"""frontier_advisor.py - advisor-mode core for FrontierFuse.

In advisor mode (default), the selected executor is the main loop and consults the configured
frontier model ON-DEMAND via ``ask_frontier``. The frontier model is a managed consult only: the
host-bound harness and selected executor remain the session lead. Selecting a frontier model does
not hot-swap the host. The frontier model gives concise guidance; the executor does the work.

stdlib-only, Python 3.10+, importable for offline contract tests.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontier_common as fc

# Pass advice through whole unless output exceeds this (default: 8× bounded handoff size).
_ADVICE_WHOLE_LIMIT = int(os.environ.get("FRONTIER_ADVICE_WHOLE_LIMIT", str(fc.MAX_RETURN_CHARS * 8)))


def _lead_description(cfg: dict) -> str:
    executor = (cfg.get("executor") or "codex").lower()
    if executor == "claude":
        return f"Claude ({cfg.get('claude_model') or 'claude-sonnet-5'})"
    if executor == "grok":
        return f"Grok ({cfg.get('grok_model') or 'grok-4.5'})"
    if executor == "gemini":
        return f"Gemini ({cfg.get('gemini_model') or 'gemini-3.5-flash'})"
    model = cfg.get("codex_model") or "account default"
    return f"Codex ({model})"


def _build_advisor_prompt(question: str, context: str, lead: str = "the selected executor") -> str:
    """Frame the frontier model as an on-demand advisor, not the worker."""
    parts = [
        "You are the ON-DEMAND FRONTIER ADVISOR in a FrontierFuse session.",
        f"{lead} is the EXECUTOR (BODY) - it performs all implementation, tool use, and execution.",
        "Your role is ADVISOR ONLY: give concise, actionable guidance so the executor can succeed.",
        "Do NOT do the work yourself. Do NOT produce full implementations unless a tiny snippet clarifies.",
        "Prefer: decision rationale, risks, next steps, verification hints, and what to avoid.",
        "",
        "## Question",
        (question or "").strip(),
    ]
    ctx = (context or "").strip()
    if ctx:
        parts.extend(["", "## Context", ctx])
    parts.extend(["", "## Response", "Give direct, actionable advice:"])
    return "\n".join(parts)


def _normalize_advice(raw: str) -> str:
    """Return advice mostly whole; summarize only when extremely long."""
    text = (raw or "").strip()
    if not text:
        return ""
    if len(text) <= _ADVICE_WHOLE_LIMIT:
        return text
    return fc.extractive_summary(text, _ADVICE_WHOLE_LIMIT)


def ask_frontier(
    question: str,
    context: str = "",
    timeout: int = 180,
    session_id: str | None = None,
) -> dict:
    """Consult the configured frontier model on-demand. Returns {ok, advice, model, note}.

    ``model`` is the effective frontier model selected by ``build_frontier_command`` policy
    (via ``effective_frontier_model``), not a hard-wired Claude Fable ID.
    """
    cfg = fc.resolve_config(session_id=session_id)
    cmd = fc.build_frontier_command(cfg)
    effective_model = fc.effective_frontier_model(cfg)
    prompt = _build_advisor_prompt(question, context, _lead_description(cfg))
    rc, stdout, stderr = fc.run_engine(cmd, prompt, timeout=timeout)

    if rc == 0:
        return {
            "ok": True,
            "advice": _normalize_advice(stdout),
            "model": effective_model,
            "note": "",
        }

    note = stderr or stdout or f"engine exit {rc}"
    return {
        "ok": False,
        "advice": "",
        "model": effective_model,
        "note": note,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consult the configured frontier model for actionable guidance to an executor.",
    )
    parser.add_argument("question", help="Question or decision point for the advisor")
    parser.add_argument("--context", default="", help="Optional background context for the advisor")
    parser.add_argument("--session", default=None, help="Session id for per-session config overrides")
    parser.add_argument("--timeout", type=int, default=180, help="Engine timeout in seconds (default: 180)")
    args = parser.parse_args(argv)

    result = ask_frontier(args.question, context=args.context, timeout=args.timeout, session_id=args.session)
    if result["ok"]:
        print(result["advice"])
        return 0
    print(result["note"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
