#!/usr/bin/env python3
"""fable_advisor.py — advisor-mode core for FableFuse.

In advisor mode (default), the selected lead/executor is the main loop and consults Fable ON-DEMAND
via ``ask_fable``. Fable gives concise, actionable guidance; the executor does the work.

stdlib-only, Python 3.10+, importable for offline contract tests.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fable_common as fc

# Pass advice through whole unless output exceeds this (default: 8× bounded handoff size).
_ADVICE_WHOLE_LIMIT = int(os.environ.get("FABLE_ADVICE_WHOLE_LIMIT", str(fc.MAX_RETURN_CHARS * 8)))


def _lead_description(cfg: dict) -> str:
    executor = (cfg.get("executor") or "codex").lower()
    if executor == "opus":
        return f"Opus ({cfg.get('opus_model') or 'claude-opus-4-8'})"
    if executor == "sonnet":
        return f"Sonnet ({cfg.get('sonnet_model') or 'claude-sonnet-5'})"
    if executor == "grok":
        return f"Grok ({cfg.get('grok_model') or 'grok-4.5'})"
    return "Codex"


def _build_advisor_prompt(question: str, context: str, lead: str = "the selected executor/lead") -> str:
    """Frame Fable as an on-demand advisor to an executor, not the worker."""
    parts = [
        "You are Fable, the ON-DEMAND ADVISOR in a FableFuse session.",
        f"{lead} is the EXECUTOR/LEAD (BODY) — it performs all implementation, tool use, and execution.",
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


def ask_fable(
    question: str,
    context: str = "",
    timeout: int = 180,
    session_id: str | None = None,
) -> dict:
    """Consult Fable on-demand. Returns {ok, advice, model, note}."""
    cfg = fc.resolve_config(session_id=session_id)
    cmd = fc.build_fable_command(cfg)
    prompt = _build_advisor_prompt(question, context, _lead_description(cfg))
    rc, stdout, stderr = fc.run_engine(cmd, prompt, timeout=timeout)

    if rc == 0:
        return {
            "ok": True,
            "advice": _normalize_advice(stdout),
            "model": cfg.get("fable_model") or "claude-fable-5",
            "note": "",
        }

    note = stderr or stdout or f"engine exit {rc}"
    return {
        "ok": False,
        "advice": "",
        "model": cfg.get("fable_model") or "claude-fable-5",
        "note": note,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consult Fable (on-demand advisor) for actionable guidance to an executor.",
    )
    parser.add_argument("question", help="Question or decision point for the advisor")
    parser.add_argument("--context", default="", help="Optional background context for the advisor")
    parser.add_argument("--session", default=None, help="Session id for per-session config overrides")
    parser.add_argument("--timeout", type=int, default=180, help="Engine timeout in seconds (default: 180)")
    args = parser.parse_args(argv)

    result = ask_fable(args.question, context=args.context, timeout=args.timeout, session_id=args.session)
    if result["ok"]:
        print(result["advice"])
        return 0
    print(result["note"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
