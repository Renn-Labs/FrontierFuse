#!/usr/bin/env python3
"""frontier_common.py — shared foundation for FrontierFuse (the contract every module imports).

FrontierFuse pairs two model roles. A configured frontier model is a managed consult; the
host-bound harness remains the session lead. Selecting a frontier model does not hot-swap the
host conversation model, and no frontier model (including Claude Fable) is hard-wired.

  - BODY / EXECUTOR = the selected provider (codex|claude|grok|gemini|openrouter) that performs work.
                      Codex default is unpinned (account-aware) unless FRONTIER_CODEX_MODEL /
                      --model / --executor-model explicitly pins one.
  - FRONTIER / ADVISOR = a managed consult to the configured frontier provider/model
                         (default Claude Fable 5 via `claude -p --model claude-fable-5`).

Two control flows are built on this foundation:
  - advisor  (default): host/executor-led — the selected executor is the main loop and consults the
                        configured frontier model ON-DEMAND via `ask_frontier` (frontier_advisor.py /
                        frontier_advisor_mcp.py). Selecting a frontier model never makes it the host.
  - orchestrator      : host-led verified orchestration with managed frontier consult/executor
                        bodies (frontier_dispatch.py) behind a workflow guardrail + deterministic
                        verdict. The host harness remains the session lead.

This module owns everything shared so the two loops never drift:
  - config toggles + precedence (body/advisor models, effort, fast mode)
  - per-session state file (arm marker, last_dispatch_ts, verdict, session config)
  - body/lead + managed-frontier command builders and a single run_engine()
  - artifact capture + bounded handoff cards (so N bodies never flood a controller's context)
  - the verdict schema + kill-switch helper

stdlib-only, Python 3.10+, importable (underscore name) for offline contract tests.
"""
from __future__ import annotations

import ctypes
import datetime
import errno
import hashlib
import hmac
import json
import secrets
import math
import os
import selectors
import shlex
import signal
import sys
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_HOME = Path(os.environ.get("FRONTIER_CONFIG_DIR", Path.home() / ".config" / "frontier-fuse"))
GLOBAL_CONFIG = Path(os.environ.get("FRONTIER_CONFIG", CONFIG_HOME / "config.json"))
STATE_DIR = Path(os.environ.get("FRONTIER_STATE_DIR", CONFIG_HOME / "state"))

# Owner-only modes for sensitive local artifacts (config, state, prompts, runs).
OWNER_ONLY_FILE = 0o600
OWNER_ONLY_DIR = 0o700
CONFIG_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
HANDOFF_SCHEMA_VERSION = 1
MAX_VERDICT_RECEIPT_BYTES = 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 4 * 1024 * 1024
KNOWN_EXECUTORS = frozenset({"codex", "claude", "grok", "gemini", "openrouter"})
KNOWN_PROFILES = frozenset({"advisor", "orchestrator"})


# --------------------------------------------------------------------------- #
# Config toggles + precedence
# --------------------------------------------------------------------------- #
# Effective config keys (all optional; env/config/session may override defaults):
#   codex_model   body model            (default "" -> unset, Codex CLI's own current default)
#   codex_effort  Codex reasoning effort  low|medium|high|xhigh (default high)
#   grok_effort   Grok reasoning effort   low|medium|high (default high)
#   fast          bool speed preset      (default False) -> body uses fast_effort/fast_model
#   fast_effort   effort when fast=on    (default low)
#   fast_model    optional fast model (default None -> inherit codex_model; "" -> account default)
#   profile       control flow            advisor|orchestrator (default advisor)
#   frontier_provider managed advisor     codex|claude|grok|gemini (default claude)
#   frontier_model frontier model         (default claude-fable-5)
#   executor      body provider           codex|claude|grok|gemini (default codex)
#   claude_model  model when executor=claude (default claude-sonnet-5)
#   grok_model    model when executor=grok   (default grok-4.5)
#   gemini_model  model when executor=gemini (default gemini-3.5-flash)
#   update_mode   release reminders          passive|manual|off (default passive)
# Autonomous permission flags are opt-in (default OFF as of 0.2.6):
#   FRONTIER_CODEX_YOLO=1 adds Codex --yolo
#   FRONTIER_GROK_YOLO=1 adds Grok --permission-mode bypassPermissions
#   FRONTIER_GROK_PERMISSION_MODE=<mode> sets an explicit Grok permission mode
CONFIG_KEYS = ("codex_model", "codex_effort", "fast", "fast_effort", "fast_model",
               "profile", "frontier_provider", "frontier_model", "executor", "claude_model",
               "grok_model", "gemini_model", "openrouter_model", "grok_effort", "update_mode",
               "roles")
UPDATE_MODES = frozenset({"passive", "manual", "off"})
_GATE_PUNCTUATION = "();<>|&`"
CODEX_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh"})
GROK_EFFORT_LEVELS = frozenset({"low", "medium", "high"})

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def defaults() -> dict:
    return {
        # Deliberately empty: OpenAI ships new gpt-5.x / *-codex releases every few weeks (see
        # "Staying current" in README) — hardcoding a version number is how this project shipped
        # an invented, nonexistent "gpt-5.5-codex" default in the first place. Empty means
        # build_codex_command() omits --model so `codex exec` uses its own current account-aware
        # default. Set FRONTIER_CODEX_MODEL / --model to pin a specific release.
        "codex_model": "",
        "codex_effort": "high",
        "grok_effort": "high",
        "fast": False,
        "fast_effort": "low",
        "fast_model": None,
        "profile": "advisor",
        "frontier_provider": "claude",
        "frontier_model": "claude-fable-5",
        "executor": "codex",
        "claude_model": "claude-sonnet-5",
        "grok_model": "grok-4.5",
        "gemini_model": "gemini-3.5-flash",
        "openrouter_model": "openrouter/auto",
        "roles": {},
        "update_mode": "passive",
    }


def _env_config() -> dict:
    """Config sourced from environment (FRONTIER_* wins over nothing but loses to file/session/flag)."""
    out: dict = {}
    m = {
        "codex_model": "FRONTIER_CODEX_MODEL",
        "codex_effort": "FRONTIER_CODEX_EFFORT",
        "grok_effort": "FRONTIER_GROK_EFFORT",
        "fast_effort": "FRONTIER_CODEX_FAST_EFFORT",
        "fast_model": "FRONTIER_CODEX_FAST_MODEL",
        "profile": "FRONTIER_PROFILE",
        "frontier_provider": "FRONTIER_PROVIDER",
        "frontier_model": "FRONTIER_MODEL",
        "executor": "FRONTIER_EXECUTOR",
        "claude_model": "FRONTIER_CLAUDE_MODEL",
        "grok_model": "FRONTIER_GROK_MODEL",
        "gemini_model": "FRONTIER_GEMINI_MODEL",
        "openrouter_model": "FRONTIER_OPENROUTER_MODEL",
        "update_mode": "FRONTIER_UPDATE_MODE",
    }
    for key, env in m.items():
        v = os.environ.get(env)
        if v is not None and (v != "" or key == "fast_model"):
            out[key] = v
    if os.environ.get("FRONTIER_CODEX_FAST") is not None:
        out["fast"] = os.environ.get("FRONTIER_CODEX_FAST")
    return out


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant {value!r}")


def parse_gate_argv(gate: str) -> list[str]:
    """Parse one simple argv-style gate command without shell control syntax."""
    if not isinstance(gate, str) or "\n" in gate or "\r" in gate:
        raise ValueError("invalid argv gate")
    lexer = shlex.shlex(gate, posix=True, punctuation_chars=_GATE_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv = list(lexer)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("empty gate command")
    if any(token and all(char in _GATE_PUNCTUATION for char in token) for token in argv):
        raise ValueError("shell syntax is not allowed in an argv gate")
    return argv


class ConfigFileError(ValueError):
    """A persisted configuration document cannot be used without explicit repair."""

    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = reason
        super().__init__(
            f"configuration file {self.path} is {reason}; preserve it and run "
            "`frontier-dispatch config --repair --global`"
        )


class StateFileError(ValueError):
    """A persisted session document cannot be trusted without explicit repair."""

    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = reason
        super().__init__(
            f"session state file {self.path} is {reason}; preserve it and run "
            "`frontier-dispatch config --repair`"
        )


class SpecialFileError(ValueError):
    """A persistence path resolves to a non-regular file."""


def _schema_version_reason(schema) -> str:
    if type(schema) is int and -1_000_000 <= schema <= 1_000_000:
        return f"using unsupported schema_version {schema}"
    if type(schema) is int:
        return "using an out-of-range integer schema_version"
    return f"using a schema_version with invalid type {type(schema).__name__}"


def _read_bounded_regular_bytes(
    path: Path | str,
    *,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
) -> bytes:
    """Read a regular file without letting special-file paths become blocking reads."""
    p = Path(path)
    inspected = os.lstat(p)
    if not stat.S_ISREG(inspected.st_mode):
        raise SpecialFileError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SpecialFileError("not a regular file") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SpecialFileError("not a regular file")
        if opened.st_size > max_bytes:
            raise OverflowError(f"file exceeds {max_bytes} bytes")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise OverflowError(f"file exceeds {max_bytes} bytes")
        return raw
    finally:
        os.close(fd)


def read_bounded_regular_text(
    path: Path | str,
    *,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
) -> str:
    return _read_bounded_regular_bytes(path, max_bytes=max_bytes).decode("utf-8")


def inspect_json_file(path: Path | str) -> dict:
    """Return a non-sensitive structural status for a bounded regular JSON document."""
    p = Path(path)
    try:
        raw = read_bounded_regular_text(p)
    except FileNotFoundError:
        return {"status": "missing", "path": str(p), "data": {}}
    except UnicodeError:
        return {"status": "corrupt", "path": str(p)}
    except SpecialFileError:
        return {"status": "special_file", "path": str(p)}
    except ValueError:
        return {"status": "wrong_type", "path": str(p)}
    except OverflowError:
        return {"status": "oversized", "path": str(p)}
    except OSError as exc:
        return {"status": "unreadable", "path": str(p), "detail": exc.strerror or "read failed"}
    try:
        data = json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeError, ValueError, RecursionError):
        return {"status": "corrupt", "path": str(p)}
    if not isinstance(data, dict):
        return {"status": "wrong_type", "path": str(p)}
    return {"status": "ready", "path": str(p), "data": data}


def _read_config_document(path: Path) -> dict:
    result = inspect_json_file(path)
    status = result["status"]
    if status == "missing":
        return {}
    if status != "ready":
        raise ConfigFileError(path, status.replace("_", " "))
    data = result["data"]
    if "schema_version" in data:
        schema = data["schema_version"]
        if type(schema) is not int or schema < 1 or schema > CONFIG_SCHEMA_VERSION:
            raise ConfigFileError(path, _schema_version_reason(schema))
    return data


