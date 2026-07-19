#!/usr/bin/env python3
"""Named multi-role topology projection for FrontierFuse (stdlib-only, pure).

Roles are labels over consult/body primitives. Host remains harness-owned.
Legacy frontier_* and executor slots remain the primary aliases.
"""
from __future__ import annotations

from typing import Any

ROLE_KINDS = frozenset({"consult", "body"})
# Builtin aliases that always resolve from the two native slots.
BUILTIN_ROLE_NAMES = frozenset({"frontier", "executor", "frontier_advisor", "executor_body"})
RESERVED_ROLE_NAMES = BUILTIN_ROLE_NAMES | frozenset({"host", "verifier"})

_ROLE_NAME_RE_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _known_providers() -> frozenset[str]:
    # Late import avoids circular import at module load in some test harnesses.
    import frontier_common as fc

    return fc.KNOWN_EXECUTORS


def validate_role_name(name: str) -> str:
    n = str(name or "").strip()
    if not n or len(n) > 64:
        raise ValueError("role name must be 1..64 characters")
    if n.lower() in {"host", "verifier"}:
        raise ValueError(f"role name {n!r} is reserved")
    if any(c not in _ROLE_NAME_RE_OK for c in n):
        raise ValueError(
            f"role name {n!r} may only contain letters, digits, underscore, and hyphen"
        )
    return n


def validate_role_binding(binding: Any, *, source: str = "roles") -> dict:
    if not isinstance(binding, dict):
        raise ValueError(f"invalid role binding in {source}; expected an object")
    kind = str(binding.get("kind") or "").lower().strip()
    if kind not in ROLE_KINDS:
        raise ValueError(
            f"invalid role kind in {source}; expected one of {sorted(ROLE_KINDS)}"
        )
    provider = str(binding.get("provider") or "").lower().strip()
    if provider not in _known_providers():
        raise ValueError(
            f"invalid role provider in {source}; expected one of {sorted(_known_providers())}"
        )
    model = binding.get("model")
    if model is None:
        model = ""
    if not isinstance(model, str):
        raise ValueError(f"invalid role model in {source}; expected a string")
    effort = binding.get("effort")
    out: dict[str, Any] = {
        "kind": kind,
        "provider": provider,
        "model": model,
    }
    if effort is not None:
        if not isinstance(effort, str):
            raise ValueError(f"invalid role effort in {source}; expected a string")
        e = effort.lower().strip()
        # Accept union of Codex + Grok effort sets; provider builders enforce tighter rules.
        allowed = frozenset({"low", "medium", "high", "xhigh"})
        if e not in allowed:
            raise ValueError(
                f"invalid role effort in {source}; expected one of {sorted(allowed)}"
            )
        out["effort"] = e
    return out


def validate_roles(roles: Any, *, source: str = "roles") -> dict[str, dict]:
    if roles is None:
        return {}
    if not isinstance(roles, dict):
        raise ValueError(f"invalid {source}; expected an object mapping name -> binding")
    out: dict[str, dict] = {}
    for raw_name, raw_binding in roles.items():
        name = validate_role_name(str(raw_name))
        # Allow redefining builtins only as documentation aliases if identical shape is valid.
        out[name] = validate_role_binding(raw_binding, source=f"{source}.{name}")
    return out


def _executor_model(cfg: dict, provider: str) -> str:
    provider = provider.lower()
    if provider == "claude":
        return str(cfg.get("claude_model") or "claude-sonnet-5")
    if provider == "grok":
        return str(cfg.get("grok_model") or "grok-4.5")
    if provider == "gemini":
        return str(cfg.get("gemini_model") or "gemini-3.5-flash")
    if provider == "openrouter":
        return str(cfg.get("openrouter_model") or "openrouter/auto")
    if provider == "codex":
        pinned = str(cfg.get("codex_model") or "")
        return pinned if pinned else "account default"
    return ""


def _frontier_model(cfg: dict) -> str:
    import frontier_common as fc

    return fc.effective_frontier_model(cfg)


def builtin_roles(cfg: dict) -> dict[str, dict]:
    frontier_provider = str(cfg.get("frontier_provider") or "claude").lower()
    executor = str(cfg.get("executor") or "codex").lower()
    frontier_binding = {
        "kind": "consult",
        "provider": frontier_provider,
        "model": _frontier_model(cfg),
        "source": "frontier_slot",
    }
    effort_key = "codex_effort" if frontier_provider == "codex" else (
        "grok_effort" if frontier_provider == "grok" else None
    )
    if effort_key and cfg.get(effort_key):
        frontier_binding["effort"] = str(cfg.get(effort_key)).lower()

    body_binding: dict[str, Any] = {
        "kind": "body",
        "provider": executor,
        "model": _executor_model(cfg, executor),
        "source": "executor_slot",
    }
    if executor == "codex":
        body_binding["effort"] = str(cfg.get("codex_effort") or "high").lower()
    elif executor == "grok":
        body_binding["effort"] = str(cfg.get("grok_effort") or "high").lower()

    return {
        "frontier": frontier_binding,
        "frontier_advisor": {**frontier_binding, "source": "alias:frontier"},
        "executor": body_binding,
        "executor_body": {**body_binding, "source": "alias:executor"},
    }


