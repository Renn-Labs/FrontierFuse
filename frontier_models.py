#!/usr/bin/env python3
"""Source-backed model catalog and local CLI discovery for FrontierFuse."""
from __future__ import annotations

import shutil
import subprocess

SOURCES = {
    "codex": "https://developers.openai.com/api/docs/models/all",
    "claude": "https://platform.claude.com/docs/en/about-claude/models/overview",
    "grok": "local `grok models` plus official xAI/Grok Build availability",
    "gemini": "https://ai.google.dev/gemini-api/docs/models",
}

# Curated for coding-agent use, not an exhaustive provider inventory.
CATALOG = {
    "codex": [
        ("", "Account-aware CLI default", "recommended"),
        ("gpt-5.6-sol", "highest-capability GPT-5.6", "current"),
        ("gpt-5.6-terra", "balanced GPT-5.6", "current"),
        ("gpt-5.6-luna", "lower-cost GPT-5.6", "current"),
        ("gpt-5.5", "coding and professional work", "current"),
        ("gpt-5.4", "affordable coding and professional work", "current"),
        ("gpt-5.4-mini", "subagents and lower-cost coding", "current"),
        ("gpt-5.3-codex", "agentic coding", "previous"),
        ("gpt-5.2", "previous frontier model", "previous"),
        ("gpt-5.1", "previous coding and agentic model", "previous"),
        ("gpt-5", "previous intelligent reasoning model", "previous"),
        ("gpt-5-mini", "lower-cost GPT-5", "previous"),
    ],
    "claude": [
        ("claude-fable-5", "highest-capability Claude frontier", "recommended"),
        ("claude-opus-4-8", "complex agentic coding", "current"),
        ("claude-sonnet-5", "speed/intelligence balance", "current"),
        ("claude-sonnet-4-6", "previous Sonnet generation", "previous"),
        ("claude-opus-4-7", "previous Opus generation", "previous"),
        ("claude-opus-4-6", "older Opus generation", "previous"),
        ("claude-haiku-4-5", "fast, economical Claude", "current"),
    ],
    "grok": [
        ("grok-4.5", "Grok Build default", "recommended"),
    ],
    "gemini": [
        ("gemini-3.5-flash", "stable agentic and coding model", "recommended"),
        ("gemini-3.1-pro-preview", "complex reasoning and coding", "preview"),
        ("gemini-3.1-flash-lite", "stable low-latency model", "current"),
        ("gemini-2.5-pro", "previous complex reasoning model", "previous"),
        ("gemini-2.5-flash", "previous price/performance model", "previous"),
        ("gemini-2.5-flash-lite", "previous low-cost model", "previous"),
    ],
}

PROVIDERS = frozenset(CATALOG)


def discover_local_models(provider: str, timeout: float = 3.0) -> list[str]:
    """Return entitlement-aware model IDs exposed by a provider CLI, when supported."""
    if provider != "grok" or not shutil.which("grok"):
        return []
    try:
        proc = subprocess.run(
            ["grok", "models"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: list[str] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if line.startswith(("* ", "- ")):
            model = line[2:].split(" ", 1)[0].strip()
            if model and model not in found:
                found.append(model)
    return found


def models_for(provider: str, *, discover: bool = True) -> list[dict]:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    rows = [
        {"id": model, "description": description, "status": status, "source": "catalog"}
        for model, description, status in CATALOG[provider]
    ]
    if discover:
        known = {row["id"] for row in rows}
        for model in discover_local_models(provider):
            if model not in known:
                rows.append(
                    {
                        "id": model,
                        "description": "available to the installed CLI/account",
                        "status": "local",
                        "source": "cli",
                    }
                )
    return rows
