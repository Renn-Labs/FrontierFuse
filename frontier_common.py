#!/usr/bin/env python3
"""frontier_common.py — shared foundation for FrontierFuse (the contract every module imports).

FrontierFuse pairs two model roles. A configured frontier model is a managed consult; the
host-bound harness remains the session lead. Selecting a frontier model does not hot-swap the
host conversation model, and no frontier model (including Claude Fable) is hard-wired.

  - BODY / EXECUTOR = the selected provider (codex|claude|grok|gemini) that performs work.
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

import datetime
import errno
import hashlib
import json
import math
import os
import shlex
import signal
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

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
KNOWN_EXECUTORS = frozenset({"codex", "claude", "grok", "gemini"})
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
               "grok_model", "gemini_model", "grok_effort", "update_mode")
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
    for key in ("codex_model", "frontier_model", "claude_model", "grok_model", "gemini_model"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ValueError(f"invalid {key} in {source}; expected a model ID string")
    if "fast_model" in cfg and cfg["fast_model"] is not None and not isinstance(cfg["fast_model"], str):
        raise ValueError(f"invalid fast_model in {source}; expected null or a model ID string")


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


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate a provider process and its process group (timeout / interrupt path)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_engine(cmd: list[str], prompt: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a built engine command with the prompt. Returns (returncode, stdout, stderr).

    Provider processes run in their own process group (start_new_session=True). On timeout
    or interruption the whole group is terminated. Strips FleetFuse-style [artifact:...]
    lines from stdout for clean handoff. Codex keeps stdin transport; Grok keeps managed
    prompt-file transport via _prepare_prompt_command.
    """
    import shutil
    if not cmd or not shutil.which(cmd[0]):
        return 127, "", f"{cmd[0] if cmd else '(empty)'} not on PATH"
    cleanup: list[str] = []
    proc: subprocess.Popen | None = None
    try:
        final, stdin, cleanup = _prepare_prompt_command(cmd, prompt)
        proc = subprocess.Popen(
            final,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            return 124, "", f"timeout after {timeout}s"
        except KeyboardInterrupt:
            _kill_process_group(proc)
            raise
        text = "\n".join(ln for ln in (stdout or "").splitlines()
                         if not ln.strip().startswith("[artifact:")).strip()
        return int(proc.returncode or 0), text, (stderr or "").strip()
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
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
    import sys
    cfg = resolve_config()
    print("FrontierFuse common — effective config:")
    print(json.dumps(cfg, indent=2, sort_keys=True))
    print("executor       :", cfg["executor"])
    print("body cmd       :", " ".join(build_body_command(cfg)))
    print("frontier consult cmd:", " ".join(build_frontier_command(cfg)))
    print("guards_off     :", guards_off())
    sys.exit(0)
