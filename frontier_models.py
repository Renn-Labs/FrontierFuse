#!/usr/bin/env python3
"""Source-backed model catalog and local CLI discovery for FrontierFuse."""
from __future__ import annotations

import re
import shutil
import subprocess

SOURCES = {
    "codex": "https://developers.openai.com/api/docs/models/all; local `codex models`",
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
# Only providers whose installed CLI exposes a local model list we can parse offline.
DISCOVERY_SUPPORTED = frozenset({"codex", "grok"})
_PROVIDER_CLI = {
    "codex": "codex",
    "grok": "grok",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_GROK_BULLET_RE = re.compile(r"^[\*\-\u2022\u00b7]\s+")
_CODEX_SECTION_STOP_RE = re.compile(
    r"^(Reasoning effort|Launcher examples|Usage|Options|Commands)\b",
    re.IGNORECASE,
)
_NON_MODEL_TOKENS = frozenset({
    "alias", "default", "model", "models", "available", "reasoning", "effort",
    "none", "low", "medium", "high", "xhigh", "max", "flagship", "balanced",
    "efficient", "examples",
})


def _discovery_result(
    *,
    supported: bool,
    attempted: bool,
    succeeded: bool,
    discovered_ids: list[str] | None = None,
    error_class: str | None = None,
) -> dict:
    return {
        "supported": supported,
        "attempted": attempted,
        "succeeded": succeeded,
        "discovered_ids": list(discovered_ids or []),
        "error_class": error_class,
    }


def _clean_line(raw: str) -> str:
    return _ANSI_RE.sub("", raw or "").strip()


def _valid_model_id(token: str) -> bool:
    if not token or token in _NON_MODEL_TOKENS:
        return False
    if token.lower() in _NON_MODEL_TOKENS:
        return False
    if not _MODEL_ID_RE.match(token):
        return False
    # Reject pure effort words / single-char noise that can appear in free text.
    if token.isdigit():
        return False
    return True


def _strip_model_token(token: str) -> str:
    token = (token or "").strip().strip("*,•·")
    if "(" in token:
        token = token.split("(", 1)[0].strip()
    return token.strip("[]{}(),;:\"'`")


def _parse_grok_models(stdout: str) -> list[str] | None:
    """Parse `grok models` stdout. Return None when the listing looks malformed."""
    found: list[str] = []
    saw_listing = False
    for raw in (stdout or "").splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("available models"):
            saw_listing = True
            continue
        if lower.startswith("default model:"):
            saw_listing = True
            continue
        body = line
        if _GROK_BULLET_RE.match(body):
            body = _GROK_BULLET_RE.sub("", body).strip()
            saw_listing = True
        elif body.startswith(("* ", "- ", "• ", "· ")):
            body = body[2:].strip()
            saw_listing = True
        else:
            # Non-bullet prose (login banner, etc.) is ignored.
            continue
        token = _strip_model_token(body.split()[0] if body.split() else "")
        if _valid_model_id(token) and token not in found:
            found.append(token)
    if found:
        return found
    # Exit 0 with no parseable model IDs is treated as malformed so callers get no IDs.
    if saw_listing or (stdout or "").strip():
        return None
    return []


def _parse_codex_models(stdout: str) -> list[str] | None:
    """Parse `codex models` stdout. Return None when the listing looks malformed."""
    found: list[str] = []
    in_models = False
    saw_header = False
    for raw in (stdout or "").splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        if _CODEX_SECTION_STOP_RE.match(line):
            if in_models:
                break
            continue
        lower = line.lower()
        if "models" in lower and lower.rstrip().endswith(":"):
            saw_header = True
            in_models = True
            continue
        if not in_models:
            # Accept lightly indented model rows even without a header when they look right.
            if line[:1].isspace() or raw[:1].isspace():
                in_models = True
            else:
                continue
        # Prefer original leading-space model rows; skip free-prose lines without indent
        # once we have started, unless they still look like a bare model id.
        token = _strip_model_token(line.split()[0] if line.split() else "")
        if token.lower() == "alias":
            continue
        if _valid_model_id(token) and token not in found:
            found.append(token)
    if found:
        return found
    if saw_header or (stdout or "").strip():
        return None
    return []


def discover_models(provider: str, *, timeout: float = 3.0, attempt: bool = True) -> dict:
    """Discover local model IDs via the installed provider CLI, with metadata.

    Runs the provider's local `codex models` or `grok models` command when present.
    This process does not directly probe authentication, network, or entitlement, but
    the provider CLI may use its own authentication or network behavior. Its output is
    informational only, not an entitlement guarantee. Unsupported providers report
    supported=False without inventing IDs.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    if provider not in DISCOVERY_SUPPORTED:
        return _discovery_result(
            supported=False,
            attempted=False,
            succeeded=False,
            discovered_ids=[],
            error_class=None,
        )
    if not attempt:
        return _discovery_result(
            supported=True,
            attempted=False,
            succeeded=False,
            discovered_ids=[],
            error_class=None,
        )

    cli = _PROVIDER_CLI[provider]
    if not shutil.which(cli):
        return _discovery_result(
            supported=True,
            attempted=True,
            succeeded=False,
            discovered_ids=[],
            error_class="cli_missing",
        )

    try:
        proc = subprocess.run(
            [cli, "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _discovery_result(
            supported=True,
            attempted=True,
            succeeded=False,
            discovered_ids=[],
            error_class="timeout",
        )
    except OSError:
        return _discovery_result(
            supported=True,
            attempted=True,
            succeeded=False,
            discovered_ids=[],
            error_class="os_error",
        )

    if proc.returncode != 0:
        return _discovery_result(
            supported=True,
            attempted=True,
            succeeded=False,
            discovered_ids=[],
            error_class="nonzero_exit",
        )

    if provider == "grok":
        parsed = _parse_grok_models(proc.stdout or "")
    else:
        parsed = _parse_codex_models(proc.stdout or "")

    if parsed is None:
        return _discovery_result(
            supported=True,
            attempted=True,
            succeeded=False,
            discovered_ids=[],
            error_class="malformed_output",
        )

    return _discovery_result(
        supported=True,
        attempted=True,
        succeeded=True,
        discovered_ids=parsed,
        error_class=None,
    )


def discover_local_models(provider: str, timeout: float = 3.0) -> list[str]:
    """Return model IDs reported by a provider CLI, when supported.

    Convenience wrapper over discover_models(...). On any failure or unsupported
    provider, returns an empty list (never invents IDs).
    """
    if provider not in PROVIDERS:
        return []
    result = discover_models(provider, timeout=timeout, attempt=True)
    if not result["succeeded"]:
        return []
    return list(result["discovered_ids"])


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
                        "description": "reported by the installed CLI (not an entitlement guarantee)",
                        "status": "local",
                        "source": "cli",
                    }
                )
    return rows


def provider_models_payload(provider: str, *, discover: bool = True) -> dict:
    """Catalog rows plus discovery metadata for one provider (models CLI JSON shape)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    discovery = discover_models(provider, attempt=discover)
    rows = models_for(provider, discover=False)
    if discovery["succeeded"]:
        known = {row["id"] for row in rows}
        for model in discovery["discovered_ids"]:
            if model not in known:
                rows.append(
                    {
                        "id": model,
                        "description": "reported by the installed CLI (not an entitlement guarantee)",
                        "status": "local",
                        "source": "cli",
                    }
                )
                known.add(model)
    return {
        "source": SOURCES[provider],
        "models": rows,
        "custom_model_allowed": True,
        "discovery": discovery,
    }