def _validate_config_values(cfg: dict, *, source: str = "configuration") -> None:
    for key, allowed in (
        ("codex_effort", CODEX_EFFORT_LEVELS),
        ("fast_effort", CODEX_EFFORT_LEVELS),
        ("grok_effort", GROK_EFFORT_LEVELS),
    ):
        if key in cfg and (not isinstance(cfg[key], str) or cfg[key].lower() not in allowed):
            raise ValueError(f"invalid {key} in {source}; expected one of {sorted(allowed)}")
    if "fast" in cfg:
        value = cfg["fast"]
        if not isinstance(value, bool) and str(value).strip().lower() not in (_TRUE | _FALSE):
            raise ValueError(f"invalid fast in {source}; expected true or false")
    if (
        str(cfg.get("executor") or "").lower() == "grok"
        and _as_bool(cfg.get("fast"), False)
        and str(cfg.get("fast_effort") or "low").lower() not in GROK_EFFORT_LEVELS
    ):
        raise ValueError(
            f"invalid fast_effort in {source}; Grok fast mode expects one of "
            f"{sorted(GROK_EFFORT_LEVELS)}"
        )
    for key, allowed in (
        ("executor", KNOWN_EXECUTORS),
        ("frontier_provider", KNOWN_EXECUTORS),
        ("profile", KNOWN_PROFILES),
        ("update_mode", UPDATE_MODES),
    ):
        if key in cfg and (not isinstance(cfg[key], str) or cfg[key].lower() not in allowed):
            raise ValueError(f"invalid {key} in {source}; expected one of {sorted(allowed)}")
    for key in ("codex_model", "frontier_model", "claude_model", "grok_model", "gemini_model",
                "openrouter_model"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ValueError(f"invalid {key} in {source}; expected a model ID string")
    if "fast_model" in cfg and cfg["fast_model"] is not None and not isinstance(cfg["fast_model"], str):
        raise ValueError(f"invalid fast_model in {source}; expected null or a model ID string")
    if "roles" in cfg:
        from frontier_topology import validate_roles
        # Normalize in-place so persisted layers keep validated shape.
        cfg["roles"] = validate_roles(cfg.get("roles"), source=f"{source}.roles")


# --------------------------------------------------------------------------- #
# Owner-only atomic file helpers (reusable; used by config/state/artifacts)
# --------------------------------------------------------------------------- #
def mkdir_owner_only(path: Path | str) -> Path:
    """Create *path* (and parents) with owner-only directory mode when possible."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=OWNER_ONLY_DIR)
    try:
        p.chmod(OWNER_ONLY_DIR)
    except OSError:
        pass
    return p


def write_text_owner_only(
    path: Path | str,
    text: str,
    mode: int = OWNER_ONLY_FILE,
    *,
    protect_parent: bool = True,
) -> Path:
    """Atomically write *text* to *path* with owner-only permissions (tmp + replace)."""
    p = Path(path)
    if protect_parent:
        mkdir_owner_only(p.parent)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_name, p)
        try:
            p.chmod(mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def write_bytes_owner_only(path: Path | str, data: bytes, mode: int = OWNER_ONLY_FILE) -> Path:
    """Atomically write bytes to *path* with owner-only permissions."""
    p = Path(path)
    mkdir_owner_only(p.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_name, p)
        try:
            p.chmod(mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def write_json_owner_only(
    path: Path | str,
    data,
    mode: int = OWNER_ONLY_FILE,
    *,
    protect_parent: bool = True,
) -> Path:
    """Atomically write JSON (indent=2, sort_keys) with owner-only permissions."""
    return write_text_owner_only(
        path,
        _serialized_json_text(data),
        mode=mode,
        protect_parent=protect_parent,
    )


def write_json_owner_only_no_replace(
    path: Path | str,
    data,
    mode: int = OWNER_ONLY_FILE,
) -> Path:
    """Atomically publish owner-only JSON only when *path* does not already exist."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_serialized_json_text(data))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        # A same-directory hard link is atomic create-if-absent and cannot overwrite a file or
        # symlink created after the earlier availability check.
        os.link(tmp_name, p)
        return p
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _serialized_json_text(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"


def write_config_owner_only(path: Path | str, config: dict) -> Path:
    serialized = _serialized_json_text(config)
    if len(serialized.encode("utf-8")) > MAX_JSON_DOCUMENT_BYTES:
        raise ConfigFileError(Path(path), "exceeds the bounded JSON document size")
    return write_text_owner_only(path, serialized)


def write_state_owner_only(path: Path | str, state: dict) -> Path:
    serialized = _serialized_json_text(state)
    if len(serialized.encode("utf-8")) > MAX_JSON_DOCUMENT_BYTES:
        raise StateFileError(Path(path), "exceeds the bounded JSON document size")
    return write_text_owner_only(path, serialized)


def compact_verdict_receipt(verdict: dict) -> dict:
    """Keep shared receipt artifacts bounded while preserving cleanup and result identity."""
    encoded = _serialized_json_text(verdict).encode("utf-8")
    if len(encoded) <= MAX_VERDICT_RECEIPT_BYTES:
        return verdict
    keep = (
        "schema_version",
        "verification_id",
        "session_id",
        "result",
        "exit_code",
        "diff_sha",
        "ts",
        "dispatch_generation",
        "gate_mode",
        "unsafe",
        "unsafe_reason",
        "snapshot_stable",
        "workspace_supported",
    )
    compact = {key: verdict[key] for key in keep if key in verdict}
    gate = verdict.get("gate")
    compact["gate"] = gate if isinstance(gate, str) and len(gate.encode("utf-8")) <= 4096 else "<compacted>"
    compact.update({
        "paths": [],
        "receipt_compacted": True,
        "original_receipt_bytes": len(encoded),
        "original_receipt_sha256": hashlib.sha256(encoded).hexdigest(),
    })
    bounded = _serialized_json_text(compact).encode("utf-8")
    if len(bounded) > MAX_VERDICT_RECEIPT_BYTES:
        raise ValueError("verdict receipt identity exceeds the publication size limit")
    return compact


def config_lock_path(path: Path | str) -> Path:
    p = Path(path)
    return p.with_name(f".{p.name}.lock")


@contextmanager
def advisory_lock(path: Path | str):
    """Serialize local config/state writers on supported Unix harness platforms."""
    import fcntl

    p = Path(path)
    mkdir_owner_only(p.parent)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(p, flags, OWNER_ONLY_FILE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"lock path is not a regular file: {p}")
        try:
            os.fchmod(fd, OWNER_ONLY_FILE)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_global_config() -> dict:
    raw = _read_config_document(GLOBAL_CONFIG)
    cfg = {k: v for k, v in raw.items() if k in CONFIG_KEYS}
    try:
        _validate_config_values(cfg, source=str(GLOBAL_CONFIG))
    except ValueError as exc:
        raise ConfigFileError(GLOBAL_CONFIG, str(exc)) from exc
    return cfg


def save_global_config(cfg: dict) -> None:
    mkdir_owner_only(GLOBAL_CONFIG.parent)
    patch = {k: v for k, v in cfg.items() if k in CONFIG_KEYS}
    _validate_config_values(patch, source="global config update")
    with advisory_lock(config_lock_path(GLOBAL_CONFIG)):
        merged = {**load_global_config(), **patch}
        _validate_config_values(merged, source="global config")
        write_config_owner_only(
            GLOBAL_CONFIG,
            {"schema_version": CONFIG_SCHEMA_VERSION, **merged},
        )


def update_config_transaction(session_id: str, patch: dict, *, global_scope: bool) -> None:
    """Validate and persist one config layer while both config layers are locked."""
    state_file = state_path(session_id)
    mkdir_owner_only(GLOBAL_CONFIG.parent)
    mkdir_owner_only(state_file.parent)
    filtered = {k: v for k, v in patch.items() if k in CONFIG_KEYS}
    _validate_config_values(filtered, source="config update")

    # All layered config transactions use this order so global/session writers cannot interleave.
    with advisory_lock(config_lock_path(GLOBAL_CONFIG)):
        with advisory_lock(config_lock_path(state_file)):
            global_config = load_global_config()
            state = read_state(session_id)
            session_config = {
                k: v for k, v in state.get("config", {}).items() if k in CONFIG_KEYS
            }
            if global_scope:
                global_config = {**global_config, **filtered}
                _validate_config_values(global_config, source="global config")
            else:
                session_config = {**session_config, **filtered}
                _validate_config_values(session_config, source="session config")

            effective = defaults()
            effective.update(_env_config())
            effective.update(global_config)
            effective.update(session_config)
            _validate_config_values(effective, source="effective configuration")

            if global_scope:
                write_config_owner_only(
                    GLOBAL_CONFIG,
                    {"schema_version": CONFIG_SCHEMA_VERSION, **global_config},
                )
            else:
                state = _merge_state_patch(
                    state,
                    {"config": filtered},
                    source=str(state_file),
                )
                write_state_owner_only(state_file, state)


def repair_config_file(
    path: Path | str,
    *,
    kind: str,
    legacy_path: Path | str | None = None,
    session_id: str | None = None,
) -> dict:
    """Explicitly back up and reset a malformed global config or session state file."""
    if kind not in {"global", "state"}:
        raise ValueError(f"unknown repair kind {kind!r}")
    p = Path(path)
    with advisory_lock(config_lock_path(p)):
        source = p
        result = inspect_json_file(source)
        if (
            kind == "state"
            and result["status"] == "missing"
            and legacy_path is not None
            and os.path.lexists(legacy_path)
        ):
            source = Path(legacy_path)
            result = inspect_json_file(source)
        if result["status"] == "missing":
            return {"status": "not_needed", "path": str(p), "backup": ""}
        if result["status"] == "unreadable":
            error_type = ConfigFileError if kind == "global" else StateFileError
            raise error_type(p, "unreadable; fix ownership or permissions before repair")
        if result["status"] == "ready":
            try:
                if kind == "global":
                    raw = _read_config_document(source)
                    _validate_config_values(
                        {k: v for k, v in raw.items() if k in CONFIG_KEYS},
                        source=str(p),
                    )
                elif kind == "state":
                    state = result["data"]
                    if "schema_version" in state:
                        schema = state["schema_version"]
                        if type(schema) is not int or schema < 1 or schema > STATE_SCHEMA_VERSION:
                            raise ValueError(_schema_version_reason(schema))
                    config = state.get("config", {})
                    if not isinstance(config, dict):
                        raise ValueError("session config must be an object")
                    _validate_config_values(config, source=str(p))
                    _validate_state_values(state, source=str(p))
            except (ConfigFileError, ValueError):
                pass
            else:
                if source == p:
                    return {"status": "not_needed", "path": str(p), "backup": ""}
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = source.with_name(f"{source.name}.invalid-{timestamp}.bak")
        try:
            source_stat = os.lstat(source)
        except OSError as exc:
            error_type = ConfigFileError if kind == "global" else StateFileError
            raise error_type(source, exc.strerror or "cannot inspect before repair") from exc
        if not stat.S_ISREG(source_stat.st_mode):
            error_type = ConfigFileError if kind == "global" else StateFileError
            raise error_type(source, "not a regular file; replace it before repair")
        recovered_receipt_path = None
        recovered_receipt_identity = None
        if kind == "state" and result.get("status") == "ready":
            candidate = result.get("data", {})
            for candidate_identity in (
                candidate.get("verdict"),
                candidate.get("receipt_identity"),
            ):
                identity = _receipt_identity(candidate_identity)
                if not _receipt_identity_can_cleanup(identity, session_id=session_id):
                    continue
                try:
                    json.dumps(identity, allow_nan=False)
                except (TypeError, ValueError, OverflowError, RecursionError):
                    continue
                recovered_receipt_identity = identity
                break

            candidate_path = candidate.get("verdict_path")
            if candidate_path is not None and recovered_receipt_identity:
                try:
                    _validate_persisted_path(
                        candidate_path,
                        source=str(p),
                        receipt=True,
                    )
                    recovered_receipt_path = candidate_path
                except StateFileError:
                    recovered_receipt_path = None

            if recovered_receipt_path is None and recovered_receipt_identity:
                approved = candidate.get("approved_gate")
                legacy_cwd = approved.get("cwd") if isinstance(approved, dict) else None
                if legacy_cwd is not None:
                    try:
                        _validate_persisted_path(
                            legacy_cwd,
                            source=str(p),
                            receipt=False,
                        )
                    except StateFileError:
                        pass
                    else:
                        recovered_receipt_path = str(Path(legacy_cwd, "verdict.json"))
        if kind == "global":
            payload = {"schema_version": CONFIG_SCHEMA_VERSION}
        elif kind == "state":
            payload = {
                "schema_version": STATE_SCHEMA_VERSION,
                "armed": False,
                "last_dispatch_ts": 0.0,
                "dispatch_generation": 0,
                "state_revision": 0,
                "verdict": None,
                "approved_gate": None,
                "receipt_identity": recovered_receipt_identity,
                "config": {},
                "active_dispatches": [],
                "active_verifications": [],
                "verdict_path": recovered_receipt_path,
                "completion_pending": False,
                "completion_closed": False,
            }
        os.link(source, backup, follow_symlinks=False)
        try:
            os.chmod(backup, OWNER_ONLY_FILE)
            if kind == "state":
                write_state_owner_only(p, payload)
            else:
                write_config_owner_only(p, payload)
        except Exception:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if source != p:
            source.unlink(missing_ok=True)
    return {"status": "repaired", "path": str(p), "backup": str(backup)}


def _compose_config_layers(overrides: dict | None = None, session_id: str | None = None) -> dict:
    cfg = defaults()
    cfg.update(_env_config())
    cfg.update(load_global_config())
    if session_id:
        cfg.update({k: v for k, v in read_state(session_id).get("config", {}).items() if k in CONFIG_KEYS})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k in CONFIG_KEYS and v is not None})
    return cfg


def resolve_config_shape(
    overrides: dict | None = None,
    session_id: str | None = None,
) -> tuple[str, bool]:
    """Return routing fields before cross-layer validation so one command can heal them."""
    cfg = _compose_config_layers(overrides=overrides, session_id=session_id)
    return (
        str(cfg.get("executor") or "codex").lower(),
        _as_bool(cfg.get("fast"), False),
    )


def resolve_config(overrides: dict | None = None, session_id: str | None = None) -> dict:
    """Precedence (low -> high): defaults < env < global config file < session config < explicit flags."""
    cfg = _compose_config_layers(overrides=overrides, session_id=session_id)
    _validate_config_values(cfg, source="effective configuration")
    cfg["fast"] = _as_bool(cfg.get("fast"), False)
    cfg["codex_effort"] = str(cfg.get("codex_effort") or "high").lower()
    cfg["grok_effort"] = str(cfg.get("grok_effort") or "high").lower()
    cfg["fast_effort"] = str(cfg.get("fast_effort") or "low").lower()
    cfg["executor"] = str(cfg.get("executor") or "codex").lower()
    cfg["profile"] = str(cfg.get("profile") or "advisor").lower()
    cfg["frontier_provider"] = str(cfg.get("frontier_provider") or "claude").lower()
    if cfg["executor"] not in KNOWN_EXECUTORS:
        raise ValueError(f"unknown executor {cfg['executor']!r}; expected one of {sorted(KNOWN_EXECUTORS)}")
    if cfg["frontier_provider"] not in KNOWN_EXECUTORS:
        raise ValueError(
            f"unknown frontier provider {cfg['frontier_provider']!r}; "
            f"expected one of {sorted(KNOWN_EXECUTORS)}"
        )
    if cfg["profile"] not in KNOWN_PROFILES:
        raise ValueError(f"unknown profile {cfg['profile']!r}; expected one of {sorted(KNOWN_PROFILES)}")
    cfg["update_mode"] = str(cfg.get("update_mode") or "passive").lower()
    if not isinstance(cfg.get("openrouter_model"), str):
        cfg["openrouter_model"] = "openrouter/auto"
    from frontier_topology import validate_roles
    cfg["roles"] = validate_roles(cfg.get("roles") or {}, source="effective configuration.roles")
    return cfg


# --------------------------------------------------------------------------- #
# Per-session state (arm marker / last_dispatch_ts / verdict / session config)
# --------------------------------------------------------------------------- #
def session_id_is_valid(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


_HASHED_STATE_PREFIX = "frontierfuse-hashed-session-state-collision-resistant-namespace-v1-"


def state_path(session_id: str) -> Path:
    raw = session_id or "default"
    if not session_id_is_valid(raw):
        raise StateFileError(STATE_DIR, "using an invalid session identifier")
    stem = f"{_HASHED_STATE_PREFIX}{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    return STATE_DIR / f"{stem}.json"


def legacy_state_path(session_id: str) -> Path:
    raw = session_id or "default"
    if not session_id_is_valid(raw):
        raise StateFileError(STATE_DIR, "using an invalid session identifier")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw)[:120]
    return STATE_DIR / f"{safe or 'default'}.json"


