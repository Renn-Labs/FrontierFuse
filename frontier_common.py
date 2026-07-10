#!/usr/bin/env python3
"""frontier_common.py — shared foundation for FrontierFuse (the contract every module imports).

FrontierFuse pairs two model roles:
  - BODY / EXECUTOR = Codex   (`codex exec -c model_reasoning_effort=high`, no pinned model —
                              Codex's own current account-aware default is used unless
                              FRONTIER_CODEX_MODEL / --model explicitly pins one)
                     or Sonnet / Opus through the Claude CLI, or Grok Build CLI when selected.
  - BRAIN / ADVISOR = Fable 5 (`claude -p --model claude-fable-5`)

Two control flows are built on this foundation:
  - advisor  (default): the selected executor/lead is the main loop and consults Fable ON-DEMAND
                        via `ask_frontier` (frontier_advisor.py / frontier_advisor_mcp.py).
  - orchestrator      : Fable is the in-session main loop and dispatches selected bodies
                        (frontier_dispatch.py) behind a workflow guardrail + deterministic verdict.

This module owns everything shared so the two loops never drift:
  - config toggles + precedence (body/advisor models, effort, fast mode)
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
import signal
import subprocess
import tempfile
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
KNOWN_EXECUTORS = frozenset({"codex", "claude", "grok", "gemini"})
KNOWN_PROFILES = frozenset({"advisor", "orchestrator"})


# --------------------------------------------------------------------------- #
# Config toggles + precedence
# --------------------------------------------------------------------------- #
# Effective config keys (all optional; env/config/session may override defaults):
#   codex_model   body model            (default "" -> unset, Codex CLI's own current default)
#   codex_effort  Codex reasoning effort  low|medium|high (default high)
#   grok_effort   Grok reasoning effort   low|medium|high (default high)
#   fast          bool speed preset      (default False) -> body uses fast_effort/fast_model
#   fast_effort   effort when fast=on    (default low)
#   fast_model    optional lighter body model when fast=on (default "" -> keep codex_model)
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
        "fast_model": "",
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
        if v not in (None, ""):
            out[key] = v
    if os.environ.get("FRONTIER_CODEX_FAST") is not None:
        out["fast"] = _as_bool(os.environ.get("FRONTIER_CODEX_FAST"))
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


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


def write_text_owner_only(path: Path | str, text: str, mode: int = OWNER_ONLY_FILE) -> Path:
    """Atomically write *text* to *path* with owner-only permissions (tmp + replace)."""
    p = Path(path)
    mkdir_owner_only(p.parent)
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


def write_json_owner_only(path: Path | str, data, mode: int = OWNER_ONLY_FILE) -> Path:
    """Atomically write JSON (indent=2, sort_keys) with owner-only permissions."""
    return write_text_owner_only(
        path,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        mode=mode,
    )


def load_global_config() -> dict:
    return {k: v for k, v in _read_json(GLOBAL_CONFIG).items() if k in CONFIG_KEYS}


def save_global_config(cfg: dict) -> None:
    mkdir_owner_only(GLOBAL_CONFIG.parent)
    merged = {**load_global_config(), **{k: v for k, v in cfg.items() if k in CONFIG_KEYS}}
    write_json_owner_only(GLOBAL_CONFIG, merged)


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
    update_mode = str(cfg.get("update_mode") or "passive").lower()
    cfg["update_mode"] = update_mode if update_mode in UPDATE_MODES else "passive"
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
    mkdir_owner_only(p.parent)
    write_json_owner_only(p, st)
    return st


def clear_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
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
def build_codex_command(cfg: dict) -> list[str]:
    """Build the Codex BODY command from the effective config.

    Default (0.2.6+): `codex exec -c model_reasoning_effort=<e> -` — inherits Codex's own
    permission defaults. Prompt is fed on stdin (`-`; run_engine handles stdin).
    Autonomously elevated permissions require explicit opt-in: FRONTIER_CODEX_YOLO=1 adds --yolo.
    fast=on swaps effort->fast_effort and (if set) model->fast_model.
    No --model flag is added unless codex_model/fast_model is explicitly set — Codex's own
    account-aware default keeps working as OpenAI ships new releases.
    Whole-command override: FRONTIER_CODEX_CMD (trusted compatibility input, e.g. `echo` in tests).
    """
    override = os.environ.get("FRONTIER_CODEX_CMD")
    if override:
        return shlex.split(override)
    effort = cfg["fast_effort"] if cfg.get("fast") else cfg["codex_effort"]
    model = (cfg.get("fast_model") or cfg["codex_model"]) if cfg.get("fast") else cfg["codex_model"]
    # Opt-in only: default False so --yolo is never added unless explicitly enabled.
    yolo = ["--yolo"] if _as_bool(os.environ.get("FRONTIER_CODEX_YOLO"), False) else []
    model_flag = ["--model", model] if model else []
    return ["codex", "exec", *yolo, *model_flag, "-c", f"model_reasoning_effort={effort}", "-"]


def build_frontier_command(cfg: dict) -> list[str]:
    """Build the managed frontier/advisor command. Override with FRONTIER_ADVISOR_CMD."""
    override = os.environ.get("FRONTIER_ADVISOR_CMD")
    if override:
        return shlex.split(override)
    provider = str(cfg.get("frontier_provider") or "claude").lower()
    model = str(cfg.get("frontier_model") or "")
    if provider == "claude":
        return ["claude", "-p", "--model", model or "claude-fable-5"]
    if provider == "codex":
        model_flag = ["--model", model] if model else []
        return ["codex", "exec", *model_flag, "-"]
    if provider == "grok":
        return ["grok", "--model", model or "grok-4.5", "--prompt-file", "{prompt_file}"]
    if provider == "gemini":
        return ["gemini", "--model", model or "gemini-3.5-flash", "--prompt", ""]
    raise ValueError(f"unknown frontier provider {provider!r}")


def build_claude_command(cfg: dict) -> list[str]:
    """Build a Claude executor command. Override with FRONTIER_CLAUDE_CMD."""
    override = os.environ.get("FRONTIER_CLAUDE_CMD")
    if override:
        return shlex.split(override)
    return ["claude", "-p", "--model", cfg.get("claude_model") or "claude-sonnet-5"]


def build_grok_command(cfg: dict) -> list[str]:
    """Build the Grok Build lead/body command. Override with FRONTIER_GROK_CMD.

    Default (0.2.6+): no --permission-mode (inherits Grok's provider defaults).
    Opt-in autonomy: FRONTIER_GROK_YOLO=1 adds --permission-mode bypassPermissions.
    Explicit mode: FRONTIER_GROK_PERMISSION_MODE=<mode> always wins when set.
    Prompt transport: managed owner-only temp file via {prompt_file}.
    """
    override = os.environ.get("FRONTIER_GROK_CMD")
    if override:
        return shlex.split(override)
    effort = cfg["fast_effort"] if cfg.get("fast") else cfg.get("grok_effort", "high")
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
    override = os.environ.get("FRONTIER_GEMINI_CMD")
    if override:
        return shlex.split(override)
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
    override = os.environ.get("FRONTIER_BODY_CMD") or os.environ.get("FRONTIER_EXECUTOR_CMD")
    if override:
        return shlex.split(override)
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
    print("frontier brain cmd:", " ".join(build_frontier_command(cfg)))
    print("guards_off     :", guards_off())
    sys.exit(0)
