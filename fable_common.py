#!/usr/bin/env python3
"""fable_common.py — shared foundation for FableFuse (the contract every module imports).

FableFuse pairs two model roles:
  - BODY / EXECUTOR = Codex   (`codex exec -c model_reasoning_effort=high`, no pinned model —
                              Codex's own current account-aware default is used unless
                              FABLE_CODEX_MODEL / --model explicitly pins one)
                     or Sonnet / Opus through the Claude CLI when selected.
  - BRAIN / ADVISOR = Fable 5 (`claude -p --model claude-fable-5`)

Two control flows are built on this foundation:
  - advisor  (default): the selected executor/lead is the main loop and consults Fable ON-DEMAND
                        via `ask_fable` (fable_advisor.py / fable_advisor_mcp.py).
  - orchestrator      : Fable is the in-session main loop and dispatches selected bodies
                        (fable_dispatch.py) behind a hard gate + deterministic verdict.

This module owns everything shared so the two loops never drift:
  - config toggles + precedence (codex model/effort/fast, fable model)
  - per-session state file (arm marker, last_dispatch_ts, verdict, session config)
  - body/lead + Fable command builders and a single run_engine()
  - artifact capture + bounded handoff cards (so N bodies never flood a brain's context)
  - the verdict schema + kill-switch helper

stdlib-only, Python 3.10+, importable (underscore name) for offline contract tests.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_HOME = Path(os.environ.get("FABLE_CONFIG_DIR", Path.home() / ".config" / "fable-fuse"))
GLOBAL_CONFIG = Path(os.environ.get("FABLE_CONFIG", CONFIG_HOME / "config.json"))
STATE_DIR = Path(os.environ.get("FABLE_STATE_DIR", CONFIG_HOME / "state"))


# --------------------------------------------------------------------------- #
# Config toggles + precedence
# --------------------------------------------------------------------------- #
# Effective config keys (all optional; env/config/session may override defaults):
#   codex_model   body model            (default "" -> unset, Codex CLI's own current default)
#   codex_effort  body reasoning effort  low|medium|high (default high)
#   fast          bool speed preset      (default False) -> body uses fast_effort/fast_model
#   fast_effort   effort when fast=on    (default low)
#   fast_model    optional lighter body model when fast=on (default "" -> keep codex_model)
#   fable_model   advisor (brain) model  (default claude-fable-5)
#   executor      body/driver engine     codex|sonnet|opus|custom (default codex)
#   sonnet_model  model when executor=sonnet (default claude-sonnet-5)
#   opus_model    model when executor=opus   (default claude-opus-5)
CONFIG_KEYS = ("codex_model", "codex_effort", "fast", "fast_effort", "fast_model",
               "fable_model", "executor", "sonnet_model", "opus_model")

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
        # default. Set FABLE_CODEX_MODEL / --model to pin a specific release.
        "codex_model": "",
        "codex_effort": "high",
        "fast": False,
        "fast_effort": "low",
        "fast_model": "",
        "fable_model": "claude-fable-5",
        "executor": "codex",
        "sonnet_model": "claude-sonnet-5",
        "opus_model": "claude-opus-5",
    }


def _env_config() -> dict:
    """Config sourced from environment (FABLE_* wins over nothing but loses to file/session/flag)."""
    out: dict = {}
    m = {
        "codex_model": "FABLE_CODEX_MODEL",
        "codex_effort": "FABLE_CODEX_EFFORT",
        "fast_effort": "FABLE_CODEX_FAST_EFFORT",
        "fast_model": "FABLE_CODEX_FAST_MODEL",
        "fable_model": "FABLE_MODEL",
        "executor": "FABLE_EXECUTOR",
        "sonnet_model": "FABLE_SONNET_MODEL",
        "opus_model": "FABLE_OPUS_MODEL",
    }
    for key, env in m.items():
        v = os.environ.get(env)
        if v not in (None, ""):
            out[key] = v
    if os.environ.get("FABLE_CODEX_FAST") is not None:
        out["fast"] = _as_bool(os.environ.get("FABLE_CODEX_FAST"))
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def load_global_config() -> dict:
    return {k: v for k, v in _read_json(GLOBAL_CONFIG).items() if k in CONFIG_KEYS}


def save_global_config(cfg: dict) -> None:
    GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load_global_config(), **{k: v for k, v in cfg.items() if k in CONFIG_KEYS}}
    GLOBAL_CONFIG.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    try:
        GLOBAL_CONFIG.chmod(0o600)
    except OSError:
        pass


def resolve_config(overrides: dict | None = None, session_id: str | None = None) -> dict:
    """Precedence (low -> high): defaults < env < global config file < session config < explicit flags."""
    cfg = defaults()
    cfg.update(_env_config())
    cfg.update(load_global_config())
    if session_id:
        cfg.update({k: v for k, v in read_state(session_id).get("config", {}).items() if k in CONFIG_KEYS})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k in CONFIG_KEYS and v is not None})
    cfg["fast"] = _as_bool(cfg.get("fast"), False)
    cfg["codex_effort"] = str(cfg.get("codex_effort") or "high").lower()
    cfg["fast_effort"] = str(cfg.get("fast_effort") or "low").lower()
    cfg["executor"] = str(cfg.get("executor") or "codex").lower()
    return cfg


# --------------------------------------------------------------------------- #
# Per-session state (arm marker / last_dispatch_ts / verdict / session config)
# --------------------------------------------------------------------------- #
def state_path(session_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (session_id or "default"))[:120]
    return STATE_DIR / f"{safe or 'default'}.json"


def read_state(session_id: str) -> dict:
    st = _read_json(state_path(session_id))
    st.setdefault("armed", False)
    st.setdefault("last_dispatch_ts", 0.0)
    st.setdefault("verdict", None)
    st.setdefault("config", {})
    return st


def write_state(session_id: str, **patch) -> dict:
    st = read_state(session_id)
    if "config" in patch and isinstance(patch["config"], dict):
        merged_cfg = {**st.get("config", {}), **{k: v for k, v in patch.pop("config").items() if k in CONFIG_KEYS}}
        st["config"] = merged_cfg
    st.update(patch)
    p = state_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return st


def clear_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Verdict schema (shared by fable_verify.py and the Stop hook)
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
    return _as_bool(os.environ.get("FABLE_GUARDS_OFF")) or _as_bool(os.environ.get("CLAUDE_GUARDS_OFF"))


# --------------------------------------------------------------------------- #
# Command builders + runner
# --------------------------------------------------------------------------- #
def build_codex_command(cfg: dict) -> list[str]:
    """Build the Codex BODY command from the effective config.

    Proven invocation (per steipete/agent-scripts codex-first): `codex exec --yolo
    -c model_reasoning_effort=<e> -` — the body may run commands/tests (`--yolo`), the
    prompt is fed on stdin (`-`, robust for large specs; run_engine handles stdin).
    fast=on swaps effort->fast_effort and (if set) model->fast_model.
    No --model flag is added unless codex_model/fast_model is explicitly set — Codex's own
    account-aware default keeps working as OpenAI ships new releases, instead of this project
    chasing (and inevitably getting wrong) a fast-moving version string.
    Overrides: FABLE_CODEX_CMD (whole command, e.g. `echo` in tests, or a pyolo-fleet shim);
    FABLE_CODEX_YOLO=0 disables --yolo.
    """
    override = os.environ.get("FABLE_CODEX_CMD")
    if override:
        return shlex.split(override)
    effort = cfg["fast_effort"] if cfg.get("fast") else cfg["codex_effort"]
    model = (cfg.get("fast_model") or cfg["codex_model"]) if cfg.get("fast") else cfg["codex_model"]
    yolo = ["--yolo"] if _as_bool(os.environ.get("FABLE_CODEX_YOLO"), True) else []
    model_flag = ["--model", model] if model else []
    return ["codex", "exec", *yolo, *model_flag, "-c", f"model_reasoning_effort={effort}", "-"]


def build_fable_command(cfg: dict) -> list[str]:
    """Build the Fable ADVISOR/BRAIN command. Override with FABLE_ADVISOR_CMD."""
    override = os.environ.get("FABLE_ADVISOR_CMD")
    if override:
        return shlex.split(override)
    return ["claude", "-p", "--model", cfg.get("fable_model") or "claude-fable-5"]


def build_sonnet_command(cfg: dict) -> list[str]:
    """Build the Sonnet lead/body command. Override with FABLE_SONNET_CMD."""
    override = os.environ.get("FABLE_SONNET_CMD")
    if override:
        return shlex.split(override)
    return ["claude", "-p", "--model", cfg.get("sonnet_model") or "claude-sonnet-5"]


def build_opus_command(cfg: dict) -> list[str]:
    """Build the Opus lead/body command for reverse-advisor mode. Override with FABLE_OPUS_CMD."""
    override = os.environ.get("FABLE_OPUS_CMD")
    if override:
        return shlex.split(override)
    return ["claude", "-p", "--model", cfg.get("opus_model") or "claude-opus-5"]


def build_body_command(cfg: dict) -> list[str]:
    """Build the BODY/EXECUTOR command for the selected engine (cfg['executor'] = codex|sonnet|opus).

    Universal override: FABLE_BODY_CMD / FABLE_EXECUTOR_CMD. Per-engine overrides:
    FABLE_CODEX_CMD / FABLE_SONNET_CMD / FABLE_OPUS_CMD. This is the canonical body command; fable_dispatch
    uses it so the executor is swappable per-session and permanently via config.
    """
    override = os.environ.get("FABLE_BODY_CMD") or os.environ.get("FABLE_EXECUTOR_CMD")
    if override:
        return shlex.split(override)
    executor = (cfg.get("executor") or "codex").lower()
    if executor == "sonnet":
        return build_sonnet_command(cfg)
    if executor == "opus":
        return build_opus_command(cfg)
    return build_codex_command(cfg)


def _apply_prompt(cmd: list[str], prompt: str) -> tuple[list[str], str | None]:
    """If cmd contains a literal {prompt}, substitute it (no stdin). Else append prompt as the
    final positional arg (works for `codex exec PROMPT` and `claude -p PROMPT`)."""
    if any("{prompt}" in a for a in cmd):
        return [a.replace("{prompt}", prompt) for a in cmd], None
    return cmd, prompt  # feed the prompt on stdin (codex exec `-`, claude -p, …)


def run_engine(cmd: list[str], prompt: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a built engine command with the prompt. Returns (returncode, stdout, stderr).
    Strips FleetFuse-style [artifact:...] lines from stdout for clean handoff."""
    import shutil
    if not cmd or not shutil.which(cmd[0]):
        return 127, "", f"{cmd[0] if cmd else '(empty)'} not on PATH"
    final, stdin = _apply_prompt(cmd, prompt)
    try:
        out = subprocess.run(final, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    text = "\n".join(ln for ln in (out.stdout or "").splitlines()
                     if not ln.strip().startswith("[artifact:")).strip()
    return out.returncode, text, (out.stderr or "").strip()


# --------------------------------------------------------------------------- #
# Artifacts + bounded handoff cards (adapted from FleetFuse fleet_mcp.py, MIT — see NOTICE)
# --------------------------------------------------------------------------- #
MAX_RETURN_CHARS = int(os.environ.get("FABLE_MAX_RETURN_CHARS", "1800"))
RUNS_DIR = Path(os.environ.get("FABLE_RUNS_DIR", Path.cwd() / "runs"))

_MARKERS = (
    "finding", "risk", "bug", "error", "fail", "pass", "test", "verify", "evidence",
    "source", "file", "line", "recommend", "decision", "summary", "todo", "blocker", "diff",
)


def new_run_id() -> str:
    return f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


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
    path = Path(base_dir) / f"fable-{run_id}" / f"{safe}.md"
    if raw:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# FableFuse body artifact\n\n"
            f"- run_id: `{run_id}`\n- label: `{label}`\n- task: {task[:240]}\n"
            f"- sha256: `{digest}`\n- bytes: {len(raw.encode())}\n\n## Raw output\n\n{raw}\n"
        )
    return {"path": str(path) if raw else "", "sha256": digest, "bytes": len(raw.encode())}


def handoff_card(label: str, task: str, text: str, artifact: dict,
                 max_chars: int = MAX_RETURN_CHARS, ok: bool = True, note: str = "") -> dict:
    """Bounded card returned to the brain: a summary + a pointer to the raw artifact.
    Raw transcripts stay on disk so fanning out N bodies never floods the brain's context."""
    return {
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
    print("FableFuse common — effective config:")
    print(json.dumps(cfg, indent=2, sort_keys=True))
    print("executor       :", cfg["executor"])
    print("body cmd       :", " ".join(build_body_command(cfg)))
    print("fable brain cmd:", " ".join(build_fable_command(cfg)))
    print("guards_off     :", guards_off())
    sys.exit(0)