def read_state(session_id: str) -> dict:
    path = state_path(session_id)
    result = inspect_json_file(path)
    if result["status"] == "missing":
        legacy = legacy_state_path(session_id)
        if legacy != path and os.path.lexists(legacy):
            raise StateFileError(
                path,
                f"using a legacy ambiguous state path at {legacy}; run explicit session repair",
            )
        st = {}
    elif result["status"] != "ready":
        raise StateFileError(path, result["status"].replace("_", " "))
    else:
        st = result["data"]
        if "schema_version" in st:
            schema = st["schema_version"]
            if type(schema) is not int or schema < 1 or schema > STATE_SCHEMA_VERSION:
                raise StateFileError(path, _schema_version_reason(schema))
        config = st.get("config", {})
        if not isinstance(config, dict):
            raise StateFileError(path, "using a non-object config")
        try:
            _validate_config_values(config, source=str(path))
        except ValueError as exc:
            raise StateFileError(path, str(exc)) from exc
    st.setdefault("schema_version", STATE_SCHEMA_VERSION)
    st.setdefault("armed", False)
    st.setdefault("last_dispatch_ts", 0.0)
    st.setdefault("dispatch_generation", 0)
    st.setdefault("state_revision", 0)
    st.setdefault("verdict", None)
    st.setdefault("receipt_identity", None)
    st.setdefault("config", {})
    st.setdefault("active_dispatches", [])
    st.setdefault("active_verifications", [])
    st.pop("verdict_paths", None)  # Remove the unreleased append-only 0.3.2 draft field.
    st.setdefault("verdict_path", None)
    st.setdefault("completion_pending", False)
    st.setdefault("completion_closed", False)
    _validate_state_values(st, source=str(path))
    return st