def resolve_role(cfg: dict, role_name: str) -> dict:
    """Resolve a role name to a concrete binding (kind/provider/model[/effort])."""
    name = validate_role_name(role_name)
    customs = validate_roles(cfg.get("roles") or {}, source="roles")
    builtins = builtin_roles(cfg)
    # Custom bindings override builtins when explicitly set.
    if name in customs:
        binding = dict(customs[name])
        binding["source"] = f"roles.{name}"
        binding["name"] = name
        return binding
    if name in builtins:
        binding = dict(builtins[name])
        binding["name"] = name
        return binding
    raise ValueError(
        f"unknown role {name!r}; configure it under roles or use frontier|executor"
    )


def project_topology(cfg: dict, *, host_note: str | None = None) -> dict:
    """Pure topology projection. Never launches providers or spends tokens."""
    customs = validate_roles(cfg.get("roles") or {}, source="roles")
    builtins = builtin_roles(cfg)
    roles: dict[str, dict] = {}
    # Stable presentation order: builtins first, then customs.
    for name in ("frontier", "frontier_advisor", "executor", "executor_body"):
        roles[name] = builtins[name]
    for name in sorted(customs):
        roles[name] = {**customs[name], "source": f"roles.{name}"}

    providers_used = sorted({
        str(r.get("provider")) for r in roles.values() if r.get("provider")
    })
    crossings = []
    for name, binding in roles.items():
        provider = str(binding.get("provider") or "")
        if provider and provider != "local":
            crossings.append({
                "role": name,
                "provider": provider,
                "kind": binding.get("kind"),
                "context_leaves_machine": True,
                "note": (
                    "Managed provider calls send prompt context to that provider. "
                    "OpenRouter may route to third-party models behind the scenes."
                    if provider == "openrouter"
                    else "Managed provider calls send prompt context to that provider."
                ),
            })

    return {
        "schema": "frontierfuse.topology.v1",
        "host": {
            "owned_by": "harness",
            "swappable_by_plugin": False,
            "note": host_note or (
                "The host harness session model is never replaced by FrontierFuse. "
                "Configure managed consults and bodies only."
            ),
        },
        "profile": str(cfg.get("profile") or "advisor"),
        "verifier": {
            "kind": "deterministic_gate",
            "is_model": False,
            "note": "GREEN only from snapshot-bound gate evidence, never model prose.",
        },
        "native_slots": {
            "frontier": {
                "provider": str(cfg.get("frontier_provider") or "claude"),
                "model": _frontier_model(cfg),
            },
            "executor": {
                "provider": str(cfg.get("executor") or "codex"),
                "model": _executor_model(cfg, str(cfg.get("executor") or "codex")),
            },
        },
        "roles": roles,
        "providers_in_use": providers_used,
        "provider_crossings": crossings,
        "recipes": {
            "premium_host_fable_sol_grok": {
                "host": "harness premium model (e.g. Opus)",
                "frontier": "claude / claude-fable-5",
                "orchestration_consult": "codex / gpt-5.6-sol @ xhigh (named role)",
                "executor": "grok / grok-4.5",
            },
            "openrouter_menagerie": {
                "note": (
                    "Bind any named consult/body role to provider=openrouter with an exact "
                    "catalog model ID. Requires OPENROUTER_API_KEY for live calls."
                ),
            },
        },
        "limitations": [
            "No automatic model router.",
            "Recursive delegation is denied before managed-controller releases.",
            "Named roles are labels over consult/body primitives, not free-form agents.",
        ],
    }


def cfg_for_role_consult(cfg: dict, role_name: str) -> dict:
    """Return a shallow-copied cfg with frontier_* set from a consult role."""
    binding = resolve_role(cfg, role_name)
    if binding.get("kind") != "consult":
        raise ValueError(
            f"role {role_name!r} is kind={binding.get('kind')!r}; consult requires kind=consult"
        )
    out = dict(cfg)
    out["frontier_provider"] = binding["provider"]
    out["frontier_model"] = binding.get("model") or ""
    provider = binding["provider"]
    effort = binding.get("effort")
    if effort:
        if provider == "codex":
            out["codex_effort"] = effort
        elif provider == "grok":
            if effort == "xhigh":
                raise ValueError("Grok effort has no xhigh; use low|medium|high")
            out["grok_effort"] = effort
    if provider == "openrouter":
        out["openrouter_model"] = binding.get("model") or out.get("openrouter_model") or ""
    return out


def cfg_for_role_body(cfg: dict, role_name: str) -> dict:
    """Return a shallow-copied cfg with executor fields set from a body role."""
    binding = resolve_role(cfg, role_name)
    if binding.get("kind") != "body":
        raise ValueError(
            f"role {role_name!r} is kind={binding.get('kind')!r}; body dispatch requires kind=body"
        )
    out = dict(cfg)
    provider = binding["provider"]
    out["executor"] = provider
    model = binding.get("model") or ""
    if provider == "claude":
        out["claude_model"] = model or out.get("claude_model")
    elif provider == "grok":
        out["grok_model"] = model or out.get("grok_model")
    elif provider == "gemini":
        out["gemini_model"] = model or out.get("gemini_model")
    elif provider == "codex":
        out["codex_model"] = "" if model in ("", "account default") else model
    elif provider == "openrouter":
        out["openrouter_model"] = model or out.get("openrouter_model")
    effort = binding.get("effort")
    if effort:
        if provider == "codex":
            out["codex_effort"] = effort
        elif provider == "grok":
            if effort == "xhigh":
                raise ValueError("Grok effort has no xhigh; use low|medium|high")
            out["grok_effort"] = effort
    return out