def _validate_state_values(st: dict, *, source: str) -> None:
    for key in ("armed", "completion_pending", "completion_closed"):
        if key in st and not isinstance(st[key], bool):
            raise StateFileError(Path(source), f"using non-boolean {key}")
    value = st.get("last_dispatch_ts", 0.0)
    try:
        timestamp_is_finite = math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        timestamp_is_finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not timestamp_is_finite:
        raise StateFileError(Path(source), "using non-numeric last_dispatch_ts")
    generation = st.get("dispatch_generation", 0)
    if type(generation) is not int or generation < 0:
        raise StateFileError(Path(source), "using invalid dispatch_generation")
    revision = st.get("state_revision", 0)
    if type(revision) is not int or revision < 0:
        raise StateFileError(Path(source), "using invalid state_revision")
    for key in ("verdict", "approved_gate", "receipt_identity"):
        if key in st and st[key] is not None and not isinstance(st[key], dict):
            raise StateFileError(Path(source), f"using non-object {key}")
    active = st.get("active_dispatches", [])
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
        raise StateFileError(Path(source), "using invalid active_dispatches")
    verifications = st.get("active_verifications", [])
    if not isinstance(verifications, list) or any(not isinstance(item, str) for item in verifications):
        raise StateFileError(Path(source), "using invalid active_verifications")
    verdict_path = st.get("verdict_path")
    if verdict_path is not None and not isinstance(verdict_path, str):
        raise StateFileError(Path(source), "using invalid verdict_path")
    if isinstance(verdict_path, str):
        _validate_persisted_path(verdict_path, source=source, receipt=True)
    approved_gate = st.get("approved_gate")
    if isinstance(approved_gate, dict):
        gate = approved_gate.get("gate")
        argv = approved_gate.get("argv")
        if (
            not isinstance(gate, str)
            or not gate
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise StateFileError(Path(source), "using an incomplete approved_gate")
        try:
            parsed = parse_gate_argv(gate)
        except ValueError as exc:
            raise StateFileError(Path(source), "using an invalid approved_gate command") from exc
        if parsed != argv:
            raise StateFileError(Path(source), "using a mismatched approved_gate argv")
        _validate_persisted_path(approved_gate.get("cwd"), source=source, receipt=False)
    try:
        json.dumps(st, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise StateFileError(Path(source), "containing non-serializable or non-finite values") from exc


def _validate_persisted_path(value, *, source: str, receipt: bool) -> None:
    label = "verdict_path" if receipt else "approved_gate.cwd"
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise StateFileError(Path(source), f"using invalid {label}")
    try:
        os.fsencode(value)
    except (UnicodeEncodeError, ValueError):
        raise StateFileError(Path(source), f"using invalid {label}") from None
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise StateFileError(Path(source), f"using non-normalized absolute {label}")
    if receipt and Path(value).name != "verdict.json":
        raise StateFileError(Path(source), "using a verdict_path not named verdict.json")


def _merge_state_patch(st: dict, patch: dict, *, source: str) -> dict:
    merged = dict(st)
    patch = dict(patch)
    patch.pop("state_revision", None)
    if "config" in patch:
        if not isinstance(patch["config"], dict):
            raise StateFileError(Path(source), "using a non-object config update")
        config_patch = {k: v for k, v in patch.pop("config").items() if k in CONFIG_KEYS}
        _validate_config_values(config_patch, source="session config update")
        merged_cfg = {**merged.get("config", {}), **config_patch}
        _validate_config_values(merged_cfg, source="session config")
        merged["config"] = merged_cfg
    merged.update(patch)
    merged["schema_version"] = STATE_SCHEMA_VERSION
    merged["state_revision"] = st.get("state_revision", 0) + 1
    _validate_state_values(merged, source=source)
    return merged


def _unlink_managed_verdict(path: Path | str | None, expected: dict | None = None) -> bool:
    """Delete only a regular FrontierFuse verdict receipt, never an arbitrary path occupant."""
    if not path:
        return True
    receipt = Path(path)
    try:
        inspected = os.lstat(receipt)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(inspected.st_mode):
        return False
    quarantine = receipt.with_name(
        f".{receipt.name}.frontier-quarantine-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        os.replace(receipt, quarantine)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    def restore_quarantine() -> None:
        try:
            if not os.path.lexists(receipt):
                os.replace(quarantine, receipt)
        except OSError:
            pass

    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(quarantine, flags)
    except OSError:
        restore_quarantine()
        return False
    valid = False
    try:
        opened = os.fstat(fd)
        if stat.S_ISREG(opened.st_mode) and opened.st_size <= MAX_VERDICT_RECEIPT_BYTES:
            chunks = []
            total = 0
            while total <= MAX_VERDICT_RECEIPT_BYTES:
                chunk = os.read(
                    fd,
                    min(65536, MAX_VERDICT_RECEIPT_BYTES + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total <= MAX_VERDICT_RECEIPT_BYTES:
                data = json.loads(
                    b"".join(chunks).decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
                if isinstance(data, dict) and type(data.get("schema_version")) is int:
                    if data.get("schema_version", 0) >= 2:
                        verification_id = data.get("verification_id")
                        expected_id = (
                            expected.get("verification_id")
                            if isinstance(expected, dict)
                            else None
                        )
                        if isinstance(verification_id, str) and verification_id:
                            receipt_session = data.get("session_id")
                            expected_session = (
                                expected.get("session_id")
                                if isinstance(expected, dict)
                                else None
                            )
                            valid = (
                                isinstance(expected_id, str)
                                and verification_id == expected_id
                                and (
                                    receipt_session is None
                                    or receipt_session == expected_session
                                )
                            )
                        elif isinstance(expected, dict):
                            identity_keys = ("result", "gate", "exit_code", "diff_sha", "ts")
                            valid = all(
                                key in expected and data.get(key) == expected.get(key)
                                for key in identity_keys
                            )
    except (OSError, UnicodeError, ValueError, RecursionError):
        valid = False
    finally:
        os.close(fd)
    if not valid:
        restore_quarantine()
        return False
    try:
        os.unlink(quarantine)
    except OSError:
        restore_quarantine()
        return False
    return True


def _receipt_identity(verdict: dict | None) -> dict | None:
    if not isinstance(verdict, dict):
        return None
    keys = (
        "schema_version", "verification_id", "session_id", "result", "gate", "exit_code",
        "diff_sha", "ts",
    )
    identity = {key: verdict[key] for key in keys if key in verdict}
    return identity or None


def _receipt_identity_can_cleanup(
    identity: dict | None,
    *,
    session_id: str | None = None,
) -> bool:
    if not isinstance(identity, dict):
        return False
    verification_id = identity.get("verification_id")
    if isinstance(verification_id, str) and verification_id:
        return session_id_is_valid(session_id) and identity.get("session_id") == session_id
    return all(key in identity for key in ("result", "gate", "exit_code", "diff_sha", "ts"))


def _require_receipt_target_available(path: Path | str | None) -> None:
    """Refuse publication over any pre-existing non-managed file, symlink, or directory."""
    if not path:
        return
    target = Path(path)
    try:
        target.lstat()
    except FileNotFoundError:
        return
    raise ValueError(
        f"verdict receipt target already exists and is not a managed FrontierFuse receipt: {target}"
    )


def write_state(session_id: str, **patch) -> dict:
    p = state_path(session_id)
    mkdir_owner_only(p.parent)
    with advisory_lock(config_lock_path(p)):
        st = _merge_state_patch(read_state(session_id), patch, source=str(p))
        write_state_owner_only(p, st)
        return st


def arm_session(session_id: str, approved_gate: dict | None) -> dict:
    """Arm while retaining ownership metadata for any receipt invalidated by re-arm."""
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
        cleanup_identity = _receipt_identity(prior_verdict) or st.get("receipt_identity")
        st = _merge_state_patch(
            st,
            {
                "armed": True,
                "approved_gate": approved_gate,
                "verdict": None,
                "receipt_identity": cleanup_identity,
                "completion_pending": False,
                "completion_closed": False,
            },
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def compare_and_write_state(session_id: str, expected: dict, **patch) -> tuple[bool, dict]:
    """Apply *patch* only when the selected state fields still match *expected*."""
    p = state_path(session_id)
    mkdir_owner_only(p.parent)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        if any(st.get(key) != value for key, value in expected.items()):
            return False, st
        st = _merge_state_patch(st, patch, source=str(p))
        write_state_owner_only(p, st)
        return True, st


def mark_completion_pending_locked(state_file: Path | str, st: dict) -> dict:
    """Fence new work after Stop validation while the caller holds the state lock."""
    if st.get("completion_pending"):
        return st
    path = Path(state_file)
    pending = _merge_state_patch(
        st,
        {"completion_pending": True},
        source=str(path),
    )
    write_state_owner_only(path, pending)
    return pending


def reopen_after_blocked_stop(session_id: str) -> dict:
    """A subsequent PreTool event proves that an allowed Stop was blocked elsewhere."""
    path = state_path(session_id)
    with advisory_lock(config_lock_path(path)):
        st = read_state(session_id)
        if not st.get("completion_pending"):
            return st
        prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
        reopened = _merge_state_patch(
            st,
            {
                "completion_pending": False,
                "dispatch_generation": st.get("dispatch_generation", 0) + 1,
                "verdict": None,
                "receipt_identity": _receipt_identity(prior_verdict)
                or st.get("receipt_identity"),
            },
            source=str(path),
        )
        write_state_owner_only(path, reopened)
        return reopened


def mark_dispatch_started(session_id: str, run_id: str, ts: float) -> dict:
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        if st.get("completion_closed"):
            raise ValueError(
                "session completion was already accepted; re-arm or disarm before dispatching"
            )
        if st.get("completion_pending"):
            raise ValueError("session Stop is pending; wait for host continuation before dispatching")
        active = list(st.get("active_dispatches", []))
        if run_id not in active:
            active.append(run_id)
        receipt_path = st.get("verdict_path")
        prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
        cleanup_identity = prior_verdict or st.get("receipt_identity")
        approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else {}
        approved_cwd = approved.get("cwd")
        if not receipt_path and isinstance(approved_cwd, str) and approved_cwd:
            receipt_path = str(Path(approved_cwd, "verdict.json"))
        st = _merge_state_patch(
            st,
            {
                "active_dispatches": active,
                "last_dispatch_ts": float(ts),
                "dispatch_generation": st.get("dispatch_generation", 0) + 1,
                "verdict": None,
                "verdict_path": receipt_path,
                "receipt_identity": _receipt_identity(cleanup_identity),
            },
            source=str(p),
        )
        write_state_owner_only(p, st)
        try:
            if receipt_path is None:
                return st
            cleaned = _unlink_managed_verdict(receipt_path, cleanup_identity)
        except (OSError, ValueError):
            st = _merge_state_patch(
                st,
                {
                    "active_dispatches": [item for item in active if item != run_id],
                    "verdict": None,
                },
                source=str(p),
            )
            write_state_owner_only(p, st)
            raise
        if not cleaned:
            return st
        st = _merge_state_patch(
            st,
            {"verdict_path": None, "receipt_identity": None},
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def mark_dispatch_finished(session_id: str, run_id: str, ts: float) -> dict:
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        prior_active = list(st.get("active_dispatches", []))
        active = [item for item in prior_active if item != run_id]
        patch = {"active_dispatches": active, "last_dispatch_ts": float(ts)}
        if run_id not in prior_active:
            prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
            patch.update({
                "dispatch_generation": st.get("dispatch_generation", 0) + 1,
                "verdict": None,
                "receipt_identity": _receipt_identity(prior_verdict) or st.get("receipt_identity"),
            })
        st = _merge_state_patch(
            st,
            patch,
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def mark_verification_started(
    session_id: str,
    verification_id: str,
    artifact_path: Path | str | None = None,
) -> dict:
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        if st.get("completion_closed"):
            raise ValueError(
                "session completion was already accepted; re-arm or disarm before verifying"
            )
        if st.get("completion_pending"):
            raise ValueError("session Stop is pending; wait for host continuation before verifying")
        active = list(st.get("active_verifications", []))
        if verification_id not in active:
            active.append(verification_id)
        old_receipt_path = st.get("verdict_path")
        prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
        cleanup_identity = prior_verdict or st.get("receipt_identity")
        if not old_receipt_path:
            approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else {}
            approved_cwd = approved.get("cwd")
            if isinstance(approved_cwd, str) and approved_cwd:
                old_receipt_path = str(Path(approved_cwd, "verdict.json"))
        receipt_path = str(Path(artifact_path)) if artifact_path is not None else old_receipt_path
        st = _merge_state_patch(
            st,
            {
                "active_verifications": active,
                "verdict": None,
                "verdict_path": old_receipt_path,
                "receipt_identity": _receipt_identity(cleanup_identity),
            },
            source=str(p),
        )
        write_state_owner_only(p, st)
        try:
            if not _unlink_managed_verdict(old_receipt_path, cleanup_identity):
                raise ValueError(
                    f"verdict receipt target already exists and could not be safely removed: "
                    f"{old_receipt_path}"
                )
            if receipt_path != old_receipt_path:
                st = _merge_state_patch(
                    st,
                    {
                        "verdict_path": receipt_path,
                        "receipt_identity": _receipt_identity(cleanup_identity),
                    },
                    source=str(p),
                )
                write_state_owner_only(p, st)
                if not _unlink_managed_verdict(receipt_path, cleanup_identity):
                    raise ValueError(
                        f"verdict receipt target already exists and could not be safely removed: "
                        f"{receipt_path}"
                    )
            _require_receipt_target_available(receipt_path)
        except (OSError, ValueError):
            st = _merge_state_patch(
                st,
                {
                    "active_verifications": [
                        item for item in active if item != verification_id
                    ],
                    "verdict": None,
                },
                source=str(p),
            )
            write_state_owner_only(p, st)
            raise
        st = _merge_state_patch(
            st,
            {
                "verdict_path": receipt_path,
                "receipt_identity": {
                    "verification_id": verification_id,
                    "session_id": session_id,
                },
            },
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def finish_verification(
    session_id: str,
    verification_id: str,
    expected_revision: int,
    expected_generation: int,
    verdict: dict,
    artifact_path: Path | str | None = None,
    artifact_verdict: dict | None = None,
) -> tuple[bool, dict]:
    """Remove an in-flight verifier and publish its receipt only if it remained exclusive."""
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        active = list(st.get("active_verifications", []))
        if verification_id not in active:
            return False, st
        exclusive = active == [verification_id]
        unchanged = st.get("state_revision", 0) == expected_revision
        dispatch_safe = (
            st.get("dispatch_generation", 0) == expected_generation
            and not st.get("active_dispatches")
        )
        patch = {
            "active_verifications": [item for item in active if item != verification_id],
        }
        publish = exclusive and unchanged and dispatch_safe
        if publish:
            patch["verdict"] = verdict
            if artifact_path is not None:
                patch["verdict_path"] = str(Path(artifact_path))
            patch["receipt_identity"] = None
        st = _merge_state_patch(st, patch, source=str(p))
        staged_artifact = Path(artifact_path) if publish and artifact_path is not None else None
        if staged_artifact is not None:
            _require_receipt_target_available(staged_artifact)
            # Keep the verifier active in authoritative state until the shared receipt is durable.
            write_json_owner_only_no_replace(
                staged_artifact,
                artifact_verdict if artifact_verdict is not None else verdict,
            )
        try:
            write_state_owner_only(p, st)
        except Exception:
            if staged_artifact is not None:
                try:
                    _unlink_managed_verdict(
                        staged_artifact,
                        artifact_verdict if artifact_verdict is not None else verdict,
                    )
                except OSError:
                    pass
            raise
        return publish, st


def abandon_verification(session_id: str, verification_id: str) -> dict:
    """Idempotently remove an in-flight verifier without publishing a receipt."""
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        active = list(st.get("active_verifications", []))
        if verification_id not in active:
            return st
        st = _merge_state_patch(
            st,
            {"active_verifications": [item for item in active if item != verification_id]},
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def disarm_session(session_id: str) -> dict:
    """Clear guard authority and forget the current receipt without revisiting old workspaces."""
    p = state_path(session_id)
    with advisory_lock(config_lock_path(p)):
        st = read_state(session_id)
        receipt_path = st.get("verdict_path")
        if receipt_path is None:
            approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else {}
            approved_cwd = approved.get("cwd")
            if isinstance(approved_cwd, str) and approved_cwd:
                receipt_path = str(Path(approved_cwd, "verdict.json"))
        prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
        cleanup_identity = prior_verdict or st.get("receipt_identity")
        retained_identity = _receipt_identity(cleanup_identity)
        st = _merge_state_patch(
            st,
            {
                "armed": False,
                "approved_gate": None,
                "active_dispatches": [],
                "active_verifications": [],
                "completion_pending": False,
                "completion_closed": False,
                "verdict": None,
                "verdict_path": receipt_path,
                "receipt_identity": retained_identity,
            },
            source=str(p),
        )
        write_state_owner_only(p, st)
        if receipt_path is None:
            return st
        if not _unlink_managed_verdict(receipt_path, cleanup_identity):
            return st
        st = _merge_state_patch(
            st,
            {"verdict_path": None, "receipt_identity": None},
            source=str(p),
        )
        write_state_owner_only(p, st)
        return st


def _consume_completion_state(state_file: Path, st: dict) -> dict:
    """Consume GREEN and its managed receipt. Caller must hold this state's advisory lock."""
    receipt_path = st.get("verdict_path")
    prior_verdict = st.get("verdict") if isinstance(st.get("verdict"), dict) else None
    cleanup_identity = prior_verdict or st.get("receipt_identity")
    if not _unlink_managed_verdict(receipt_path, cleanup_identity):
        raise ValueError("managed verdict receipt could not be safely removed")
    closed = _merge_state_patch(
        st,
        {
            "armed": False,
            "approved_gate": None,
            "verdict": None,
            "verdict_path": None,
            "receipt_identity": None,
            "completion_pending": False,
            "completion_closed": True,
        },
        source=str(state_file),
    )
    write_state_owner_only(state_file, closed)
    return closed


def consume_completion_locked(state_file: Path | str, st: dict) -> dict:
    """Consume completion while the caller holds the matching session-state lock."""
    return _consume_completion_state(Path(state_file), st)


def consume_completion(session_id: str, expected_revision: int) -> tuple[bool, dict]:
    """Compare-and-consume a successful completion under the session-state lock."""
    state_file = state_path(session_id)
    with advisory_lock(config_lock_path(state_file)):
        st = read_state(session_id)
        if st.get("state_revision") != expected_revision:
            return False, st
        return True, _consume_completion_state(state_file, st)


def clear_state(session_id: str) -> None:
    p = state_path(session_id)
    try:
        with advisory_lock(config_lock_path(p)):
            p.unlink(missing_ok=True)
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Verdict schema (shared by frontier_verify.py and the Stop hook)
# --------------------------------------------------------------------------- #
def make_verdict(gate: str, exit_code: int, diff_sha: str, paths: list[str], ts: float,
                 after_dispatch_ts: float) -> dict:
    return {
        "result": "GREEN" if exit_code == 0 else "RED",
        "gate": gate,
        "exit_code": int(exit_code),
        "diff_sha": diff_sha,
        "paths": list(paths or []),
        "ts": float(ts),
        "after_dispatch_ts": float(after_dispatch_ts),
    }


def verdict_is_fresh_green(verdict: dict | None, last_dispatch_ts: float) -> bool:
    """A verdict may close a loop only if it is GREEN and was stamped at or after the last
    dispatch (inclusive: verify always runs strictly after a real dispatch completes, so `>=`
    only matters for coincident timestamps, e.g. in tests)."""
    if not isinstance(verdict, dict):
        return False
    return verdict.get("result") == "GREEN" and float(verdict.get("ts", 0)) >= float(last_dispatch_ts or 0)


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def guards_off() -> bool:
    return _as_bool(os.environ.get("FRONTIER_GUARDS_OFF")) or _as_bool(os.environ.get("CLAUDE_GUARDS_OFF"))


# --------------------------------------------------------------------------- #
# Command builders + runner
# --------------------------------------------------------------------------- #
def _split_command_override(value: str | None, name: str) -> list[str] | None:
    if value is None or value == "":
        return None
    if not value.strip():
        raise ValueError(f"{name} is blank")
    command = shlex.split(value)
    if not command:
        raise ValueError(f"{name} does not contain a command")
    return command


def build_codex_command(cfg: dict) -> list[str]:
    """Build the Codex BODY command from the effective config.

    Default (0.2.6+): `codex exec -c model_reasoning_effort=<e> -` — inherits Codex's own
    permission defaults. Prompt is fed on stdin (`-`; run_engine handles stdin).
    Autonomously elevated permissions require explicit opt-in: FRONTIER_CODEX_YOLO=1 adds --yolo.
    fast=on swaps effort->fast_effort and model->fast_model when it is not null. An empty
    fast_model explicitly omits --model to select Codex's account-aware default.
    No --model flag is added unless codex_model/fast_model is explicitly set — Codex's own
    account-aware default keeps working as OpenAI ships new releases.
    Whole-command override: FRONTIER_CODEX_CMD (trusted compatibility input, e.g. `echo` in tests).
    """
    override = _split_command_override(os.environ.get("FRONTIER_CODEX_CMD"), "FRONTIER_CODEX_CMD")
    if override is not None:
        return override
    effort = cfg["fast_effort"] if cfg.get("fast") else cfg["codex_effort"]
    if cfg.get("fast"):
        model = cfg.get("fast_model")
        if model is None:
            model = cfg["codex_model"]
    else:
        model = cfg["codex_model"]
    # Opt-in only: default False so --yolo is never added unless explicitly enabled.
    yolo = ["--yolo"] if _as_bool(os.environ.get("FRONTIER_CODEX_YOLO"), False) else []
    model_flag = ["--model", model] if model else []
    return ["codex", "exec", *yolo, *model_flag, "-c", f"model_reasoning_effort={effort}", "-"]


def effective_frontier_model(cfg: dict) -> str:
    """Return the effective frontier model selected by build_frontier_command policy.

    Empty Codex pin is reported as ``account default`` (never as Claude Fable). Claude, Grok,
    and Gemini apply their builder defaults when the pin is empty.
    """
    provider = str(cfg.get("frontier_provider") or "claude").lower()
    pinned = str(cfg.get("frontier_model") or "")
    if provider == "claude":
        return pinned or "claude-fable-5"
    if provider == "codex":
        return pinned if pinned else "account default"
    if provider == "grok":
        return pinned or "grok-4.5"
    if provider == "gemini":
        return pinned or "gemini-3.5-flash"
    if provider == "openrouter":
        return pinned or "openrouter/auto"
    return pinned or "account default"


def build_frontier_command(cfg: dict) -> list[str]:
    """Build the managed frontier/advisor consult command. Override with FRONTIER_ADVISOR_CMD.

    This is a managed consult only: it does not replace the host harness session lead.
    """
    override = _split_command_override(
        os.environ.get("FRONTIER_ADVISOR_CMD"), "FRONTIER_ADVISOR_CMD"
    )
    if override is not None:
        return override
    provider = str(cfg.get("frontier_provider") or "claude").lower()
    model = str(cfg.get("frontier_model") or "")
    if provider == "claude":
        return ["claude", "-p", "--model", effective_frontier_model(cfg)]
    if provider == "codex":
        # Empty pin omits --model so Codex keeps its account-aware default.
        model_flag = ["--model", model] if model else []
        return ["codex", "exec", *model_flag, "-"]
    if provider == "grok":
        return ["grok", "--model", effective_frontier_model(cfg), "--prompt-file", "{prompt_file}"]
    if provider == "gemini":
        return ["gemini", "--model", effective_frontier_model(cfg), "--prompt", ""]
    if provider == "openrouter":
        return build_openrouter_command(cfg, model=effective_frontier_model(cfg))
    raise ValueError(f"unknown frontier provider {provider!r}")


def build_claude_command(cfg: dict) -> list[str]:
    """Build a Claude executor command. Override with FRONTIER_CLAUDE_CMD."""
    override = _split_command_override(
        os.environ.get("FRONTIER_CLAUDE_CMD"), "FRONTIER_CLAUDE_CMD"
    )
    if override is not None:
        return override
    return ["claude", "-p", "--model", cfg.get("claude_model") or "claude-sonnet-5"]


def build_grok_command(cfg: dict) -> list[str]:
    """Build the Grok Build lead/body command. Override with FRONTIER_GROK_CMD.

    Default (0.2.6+): no --permission-mode (inherits Grok's provider defaults).
    Opt-in autonomy: FRONTIER_GROK_YOLO=1 adds --permission-mode bypassPermissions.
    Explicit mode: FRONTIER_GROK_PERMISSION_MODE=<mode> always wins when set.
    Prompt transport: managed owner-only temp file via {prompt_file}.
    """
    override = _split_command_override(os.environ.get("FRONTIER_GROK_CMD"), "FRONTIER_GROK_CMD")
    if override is not None:
        return override
    effort = cfg["fast_effort"] if cfg.get("fast") else cfg.get("grok_effort", "high")
    if effort not in GROK_EFFORT_LEVELS:
        raise ValueError(f"Grok reasoning effort must be one of {sorted(GROK_EFFORT_LEVELS)}")
    permission = os.environ.get("FRONTIER_GROK_PERMISSION_MODE")
    # Opt-in only: default False so bypassPermissions is never added unless enabled.
    if permission is None and _as_bool(os.environ.get("FRONTIER_GROK_YOLO"), False):
        permission = "bypassPermissions"
    permission_flags = ["--permission-mode", permission] if permission else []
    return [
        "grok",
        "--model", cfg.get("grok_model") or "grok-4.5",
        "--reasoning-effort", effort,
        *permission_flags,
        "--prompt-file", "{prompt_file}",
    ]


def build_gemini_command(cfg: dict) -> list[str]:
    """Build a Gemini CLI executor command. Override with FRONTIER_GEMINI_CMD."""
    override = _split_command_override(
        os.environ.get("FRONTIER_GEMINI_CMD"), "FRONTIER_GEMINI_CMD"
    )
    if override is not None:
        return override
    return [
        "gemini", "--model", cfg.get("gemini_model") or "gemini-3.5-flash",
        "--prompt", "", "--output-format", "text",
    ]


def build_openrouter_command(cfg: dict, *, model: str | None = None) -> list[str]:
    """Build OpenRouter transport argv (stdlib helper). Override with FRONTIER_OPENROUTER_CMD."""
    override = _split_command_override(
        os.environ.get("FRONTIER_OPENROUTER_CMD"), "FRONTIER_OPENROUTER_CMD"
    )
    if override is not None:
        return override
    helper = str(Path(__file__).resolve().parent / "frontier_openrouter.py")
    mid = model if model is not None else (cfg.get("openrouter_model") or "openrouter/auto")
    return [
        sys.executable,
        helper,
        "--model",
        str(mid),
        "--prompt-file",
        "{prompt_file}",
    ]


def build_body_command(cfg: dict) -> list[str]:
    """Build the BODY/EXECUTOR command for the selected engine.

    Universal override: FRONTIER_BODY_CMD / FRONTIER_EXECUTOR_CMD (trusted whole-command inputs).
    Per-provider overrides: FRONTIER_CODEX_CMD / FRONTIER_CLAUDE_CMD / FRONTIER_GROK_CMD /
    FRONTIER_GEMINI_CMD.
    Unknown executor values fail closed (ValueError) instead of falling through to Codex.
    """
    body_override = _split_command_override(
        os.environ.get("FRONTIER_BODY_CMD"), "FRONTIER_BODY_CMD"
    )
    if body_override is not None:
        return body_override
    executor_override = _split_command_override(
        os.environ.get("FRONTIER_EXECUTOR_CMD"), "FRONTIER_EXECUTOR_CMD"
    )
    if executor_override is not None:
        return executor_override
    executor = (cfg.get("executor") or "codex").lower().strip()
    if executor == "codex":
        return build_codex_command(cfg)
    if executor == "claude":
        return build_claude_command(cfg)
    if executor == "grok":
        return build_grok_command(cfg)
    if executor == "gemini":
        return build_gemini_command(cfg)
    if executor == "openrouter":
        return build_openrouter_command(cfg)
    raise ValueError(
        f"unknown executor {executor!r}; expected one of {sorted(KNOWN_EXECUTORS)} "
        f"(or set FRONTIER_BODY_CMD / FRONTIER_EXECUTOR_CMD as a whole-command override)"
    )


def _apply_prompt(cmd: list[str], prompt: str) -> tuple[list[str], str | None]:
    """If cmd contains a literal {prompt}, substitute it (no stdin). Else feed prompt on stdin."""
    if any("{prompt}" in a for a in cmd):
        return [a.replace("{prompt}", prompt) for a in cmd], None
    return cmd, prompt  # feed the prompt on stdin (codex exec `-`, claude -p, …)


def _prepare_prompt_command(cmd: list[str], prompt: str) -> tuple[list[str], str | None, list[str]]:
    """Prepare a command for execution, including managed owner-only temp files for {prompt_file}."""
    if any("{prompt_file}" in a for a in cmd):
        fd, tmp_name = tempfile.mkstemp(prefix="frontier-prompt-", suffix=".txt")
        try:
            try:
                os.fchmod(fd, OWNER_ONLY_FILE)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(prompt)
                tmp.flush()
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        final = [a.replace("{prompt_file}", tmp_name) for a in cmd]
        return final, None, [tmp_name]
    final, stdin = _apply_prompt(cmd, prompt)
    return final, stdin, []


# --------------------------------------------------------------------------- #
# Bounded subprocess capture (shared by provider execution + verification gates)
# --------------------------------------------------------------------------- #
# Hard byte caps during pipe read. Excess is drained without retention so a hostile
# child cannot force unbounded memory growth. Limits are validated before launch.
# POSIX-only process-group lifecycle (Linux/macOS): leader exit is not group exit.
PROVIDER_CAPTURE_MAX_BYTES_DEFAULT = 8 * 1024 * 1024  # provider/engine transcripts
GATE_CAPTURE_MAX_BYTES_DEFAULT = 2 * 1024 * 1024  # gate/test output (verdicts keep tails)
CAPTURE_MAX_BYTES_HARD_CEILING = 64 * 1024 * 1024
_CAPTURE_READ_CHUNK = 64 * 1024
_CAPTURE_DRAIN_AFTER_KILL_S = 2.0
# Bounded window after the direct child exits while descendants may still hold pipes.
_CAPTURE_POST_EXIT_DRAIN_S = 0.75
_CAPTURE_GROUP_TERM_GRACE_S = 2.0
# Max select slice so leader exit is observed while pipes stay silent.
_CAPTURE_LEADER_POLL_SLICE_S = 0.2
# Reserved truncation evidence token. Child-supplied occurrences are disambiguated
# before any authentic parent-appended marker is added.
_TRUNCATION_MARKER_PREFIX = "[frontierfuse:capture_truncated"
# Zero-width non-joiner breaks the reserved token in child-controlled text only.
_TRUNCATION_MARKER_CHILD_DISAMBIG = "[frontierfuse:capture_truncation\u200c"


def validate_capture_max_bytes(value: object, *, name: str = "max_bytes") -> int:
    """Validate a per-stream byte cap before process launch.

    Rejects bools, non-integers, non-positive values, and limits above the hard ceiling.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer byte limit")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    if value > CAPTURE_MAX_BYTES_HARD_CEILING:
        raise ValueError(
            f"{name} exceeds hard ceiling of {CAPTURE_MAX_BYTES_HARD_CEILING} bytes"
        )
    return value


def _parse_capture_max_bytes_env(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        return validate_capture_max_bytes(default, name=env_name)
    try:
        parsed = int(str(raw).strip(), 10)
    except ValueError as exc:
        raise ValueError(
            f"{env_name} must be a positive integer byte limit"
        ) from exc
    return validate_capture_max_bytes(parsed, name=env_name)


def provider_capture_max_bytes() -> int:
    """Resolved provider/engine per-stream capture limit (env-overridable)."""
    return _parse_capture_max_bytes_env(
        "FRONTIER_PROVIDER_CAPTURE_MAX_BYTES",
        PROVIDER_CAPTURE_MAX_BYTES_DEFAULT,
    )


def gate_capture_max_bytes() -> int:
    """Resolved verification-gate per-stream capture limit (env-overridable)."""
    return _parse_capture_max_bytes_env(
        "FRONTIER_GATE_CAPTURE_MAX_BYTES",
        GATE_CAPTURE_MAX_BYTES_DEFAULT,
    )


def capture_truncation_marker(
    *,
    stream: str,
    retained_bytes: int,
    discarded_bytes: int,
) -> str:
    """Deterministic truncation evidence with no prompt, path, or secret content."""
    return (
        f"{_TRUNCATION_MARKER_PREFIX} stream={stream} "
        f"retained_bytes={int(retained_bytes)} discarded_bytes={int(discarded_bytes)}]"
    )


def _disambiguate_child_truncation_markers(text: str) -> str:
    """Neutralize child-supplied reserved markers so only parent appends are authentic."""
    if _TRUNCATION_MARKER_PREFIX not in text:
        return text
    return text.replace(_TRUNCATION_MARKER_PREFIX, _TRUNCATION_MARKER_CHILD_DISAMBIG)


def _require_posix_process_group_capture() -> None:
    """This release supports POSIX only; fail before launch on other platforms."""
    if os.name != "posix":
        raise OSError(
            getattr(errno, "ENOSYS", 38),
            "run_bounded_subprocess requires POSIX; "
            "Windows process-tree capture is not supported in this release",
        )
    if not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        raise OSError(
            getattr(errno, "ENOSYS", 38),
            "run_bounded_subprocess requires os.killpg/os.getpgid (POSIX process groups)",
        )


# --------------------------------------------------------------------------- #
# Descendant containment (Linux PR_SET_CHILD_SUBREAPER supervisor)
# --------------------------------------------------------------------------- #
# Process-group kill alone cannot stop a child that calls setsid() (new session).
# A dedicated per-run supervisor enables PR_SET_CHILD_SUBREAPER so escaped
# descendants reparent to that supervisor (not init/PID1). The supervisor then
# terminates them by pid and process group, reaps, and verifies quiescence before
# reporting success. Only the short-lived supervisor is a subreaper (no
# process-global adoption that could steal unrelated children). Fail closed when
# full containment cannot be enforced.
PR_SET_CHILD_SUBREAPER = 36
_CONTAINMENT_TERM_GRACE_S = 2.0
_CONTAINMENT_KILL_GRACE_S = 1.5
_CONTAINMENT_QUIESCE_POLL_S = 0.05
_CONTAINMENT_SUPERVISOR_FLAG = "--frontierfuse-containment-supervisor"


class ContainmentError(OSError):
    """Raised when descendant containment cannot be established or verified."""


def _linux_prctl_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return hasattr(libc, "prctl")
    except Exception:
        return False


def descendant_containment_supported() -> bool:
    """True when full per-run descendant containment can be enforced."""
    return (
        _linux_prctl_available()
        and hasattr(os, "killpg")
        and hasattr(os, "getpgid")
        and os.path.isdir("/proc")
    )


def _require_descendant_containment() -> None:
    """Fail closed before launch when full descendant containment is unavailable."""
    _require_posix_process_group_capture()
    if not descendant_containment_supported():
        raise ContainmentError(
            getattr(errno, "ENOSYS", 38),
            "run_bounded_subprocess requires Linux PR_SET_CHILD_SUBREAPER "
            "descendant containment; refusing to launch without it",
        )


def _prctl_set_child_subreaper(enabled: bool = True) -> None:
    """Enable/disable child subreaper on *this* process only (per-run supervisor)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    rc = libc.prctl(
        int(PR_SET_CHILD_SUBREAPER),
        1 if enabled else 0,
        0,
        0,
        0,
    )
    if rc != 0:
        err = ctypes.get_errno() or getattr(errno, "EINVAL", 22)
        raise ContainmentError(err, "prctl(PR_SET_CHILD_SUBREAPER) failed")


def _parse_proc_stat_ppid(stat_raw: bytes) -> int | None:
    """Parse ppid from /proc/<pid>/stat; comm may contain spaces and parentheses."""
    try:
        rparen = stat_raw.rfind(b")")
        if rparen < 0:
            return None
        rest = stat_raw[rparen + 2 :].split()
        return int(rest[1])
    except (IndexError, ValueError):
        return None


def _parse_proc_stat_starttime(stat_raw: bytes) -> int | None:
    """Parse starttime (field 22) from /proc/<pid>/stat for pid-reuse checks."""
    try:
        rparen = stat_raw.rfind(b")")
        if rparen < 0:
            return None
        rest = stat_raw[rparen + 2 :].split()
        # After comm: state(0) ppid(1) ... starttime is index 19 in rest
        # (linux proc(5): fields 3.. with 22 = starttime => rest index 19)
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def _read_pid_starttime(pid: int) -> int | None:
    """Return /proc starttime for *pid*, or None if the pid is gone/unreadable."""
    if pid <= 1:
        return None
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return _parse_proc_stat_starttime(raw)


def _pid_matches_identity(pid: int, starttime: int | None) -> bool:
    """True if *pid* is alive and (when known) still has *starttime* (not reused)."""
    if pid <= 1:
        return False
    cur = _read_pid_starttime(pid)
    if cur is None:
        return False
    if starttime is None:
        # Without a launch-time identity, refuse to signal (fail closed for reuse risk).
        return False
    return int(cur) == int(starttime)


def _list_direct_child_pids(parent_pid: int | None = None) -> set[int]:
    """Linux /proc: pids whose PPid equals *parent_pid* (default: self)."""
    me = int(parent_pid) if parent_pid is not None else os.getpid()
    kids: set[int] = set()
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid <= 1 or pid == me:
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            ppid = _parse_proc_stat_ppid(raw)
            if ppid == me:
                kids.add(pid)
    except OSError:
        pass
    return kids


def _list_descendant_pids(root_pid: int) -> set[int]:
    """All living descendants of *root_pid* via /proc PPid walk (BFS)."""
    found: set[int] = set()
    frontier = set(_list_direct_child_pids(root_pid))
    while frontier:
        pid = frontier.pop()
        if pid in found or pid == root_pid:
            continue
        found.add(pid)
        frontier |= _list_direct_child_pids(pid) - found
    return found


def _signal_pids_and_groups(
    pids: set[int],
    sig: int,
    *,
    protect_pgid: int | None = None,
    expected_ppid: int | None = None,
    starttimes: dict[int, int] | None = None,
) -> None:
    """Signal each pid and (when safe) its process group.

    Process-group signaling covers setsid() leaders that left the original session.
    Never signal *protect_pgid* (typically the supervisor's own pgid): killing that
    group would terminate the supervisor mid-cleanup when a non-setsid descendant
    still shares the session.

    PID-reuse hardening:
    - If *expected_ppid* is set, re-check /proc ppid before signaling (skip reparented/reused).
    - If *starttimes* has an entry for a pid, require matching /proc starttime.
    - killpg only when the target group still reports live members.
    """
    if protect_pgid is None:
        try:
            protect_pgid = os.getpgrp()
        except OSError:
            protect_pgid = -1
    for pid in list(pids):
        if pid <= 1:
            continue
        try:
            with open(f"/proc/{int(pid)}/stat", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if expected_ppid is not None:
            ppid = _parse_proc_stat_ppid(raw)
            if ppid != int(expected_ppid):
                continue
        if starttimes is not None and int(pid) in starttimes:
            cur_st = _parse_proc_stat_starttime(raw)
            if cur_st is None or int(cur_st) != int(starttimes[int(pid)]):
                continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            pgid = os.getpgid(pid)
            if (
                pgid > 1
                and pgid != protect_pgid
                and _process_group_alive(pgid)
            ):
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _reap_any_children() -> None:
    """Non-blocking reap of all available zombie children."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break


def _quiesce_descendants(
    *,
    root_pid: int | None = None,
    term_grace_s: float = _CONTAINMENT_TERM_GRACE_S,
    kill_grace_s: float = _CONTAINMENT_KILL_GRACE_S,
) -> bool:
    """TERM then KILL all descendants of *root_pid* (default self); reap; verify none remain.

    When *root_pid* is self (subreaper), reparented orphans appear as direct children.
    Returns True only when no descendants remain.
    """
    root = int(root_pid) if root_pid is not None else os.getpid()
    self_mode = root == os.getpid()

    def _alive() -> set[int]:
        if self_mode:
            _reap_any_children()
            return _list_direct_child_pids(root)
        return _list_descendant_pids(root)

    term_end = time.monotonic() + max(0.0, float(term_grace_s))
    while True:
        kids = _alive()
        if not kids:
            return True
        if time.monotonic() >= term_end:
            break
        exp_ppid = root if self_mode else None
        st_map = {pid: st for pid in kids if (st := _read_pid_starttime(pid)) is not None}
        _signal_pids_and_groups(
            kids, signal.SIGTERM, expected_ppid=exp_ppid, starttimes=st_map
        )
        time.sleep(_CONTAINMENT_QUIESCE_POLL_S)

    kill_end = time.monotonic() + max(0.0, float(kill_grace_s))
    while True:
        kids = _alive()
        if not kids:
            return True
        if time.monotonic() >= kill_end:
            break
        exp_ppid = root if self_mode else None
        st_map = {pid: st for pid in kids if (st := _read_pid_starttime(pid)) is not None}
        _signal_pids_and_groups(
            kids, signal.SIGKILL, expected_ppid=exp_ppid, starttimes=st_map
        )
        time.sleep(_CONTAINMENT_QUIESCE_POLL_S)

    kids = _alive()
    if kids:
        exp_ppid = root if self_mode else None
        st_map = {pid: st for pid in kids if (st := _read_pid_starttime(pid)) is not None}
        _signal_pids_and_groups(
            kids, signal.SIGKILL, expected_ppid=exp_ppid, starttimes=st_map
        )
        time.sleep(_CONTAINMENT_QUIESCE_POLL_S)
        kids = _alive()
    return not kids


_CONTAINMENT_RESULT_FD_ENV = "FRONTIERFUSE_CONTAINMENT_RESULT_FD"
_CONTAINMENT_SEAL_ENV = "FRONTIERFUSE_CONTAINMENT_SEAL"
_CONTAINMENT_RECEIPT_SCHEMA = 1


def _strip_containment_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Return env for the worker without containment IPC secrets.

    None means \"inherit cleaned os.environ\" so secrets never leak to the worker.
    """
    src = dict(os.environ) if env is None else dict(env)
    for key in list(src):
        if key.startswith("FRONTIERFUSE_CONTAINMENT_"):
            del src[key]
    return src


def _seal_containment_receipt(secret: bytes, payload: dict) -> str:
    body = {k: payload[k] for k in sorted(payload) if k != "seal"}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()


def _write_containment_receipt_fd(fd: int, payload: dict, secret: bytes) -> None:
    """Write one sealed JSON receipt to *fd* (supervisor -> parent pipe)."""
    out = dict(payload)
    out["schema_version"] = int(_CONTAINMENT_RECEIPT_SCHEMA)
    out["seal"] = _seal_containment_receipt(secret, out)
    data = json.dumps(out, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        if n <= 0:
            break
        view = view[n:]


def _read_containment_receipt_fd(fd: int, *, max_bytes: int = 65536) -> dict | None:
    """Read one JSON receipt from parent end of the containment pipe."""
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        try:
            piece = os.read(fd, min(4096, max_bytes - total))
        except InterruptedError:
            continue
        except OSError:
            break
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
        if b"\n" in piece:
            break
    if not chunks:
        return None
    raw = b"".join(chunks).split(b"\n", 1)[0].strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# Back-compat name used by a few monketests / older patches.
def _read_containment_result(path: str) -> dict | None:
    """Deprecated file-path reader; containment receipts are pipe-sealed only."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _strict_int_not_bool(value: object, *, field: str) -> int:
    if type(value) is not int:  # bool is a subclass of int — reject it
        raise ContainmentError(
            getattr(errno, "EINVAL", 22),
            f"containment receipt {field} must be int (not {type(value).__name__})",
        )
    return value


def _accept_containment_receipt(
    result: dict | None,
    *,
    root_pid: int,
    root_starttime: int | None,
    receipt_mac: bytes,
    supervisor_returncode: int | None,
    require_containment_ok: bool = True,
) -> int:
    """Validate sealed supervisor receipt; return worker_rc.

    Fail closed on missing/forged schema, pid identity mismatch, non-zero
    supervisor exit when success is required, truthy non-bool containment_ok,
    or bool-as-int worker_rc.
    """
    if result is None:
        raise ContainmentError(
            getattr(errno, "ECHILD", 10),
            "containment result missing; refusing success",
        )
    if supervisor_returncode is None:
        raise ContainmentError(
            getattr(errno, "ECHILD", 10),
            "supervisor returncode unknown; refusing success",
        )
    if require_containment_ok and int(supervisor_returncode) != 0:
        err = str(result.get("error") or "supervisor exited non-zero")
        raise ContainmentError(
            getattr(errno, "ECHILD", 10),
            f"containment supervisor exit {supervisor_returncode}: {err}",
        )
    try:
        schema = _strict_int_not_bool(result.get("schema_version"), field="schema_version")
    except ContainmentError:
        raise ContainmentError(
            getattr(errno, "EINVAL", 22),
            "containment receipt missing/invalid schema_version",
        )
    if schema != int(_CONTAINMENT_RECEIPT_SCHEMA):
        raise ContainmentError(
            getattr(errno, "EINVAL", 22),
            f"containment receipt schema_version {schema} unsupported",
        )
    if not _verify_containment_seal(receipt_mac, result):
        raise ContainmentError(
            getattr(errno, "EPERM", 1),
            "containment receipt seal invalid; possible forge",
        )
    try:
        sup_pid = _strict_int_not_bool(result.get("supervisor_pid"), field="supervisor_pid")
    except ContainmentError as exc:
        raise ContainmentError(
            getattr(errno, "EINVAL", 22),
            "containment receipt supervisor_pid invalid",
        ) from exc
    if int(sup_pid) != int(root_pid):
        raise ContainmentError(
            getattr(errno, "EPERM", 1),
            f"containment receipt supervisor_pid {sup_pid} != launched {root_pid}",
        )
    try:
        sup_st = _strict_int_not_bool(
            result.get("supervisor_starttime"), field="supervisor_starttime"
        )
    except ContainmentError as exc:
        raise ContainmentError(
            getattr(errno, "EINVAL", 22),
            "containment receipt supervisor_starttime invalid",
        ) from exc
    if root_starttime is None or int(sup_st) != int(root_starttime):
        raise ContainmentError(
            getattr(errno, "EPERM", 1),
            "containment receipt supervisor_starttime mismatch (pid reuse?)",
        )
    # Identity equality — never truthiness.
    if result.get("containment_ok") is not True:
        if require_containment_ok:
            err = str(result.get("error") or "containment cleanup failed")
            raise ContainmentError(
                getattr(errno, "ECHILD", 10),
                f"containment failed: {err}",
            )
        # timeout/error path may accept a sealed failure receipt for evidence only
        try:
            return _strict_int_not_bool(result.get("worker_rc", 1), field="worker_rc")
        except ContainmentError:
            return 1
    worker_rc = _strict_int_not_bool(result.get("worker_rc"), field="worker_rc")
    return int(worker_rc)


def _verify_containment_seal(secret: bytes, payload: dict) -> bool:
    seal = payload.get("seal")
    if not isinstance(seal, str) or not seal:
        return False
    expected = _seal_containment_receipt(secret, payload)
    try:
        return hmac.compare_digest(expected, seal)
    except (TypeError, ValueError):
        return False


def _containment_supervisor_main(job_path: str) -> int:
    """Per-run subreaper supervisor entry (invoked in a dedicated process).

    Job JSON keys: args (list|str), shell (bool), cwd (str|null).
    Receipt IPC is **not** in the job file (worker-discoverable). Parent passes
    a write-end FD + seal secret via FRONTIERFUSE_CONTAINMENT_* env vars; the
    worker is spawned with those keys stripped and close_fds=True.
    Supervisor process exit 0 only when containment_ok; worker_rc is in the sealed receipt.
    """
    result_fd_raw = os.environ.get(_CONTAINMENT_RESULT_FD_ENV, "")
    seal_hex = os.environ.get(_CONTAINMENT_SEAL_ENV, "")
    try:
        result_fd = int(result_fd_raw)
    except (TypeError, ValueError):
        result_fd = -1
    try:
        receipt_mac = bytes.fromhex(seal_hex) if seal_hex else b""
    except ValueError:
        receipt_mac = b""
    if result_fd < 0 or not receipt_mac:
        try:
            sys.stderr.write("containment supervisor: missing sealed receipt IPC\n")
        except Exception:
            pass
        return 2

    try:
        job_raw = Path(job_path).read_bytes()
        job = json.loads(job_raw.decode("utf-8"))
    except Exception as exc:
        try:
            sys.stderr.write(f"containment supervisor: bad job: {exc}\n")
        except Exception:
            pass
        try:
            os.close(result_fd)
        except OSError:
            pass
        return 2
    if not isinstance(job, dict):
        try:
            os.close(result_fd)
        except OSError:
            pass
        return 2

    # Hardening: refuse worker-visible receipt paths even if an old job smuggles them.
    if "result_path" in job:
        try:
            sys.stderr.write("containment supervisor: refusing job with result_path\n")
        except Exception:
            pass
        try:
            os.close(result_fd)
        except OSError:
            pass
        return 2

    raw_args = job.get("args")
    if isinstance(raw_args, list):
        if not raw_args or not all(isinstance(x, str) for x in raw_args):
            try:
                os.close(result_fd)
            except OSError:
                pass
            return 2
        args: list[str] | str = list(raw_args)
    elif isinstance(raw_args, str):
        args = raw_args
    else:
        try:
            os.close(result_fd)
        except OSError:
            pass
        return 2
    shell = bool(job.get("shell", False))
    cwd_raw = job.get("cwd")
    cwd: str | None = str(cwd_raw) if cwd_raw is not None else None
    # Optional worker env from parent (already free of containment keys).
    worker_env_raw = job.get("worker_env")
    worker_env: dict[str, str] | None
    if isinstance(worker_env_raw, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in worker_env_raw.items()
    ):
        worker_env = _strip_containment_env(worker_env_raw)
    else:
        worker_env = _strip_containment_env(None)

    worker_rc: int | None = None
    containment_ok = False
    error = ""
    worker: subprocess.Popen | None = None
    self_pid = os.getpid()
    self_starttime = _read_pid_starttime(self_pid)

    def _finish(code: int) -> int:
        payload = {
            "worker_rc": worker_rc if type(worker_rc) is int else None,
            "containment_ok": True if containment_ok is True else False,
            "error": error,
            "supervisor_pid": self_pid,
            "supervisor_starttime": self_starttime,
        }
        try:
            _write_containment_receipt_fd(result_fd, payload, receipt_mac)
        except OSError:
            pass
        try:
            os.close(result_fd)
        except OSError:
            pass
        return code

    try:
        _prctl_set_child_subreaper(True)
    except OSError as exc:
        error = f"subreaper enable failed: {exc}"
        return _finish(1)

    # Reduce same-uid /proc/fd snooping surface from the worker.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_DUMPABLE = 4
        libc.prctl(4, 0, 0, 0, 0)
    except Exception:
        pass

    def _term_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        nonlocal worker_rc, containment_ok, error
        try:
            if worker is not None and worker.poll() is None:
                try:
                    worker.terminate()
                except OSError:
                    pass
                try:
                    worker.wait(timeout=0.4)
                except Exception:
                    try:
                        worker.kill()
                    except OSError:
                        pass
            if worker is not None and worker_rc is None:
                try:
                    worker_rc = (
                        int(worker.returncode) if worker.returncode is not None else 1
                    )
                    if type(worker_rc) is not int:
                        worker_rc = 1
                except Exception:
                    worker_rc = 1
            containment_ok = _quiesce_descendants()
            if not containment_ok:
                error = "timeout path: descendants survived cleanup"
            else:
                error = error or "terminated by signal"
        finally:
            # Exit 0 only if fully contained (parent may still treat as timeout).
            os._exit(_finish(0 if containment_ok else 1))

    try:
        signal.signal(signal.SIGTERM, _term_handler)
        signal.signal(signal.SIGINT, _term_handler)
    except OSError:
        pass

    try:
        worker = subprocess.Popen(
            args,
            shell=shell,
            cwd=cwd,
            env=worker_env,
            stdin=None,
            stdout=None,
            stderr=None,
            start_new_session=False,
            close_fds=True,
        )
    except FileNotFoundError:
        error = "worker executable not found"
        containment_ok = _quiesce_descendants()
        return _finish(127 if containment_ok else 1)
    except OSError as exc:
        error = f"worker spawn failed: {exc}"
        containment_ok = _quiesce_descendants()
        return _finish(127 if containment_ok else 1)

    assert worker is not None
    while True:
        try:
            worker_rc = int(worker.wait())
            break
        except InterruptedError:
            continue

    _reap_any_children()
    containment_ok = _quiesce_descendants()
    if not containment_ok:
        error = "descendants remained after TERM/KILL quiesce"
        return _finish(1)
    if _list_direct_child_pids():
        containment_ok = False
        error = "post-quiesce child pids still present"
        return _finish(1)
    return _finish(0)


def _containment_supervisor_entry() -> int:
    """CLI entry: python frontier_common.py --frontierfuse-containment-supervisor <job>."""
    argv = list(sys.argv)
    job_path = None
    if len(argv) >= 3 and argv[1] == _CONTAINMENT_SUPERVISOR_FLAG:
        job_path = argv[2]
    elif len(argv) >= 2 and argv[0] == "-c":
        job_path = argv[1]
    if not job_path:
        return 2
    return _containment_supervisor_main(job_path)


def _build_supervisor_command(job_path: str) -> list[str]:
    """Build argv that re-enters this module as a containment supervisor."""
    module_path = str(Path(__file__).resolve())
    return [
        sys.executable,
        module_path,
        _CONTAINMENT_SUPERVISOR_FLAG,
        job_path,
    ]


def _process_group_alive(pgid: int) -> bool:
    """True if any process remains in *pgid* (leader exit alone does not clear this)."""
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_process_group(
    *,
    pgid: int | None = None,
    proc: subprocess.Popen | None = None,
    root_pid: int | None = None,
    root_starttime: int | None = None,
) -> bool:
    """TERM then KILL process group *and* pid-tree under *root_pid*.

    Does **not** return early merely because the session leader / direct child has
    already exited — descendants may still hold pipes, ignore SIGTERM, or have
    called setsid(). Returns True only when the group appears dead and no
    descendants remain under *root_pid* (when provided).

    PID-reuse safety: *root_pid* is signaled only when *root_starttime* still matches
    /proc starttime. After the supervisor is reaped, numeric identities are not
    trusted for kill without that identity check.
    """

    def _root_identity_live() -> bool:
        if root_pid is None or root_pid <= 1:
            return False
        return _pid_matches_identity(int(root_pid), root_starttime)

    def _collect_targets() -> set[int]:
        found: set[int] = set()
        if _root_identity_live():
            assert root_pid is not None
            found.add(int(root_pid))
            found |= _list_descendant_pids(int(root_pid))
        elif proc is not None and proc.poll() is None and getattr(proc, "pid", None):
            # Live unreaped Popen only — never chase a reaped pid number.
            found.add(int(proc.pid))
            found |= _list_descendant_pids(int(proc.pid))
        return found

    def _is_clean() -> bool:
        if proc is not None and proc.poll() is None:
            return False
        if _root_identity_live():
            return False
        # A still-populated process group means members remain (leader may be dead).
        if pgid is not None and pgid > 0 and _process_group_alive(pgid):
            return False
        return True

    def _signal_group(sig: int) -> None:
        # killpg is only sent when the group still has live members. An empty/reused
        # pgid does not report alive, so we do not signal unrelated recycled groups.
        if pgid is None or pgid <= 0:
            return
        if not _process_group_alive(pgid):
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _signal_targets(targets: set[int], sig: int) -> None:
        if not targets:
            return
        st_map: dict[int, int] = {}
        for pid in targets:
            st = _read_pid_starttime(pid)
            if st is not None:
                st_map[int(pid)] = int(st)
        # Parent-side: starttime identity only (targets may include root itself, whose
        # ppid is the capture parent — not root_pid).
        _signal_pids_and_groups(targets, sig, starttimes=st_map)

    targets = _collect_targets()
    _signal_group(signal.SIGTERM)
    _signal_targets(targets, signal.SIGTERM)

    grace_end = time.monotonic() + _CAPTURE_GROUP_TERM_GRACE_S
    while time.monotonic() < grace_end:
        if _is_clean():
            break
        targets = _collect_targets()
        _signal_targets(targets, signal.SIGTERM)
        _signal_group(signal.SIGTERM)
        time.sleep(0.05)

    if not _is_clean():
        _signal_group(signal.SIGKILL)
        targets = _collect_targets()
        _signal_targets(targets, signal.SIGKILL)
        kill_end = time.monotonic() + 1.0
        while time.monotonic() < kill_end:
            if _is_clean():
                break
            targets = _collect_targets()
            _signal_targets(targets, signal.SIGKILL)
            time.sleep(0.05)

    if proc is not None:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        else:
            try:
                if proc.returncode is None:
                    proc.wait(timeout=0.05)
            except (subprocess.TimeoutExpired, OSError):
                pass

    return _is_clean()


class _StreamCapture:
    """Byte-capped pipe reader: retain up to max_bytes, drain the rest without keeping it."""

    __slots__ = ("name", "max_bytes", "buf", "discarded", "eof")

    def __init__(self, name: str, max_bytes: int) -> None:
        self.name = name
        self.max_bytes = max_bytes
        self.buf = bytearray()
        self.discarded = 0
        self.eof = False

    def feed(self, data: bytes) -> None:
        if not data:
            return
        room = self.max_bytes - len(self.buf)
        if room > 0:
            take = data[:room]
            self.buf.extend(take)
            data = data[room:]
        if data:
            self.discarded += len(data)

    def text(self) -> str:
        body = bytes(self.buf).decode("utf-8", errors="replace")
        # Always neutralize child-supplied reserved tokens before optional authentic marker.
        body = _disambiguate_child_truncation_markers(body)
        if self.discarded <= 0:
            return body
        marker = capture_truncation_marker(
            stream=self.name,
            retained_bytes=len(self.buf),
            discarded_bytes=self.discarded,
        )
        if body and not body.endswith("\n"):
            return body + "\n" + marker
        return body + marker


def _capture_os_read(fd: int, n: int) -> bytes:
    """Single os.read for capture drains. Module-level so contracts can adversarial-monkeypatch
    without intercepting subprocess.Popen's private errpipe reads.
    """
    return os.read(fd, n)


def _capture_read_once(fd: int) -> tuple[bytes | None, bool]:
    """Read up to one capture chunk from *fd*.

    Returns ``(payload, closed)``:
    - ``(data, False)`` when bytes were read (may be partial)
    - ``(None, False)`` on transient not-ready (BlockingIOError / EAGAIN / EWOULDBLOCK /
      InterruptedError) — the fd must stay registered for later output
    - ``(b"", True)`` on true EOF (zero-length read) or permanent error — unregister
    """
    try:
        data = _capture_os_read(fd, _CAPTURE_READ_CHUNK)
    except BlockingIOError:
        return None, False
    except InterruptedError:
        return None, False
    except OSError as exc:
        err = getattr(exc, "errno", None)
        if err in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None, False
        return b"", True
    except ValueError:
        return b"", True
    if data == b"":
        return b"", True
    return data, False


def run_bounded_subprocess(
    args: list[str] | str,
    *,
    input: str | bytes | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
    max_stdout_bytes: int,
    max_stderr_bytes: int | None = None,
    start_new_session: bool = True,
) -> tuple[int, str, str]:
    """Run a subprocess with hard per-stream byte caps and full descendant containment.

    stdout/stderr are retained up to the validated limits; excess bytes are drained and
    discarded (not held). On timeout or KeyboardInterrupt the whole process group and any
    setsid-escaped descendants are terminated via a dedicated Linux subreaper supervisor.
    No daemon reader threads are used.

    Process-group isolation is mandatory: *start_new_session* must be True. Full
    descendant containment also requires Linux ``PR_SET_CHILD_SUBREAPER`` in a per-run
    supervisor process (not process-global adoption). Platforms without this capability
    fail closed before launch.

    Parent-exits-first safety: the supervisor remains alive as subreaper until every
    descendant is reaped. After the direct worker exits, only selector-ready nonblocking
    descriptors are read during a bounded post-exit drain. Transient BlockingIOError/
    EAGAIN keeps the descriptor registered. Success is returned only when the supervisor
    reports containment_ok; cleanup failure raises ContainmentError (never silent success).

    Returns (returncode, stdout_text, stderr_text). Truncation appends a deterministic
    marker that contains only stream name and byte counts — never prompt or secret data.
    Raises subprocess.TimeoutExpired after killing the contained tree when *timeout*
    elapses. Limits are validated before process launch.
    Platform: Linux with PR_SET_CHILD_SUBREAPER (explicit fail-closed otherwise).
    """
    _require_descendant_containment()
    if start_new_session is not True:
        raise ValueError(
            "run_bounded_subprocess requires start_new_session=True "
            "(process-group isolation); refusing to launch without it"
        )
    max_out = validate_capture_max_bytes(max_stdout_bytes, name="max_stdout_bytes")
    max_err = validate_capture_max_bytes(
        max_out if max_stderr_bytes is None else max_stderr_bytes,
        name="max_stderr_bytes",
    )
    if timeout is not None:
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be a finite number of seconds") from exc
        if not math.isfinite(timeout_f) or timeout_f < 0:
            raise ValueError("timeout must be a finite number of seconds >= 0")
    else:
        timeout_f = None

    stdin_data: bytes | None
    if input is None:
        stdin_data = None
    elif isinstance(input, bytes):
        stdin_data = input
    else:
        stdin_data = str(input).encode("utf-8")

    job_dir = tempfile.mkdtemp(prefix="ff-containment-")
    receipt_r: int | None = None
    receipt_w: int | None = None
    receipt_mac = secrets.token_bytes(32)
    try:
        os.chmod(job_dir, 0o700)
    except OSError:
        pass
    job_path = str(Path(job_dir) / "job.json")
    # Never put result_path or seal material in the worker-visible job file.
    # Only pass an explicit worker env map when the caller supplied one; otherwise the
    # supervisor derives a cleaned environ at spawn time (without dumping os.environ into job.json).
    job_payload: dict = {
        "args": args,
        "shell": bool(shell),
        "cwd": str(cwd) if cwd is not None else None,
    }
    if env is not None:
        job_payload["worker_env"] = _strip_containment_env(dict(env))
    job_bytes = json.dumps(job_payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    jfd = os.open(job_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(jfd, 0o600)
        os.write(jfd, job_bytes)
    finally:
        os.close(jfd)

    super_argv = _build_supervisor_command(job_path)
    # Supervisor env: caller's env (if any) + sealed receipt IPC. Worker never sees these.
    super_env = dict(os.environ) if env is None else dict(env)
    try:
        receipt_r, receipt_w = os.pipe()
        try:
            os.set_inheritable(receipt_w, True)
        except (AttributeError, OSError):
            pass
        # Parent keeps read end non-inheritable.
        try:
            os.set_inheritable(receipt_r, False)
        except (AttributeError, OSError):
            pass
        super_env[_CONTAINMENT_RESULT_FD_ENV] = str(receipt_w)
        super_env[_CONTAINMENT_SEAL_ENV] = receipt_mac.hex()
    except OSError as exc:
        raise ContainmentError(
            getattr(exc, "errno", errno.EIO),
            f"containment receipt pipe setup failed: {exc}",
        ) from exc

    proc: subprocess.Popen[bytes] | None = None
    pgid: int | None = None
    root_pid: int | None = None
    root_starttime: int | None = None
    sel: selectors.BaseSelector | None = None

    def _cleanup_job_dir() -> None:
        try:
            os.unlink(job_path)
        except OSError:
            pass
        try:
            os.rmdir(job_dir)
        except OSError:
            pass
        for fd in (receipt_r, receipt_w):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    def _read_receipt() -> dict | None:
        nonlocal receipt_r
        if receipt_r is None:
            return None
        try:
            return _read_containment_receipt_fd(receipt_r)
        finally:
            try:
                os.close(receipt_r)
            except OSError:
                pass
            receipt_r = None

    def _ensure_contained_or_raise(reason: str) -> None:
        """Best-effort kill + fail if descendants of the supervisor still live."""
        _kill_process_group(
            pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime
        )
        if root_pid is not None and _pid_matches_identity(int(root_pid), root_starttime):
            if _list_descendant_pids(int(root_pid)):
                raise ContainmentError(
                    getattr(errno, "ECHILD", 10),
                    f"{reason}: descendants survived after cleanup",
                )
        # Supervisor already reaped — still scan for obvious leaked job children is N/A;
        # parent only has the supervisor root identity.

    def _timeout_with_containment_proof() -> NoReturn:
        """Signal supervisor to quiesce; require sealed clean receipt before TimeoutExpired."""
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            wait_budget = (
                float(_CONTAINMENT_TERM_GRACE_S)
                + float(_CONTAINMENT_KILL_GRACE_S)
                + 2.0
            )
            try:
                proc.wait(timeout=wait_budget)
            except subprocess.TimeoutExpired:
                _kill_process_group(
                    pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime
                )
                try:
                    proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        drain_deadline = time.monotonic() + _CAPTURE_DRAIN_AFTER_KILL_S
        # drain handled by caller streams if needed — parent already left select loop
        result = _read_receipt()
        try:
            if root_pid is None:
                raise ContainmentError(
                    getattr(errno, "ECHILD", 10),
                    "timeout path: supervisor identity missing",
                )
            # Accept sealed receipt even if containment_ok False for error text, then fail.
            if result is None or result.get("containment_ok") is not True:
                err = ""
                if isinstance(result, dict):
                    err = str(result.get("error") or "")
                _ensure_contained_or_raise("timeout path")
                raise ContainmentError(
                    getattr(errno, "ECHILD", 10),
                    f"timeout path: containment not proven ({err or 'no sealed ok receipt'})",
                )
            # Seal/identity must still bind to the launched supervisor.
            _accept_containment_receipt(
                result,
                root_pid=int(root_pid),
                root_starttime=root_starttime,
                receipt_mac=receipt_mac,
                supervisor_returncode=0 if (proc is not None and proc.returncode == 0) else (
                    int(proc.returncode) if proc is not None and proc.returncode is not None else 1
                ),
                require_containment_ok=True,
            )
        except ContainmentError:
            _ensure_contained_or_raise("timeout path")
            raise
        # Extra belt: no living descendants under a still-alive supervisor identity.
        if root_pid is not None and _pid_matches_identity(int(root_pid), root_starttime):
            if _list_descendant_pids(int(root_pid)):
                raise ContainmentError(
                    getattr(errno, "ECHILD", 10),
                    "timeout path: descendants survived after sealed receipt",
                )
        raise subprocess.TimeoutExpired(
            args,
            timeout_f if timeout_f is not None else 0,
        )

    try:
        try:
            pass_fds = (receipt_w,) if receipt_w is not None else ()
            proc = subprocess.Popen(
                super_argv,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=None,
                env=super_env,
                shell=False,
                start_new_session=True,
                pass_fds=pass_fds,
                close_fds=True,
            )
        except FileNotFoundError:
            raise
        except OSError:
            raise
        finally:
            # Parent must close write end so EOF is visible after supervisor exits.
            if receipt_w is not None:
                try:
                    os.close(receipt_w)
                except OSError:
                    pass
                receipt_w = None

        assert proc is not None
        assert proc.stdout is not None and proc.stderr is not None
        root_pid = int(proc.pid)
        root_starttime = _read_pid_starttime(root_pid)
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = int(proc.pid)

        streams = {
            "stdout": _StreamCapture("stdout", max_out),
            "stderr": _StreamCapture("stderr", max_err),
        }
        fd_map: dict[int, _StreamCapture] = {
            proc.stdout.fileno(): streams["stdout"],
            proc.stderr.fileno(): streams["stderr"],
        }

        def _set_nonblocking(fd: int) -> None:
            try:
                os.set_blocking(fd, False)
            except (AttributeError, OSError, ValueError):
                pass

        for _fd in list(fd_map):
            _set_nonblocking(_fd)

        def _close_stdin_fd(_stdin_fd: int | None) -> None:
            if proc.stdin is None:
                return
            try:
                proc.stdin.close()
            except OSError:
                pass

        def _cleanup_group_and_raise(exc: BaseException) -> NoReturn:
            _kill_process_group(pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime)
            raise exc

        try:
            stdin_fd: int | None = None
            stdin_view: memoryview | None = None
            stdin_offset = 0
            if stdin_data is not None and proc.stdin is not None:
                stdin_fd = proc.stdin.fileno()
                try:
                    os.set_blocking(stdin_fd, False)
                except (AttributeError, OSError, ValueError):
                    pass
                stdin_view = memoryview(stdin_data)

            try:
                sel = selectors.DefaultSelector()
            except Exception as exc:
                _cleanup_group_and_raise(
                    OSError(getattr(errno, "EIO", 5), f"selector setup failed: {exc}")
                )

            try:
                for fd in list(fd_map):
                    try:
                        sel.register(fd, selectors.EVENT_READ)
                    except (OSError, ValueError) as exc:
                        _cleanup_group_and_raise(
                            OSError(
                                getattr(errno, "EIO", 5),
                                f"stdout/stderr selector registration failed: {exc}",
                            )
                        )
                if stdin_fd is not None and stdin_view is not None and len(stdin_view) > 0:
                    try:
                        sel.register(stdin_fd, selectors.EVENT_WRITE)
                    except (OSError, ValueError) as exc:
                        _cleanup_group_and_raise(
                            OSError(
                                getattr(errno, "EIO", 5),
                                f"stdin selector registration failed: {exc}",
                            )
                        )
                elif stdin_fd is not None and (
                    stdin_view is None or len(stdin_view) == 0
                ):
                    _close_stdin_fd(stdin_fd)
                    stdin_fd = None
                    stdin_view = None

                deadline = None if timeout_f is None else (time.monotonic() + timeout_f)
                timed_out = False
                leader_exit_mono: float | None = None
                force_group_cleanup = False

                try:
                    while fd_map or stdin_fd is not None:
                        if leader_exit_mono is None and proc.poll() is not None:
                            leader_exit_mono = time.monotonic()
                            if stdin_fd is not None:
                                try:
                                    sel.unregister(stdin_fd)
                                except (OSError, KeyError, ValueError):
                                    pass
                                _close_stdin_fd(stdin_fd)
                                stdin_fd = None
                                stdin_view = None

                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                if proc.poll() is None:
                                    timed_out = True
                                    break
                                if leader_exit_mono is None:
                                    leader_exit_mono = time.monotonic()
                                remaining = max(
                                    0.0,
                                    _CAPTURE_POST_EXIT_DRAIN_S
                                    - (time.monotonic() - leader_exit_mono),
                                )
                                if remaining <= 0:
                                    force_group_cleanup = True
                                    break
                        else:
                            remaining = None

                        if leader_exit_mono is not None:
                            post_left = _CAPTURE_POST_EXIT_DRAIN_S - (
                                time.monotonic() - leader_exit_mono
                            )
                            if post_left <= 0:
                                force_group_cleanup = True
                                break
                            if remaining is None:
                                remaining = post_left
                            else:
                                remaining = min(remaining, post_left)
                        else:
                            if remaining is None:
                                remaining = _CAPTURE_LEADER_POLL_SLICE_S
                            else:
                                remaining = min(remaining, _CAPTURE_LEADER_POLL_SLICE_S)

                        try:
                            events = sel.select(timeout=remaining)
                        except (OSError, ValueError):
                            force_group_cleanup = True
                            break

                        if not events:
                            if leader_exit_mono is not None:
                                if (
                                    time.monotonic() - leader_exit_mono
                                    >= _CAPTURE_POST_EXIT_DRAIN_S
                                ):
                                    force_group_cleanup = True
                                    break
                                if not fd_map and stdin_fd is None:
                                    break
                            continue

                        for key, mask in events:
                            fd = key.fd
                            if (
                                stdin_fd is not None
                                and fd == stdin_fd
                                and (mask & selectors.EVENT_WRITE)
                            ):
                                assert stdin_view is not None
                                try:
                                    written = os.write(
                                        stdin_fd,
                                        stdin_view[
                                            stdin_offset : stdin_offset
                                            + _CAPTURE_READ_CHUNK
                                        ],
                                    )
                                except BlockingIOError:
                                    written = 0
                                except OSError as exc:
                                    err = getattr(exc, "errno", None)
                                    if err in (errno.EAGAIN, errno.EWOULDBLOCK):
                                        written = 0
                                    else:
                                        written = -1
                                if written < 0:
                                    try:
                                        sel.unregister(stdin_fd)
                                    except (OSError, KeyError, ValueError):
                                        pass
                                    _close_stdin_fd(stdin_fd)
                                    stdin_fd = None
                                    stdin_view = None
                                elif written > 0:
                                    stdin_offset += written
                                    if stdin_offset >= len(stdin_view):
                                        try:
                                            sel.unregister(stdin_fd)
                                        except (OSError, KeyError, ValueError):
                                            pass
                                        _close_stdin_fd(stdin_fd)
                                        stdin_fd = None
                                        stdin_view = None
                                continue

                            if not (mask & selectors.EVENT_READ):
                                continue
                            cap = fd_map.get(fd)
                            if cap is None:
                                continue
                            data, closed = _capture_read_once(fd)
                            if data:
                                cap.feed(data)
                            elif closed:
                                cap.eof = True
                                try:
                                    sel.unregister(fd)
                                except (OSError, KeyError, ValueError):
                                    pass
                                fd_map.pop(fd, None)
                except KeyboardInterrupt:
                    if stdin_fd is not None:
                        _close_stdin_fd(stdin_fd)
                        stdin_fd = None
                    _kill_process_group(pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime)
                    raise
                finally:
                    if stdin_fd is not None:
                        if sel is not None:
                            try:
                                sel.unregister(stdin_fd)
                            except (OSError, KeyError, ValueError):
                                pass
                        _close_stdin_fd(stdin_fd)
                        stdin_fd = None
                    if sel is not None:
                        try:
                            sel.close()
                        except Exception:
                            pass
                        sel = None

                if timed_out:
                    # Drain pipes briefly for TimeoutExpired payload fidelity, then prove cleanup.
                    drain_deadline = time.monotonic() + _CAPTURE_DRAIN_AFTER_KILL_S
                    # First request orderly supervisor quiesce; do not raise timeout until proven.
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    while fd_map and time.monotonic() < drain_deadline:
                        progress = False
                        for fd in list(fd_map):
                            data, closed = _capture_read_once(fd)
                            if data:
                                fd_map[fd].feed(data)
                                progress = True
                            elif closed:
                                del fd_map[fd]
                                progress = True
                            else:
                                if proc.poll() is not None:
                                    del fd_map[fd]
                                    progress = True
                        if not progress:
                            break
                    _timeout_with_containment_proof()

                if proc.poll() is None:
                    try:
                        if deadline is not None:
                            wait_left = max(0.0, deadline - time.monotonic())
                            proc.wait(timeout=wait_left if wait_left > 0 else 0.05)
                        else:
                            proc.wait()
                    except subprocess.TimeoutExpired:
                        _timeout_with_containment_proof()

                # Optional parent-side kill only as belt-and-suspenders; sealed receipt is authority.
                if force_group_cleanup or (
                    pgid is not None and _process_group_alive(pgid)
                ):
                    _kill_process_group(
                        pgid=pgid,
                        proc=proc,
                        root_pid=root_pid,
                        root_starttime=root_starttime,
                    )
                elif fd_map:
                    _kill_process_group(
                        pgid=pgid,
                        proc=proc,
                        root_pid=root_pid,
                        root_starttime=root_starttime,
                    )
                elif root_pid is not None and _list_descendant_pids(root_pid):
                    _kill_process_group(
                        pgid=pgid,
                        proc=proc,
                        root_pid=root_pid,
                        root_starttime=root_starttime,
                    )

                result = _read_receipt()
                if root_pid is None:
                    raise ContainmentError(
                        getattr(errno, "ECHILD", 10),
                        "supervisor pid missing after run",
                    )
                worker_rc = _accept_containment_receipt(
                    result,
                    root_pid=int(root_pid),
                    root_starttime=root_starttime,
                    receipt_mac=receipt_mac,
                    supervisor_returncode=(
                        int(proc.returncode) if proc.returncode is not None else None
                    ),
                    require_containment_ok=True,
                )
                return (
                    int(worker_rc),
                    streams["stdout"].text(),
                    streams["stderr"].text(),
                )
            except BaseException:
                if proc is not None and (
                    proc.poll() is None
                    or (pgid is not None and _process_group_alive(pgid))
                    or (root_pid is not None and _list_descendant_pids(root_pid))
                ):
                    _kill_process_group(pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime)
                raise
        finally:
            if sel is not None:
                try:
                    sel.close()
                except Exception:
                    pass
                sel = None
            if proc is not None:
                if proc.poll() is None or (
                    (pgid is not None and _process_group_alive(pgid))
                    or (root_pid is not None and _list_descendant_pids(root_pid))
                ):
                    _kill_process_group(pgid=pgid, proc=proc, root_pid=root_pid, root_starttime=root_starttime)
                for pipe in (proc.stdout, proc.stderr, proc.stdin):
                    if pipe is None:
                        continue
                    try:
                        pipe.close()
                    except OSError:
                        pass
    finally:
        _cleanup_job_dir()


def run_engine(cmd: list[str], prompt: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a built engine command with the prompt. Returns (returncode, stdout, stderr).

    Provider processes run in their own process group (start_new_session=True). On timeout
    or interruption the whole group is terminated. stdout/stderr are hard byte-capped during
    read via run_bounded_subprocess. Strips FleetFuse-style [artifact:...] lines from stdout
    for clean handoff. Codex keeps stdin transport; Grok keeps managed prompt-file transport
    via _prepare_prompt_command.
    """
    import shutil
    if not cmd or not shutil.which(cmd[0]):
        return 127, "", f"{cmd[0] if cmd else '(empty)'} not on PATH"
    cleanup: list[str] = []
    try:
        final, stdin, cleanup = _prepare_prompt_command(cmd, prompt)
        try:
            max_bytes = provider_capture_max_bytes()
        except ValueError as exc:
            return 2, "", f"invalid provider capture limit: {exc}"
        try:
            rc, stdout, stderr = run_bounded_subprocess(
                final,
                input=stdin,
                timeout=timeout,
                max_stdout_bytes=max_bytes,
                max_stderr_bytes=max_bytes,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        text = "\n".join(
            ln for ln in (stdout or "").splitlines()
            if not ln.strip().startswith("[artifact:")
        ).strip()
        return int(rc), text, (stderr or "").strip()
    finally:
        for path in cleanup:
            try:
                Path(path).unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Artifacts + bounded handoff cards (adapted from FleetFuse fleet_mcp.py, MIT — see NOTICE)
# --------------------------------------------------------------------------- #
MAX_RETURN_CHARS = int(os.environ.get("FRONTIER_MAX_RETURN_CHARS", "1800"))
RUNS_DIR = Path(os.environ.get("FRONTIER_RUNS_DIR", Path.cwd() / "runs"))

_MARKERS = (
    "finding", "risk", "bug", "error", "fail", "pass", "test", "verify", "evidence",
    "source", "file", "line", "recommend", "decision", "summary", "todo", "blocker", "diff",
)


def new_run_id() -> str:
    return (
        f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-"
        f"{os.getpid()}-{uuid.uuid4().hex}"
    )


def clip_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars < 120:
        return text[:max_chars]
    head = max_chars * 2 // 3
    tail = max_chars - head - 40
    return text[:head].rstrip() + "\n\n[... truncated; see raw artifact ...]\n\n" + text[-tail:].lstrip()


def extractive_summary(text: str, max_chars: int = MAX_RETURN_CHARS) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    priority: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith(("#", "-", "*", "|", ">")) or any(m in low for m in _MARKERS):
            priority.append(s)
        if sum(len(x) + 1 for x in priority) >= max_chars:
            break
    return clip_text("\n".join(priority).strip() or text, max_chars)


def write_artifact(base_dir: Path, run_id: str, label: str, task: str, text: str) -> dict:
    raw = text or ""
    digest = hashlib.sha256(raw.encode()).hexdigest() if raw else ""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (label or "body"))[:60] or "body"
    run_dir = Path(base_dir) / f"frontier-{run_id}"
    path = run_dir / f"{safe}.md"
    if raw:
        mkdir_owner_only(run_dir)
        write_text_owner_only(
            path,
            "# FrontierFuse body artifact\n\n"
            f"- run_id: `{run_id}`\n- label: `{label}`\n- task: {task[:240]}\n"
            f"- sha256: `{digest}`\n- bytes: {len(raw.encode())}\n\n## Raw output\n\n{raw}\n",
        )
    return {"path": str(path) if raw else "", "sha256": digest, "bytes": len(raw.encode())}


def write_handoff_card(base_dir: Path, run_id: str, card: dict) -> Path:
    """Persist a bounded handoff card as owner-only JSON under the run directory."""
    label = str(card.get("label") or "body")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in label)[:60] or "body"
    run_dir = mkdir_owner_only(Path(base_dir) / f"frontier-{run_id}")
    path = run_dir / f"{safe}.handoff.json"
    write_json_owner_only(path, card)
    return path


def handoff_card(label: str, task: str, text: str, artifact: dict,
                 max_chars: int = MAX_RETURN_CHARS, ok: bool = True, note: str = "") -> dict:
    """Bounded card returned to the brain: a summary + a pointer to the raw artifact.
    Raw transcripts stay on disk so fanning out N bodies never floods the brain's context."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "label": label,
        "ok": ok,
        "note": note,
        "task": task[:200],
        "summary": extractive_summary(text, max_chars),
        "artifact": artifact.get("path", ""),
        "raw_sha256": artifact.get("sha256", ""),
        "raw_bytes": artifact.get("bytes", 0),
    }


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == _CONTAINMENT_SUPERVISOR_FLAG:
        raise SystemExit(_containment_supervisor_entry())
    import sys
    cfg = resolve_config()
    print("FrontierFuse common — effective config:")
    print(json.dumps(cfg, indent=2, sort_keys=True))
    print("executor       :", cfg["executor"])
    print("body cmd       :", " ".join(build_body_command(cfg)))
    print("frontier consult cmd:", " ".join(build_frontier_command(cfg)))
    print("guards_off     :", guards_off())
    sys.exit(0)
