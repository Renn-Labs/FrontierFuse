#!/usr/bin/env python3
"""frontier_dispatch.py — host-led orchestrator body-caller + control CLI for FrontierFuse.

Orchestrator profile is host-led verified orchestration with managed frontier consult/executor
bodies. The host-bound harness remains the session lead. A configured frontier model is a managed
consult only — selecting it does not hot-swap the host conversation model or make the frontier the
host lead, and no frontier model (including Claude Fable) is hard-wired. The host controller
delegates every execution/research/tool/MCP task to the selected body/lead executor through this
CLI, reads the bounded handoff cards, verifies against raw diff + gate stdout, and only closes on a
fresh GREEN verdict.

Subcommands:
  dispatch "task" [...]        run one selected body/lead executor (or several with --parallel)
  --parallel / -p t...         fan out N concurrent bodies (cap FRONTIER_MAX_PARALLEL, default 4)
  --fanout tasks.json          fan out tasks from a JSON list (strings or {"task": ...})
                               Total non-empty tasks (positional + fanout) are hard-capped
                               (DEFAULT_MAX_TASKS_PER_DISPATCH / FRONTIER_MAX_TASKS, hard
                               ceiling MAX_TASKS_HARD_CEILING). Overflow refuses before any
                               state mutation or provider call. --budget-usd is informational only.
  arm --gate "pytest -q"       arm and freeze a host-approved acceptance gate
  disarm | done                explicitly override, or close on snapshot-bound GREEN
  verify                       run the frozen gate while armed -> verdict.json
  config [--profile advisor|orchestrator --frontier-provider PROVIDER --frontier-model MODEL
          --executor PROVIDER --executor-model MODEL --effort --fast on|off --global]
                                print/persist toggles
  models [--provider PROVIDER]  verified catalog; local CLI discovery where supported (codex/grok)
  doctor [--check-updates]     offline readiness table; opt in to a cached release check
  update --check              cached, privacy-preserving release check
  install-hooks | uninstall-hooks   reversible merge of the hooks into ~/.claude/settings.json

stdlib-only, Python 3.10+, importable.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shlex
import shutil
import stat
import sys
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import frontier_common as fc

SESSION_ID = (os.environ.get("FRONTIER_SESSION_ID")
              or os.environ.get("CLAUDE_CODE_SESSION_ID")
              or "default")
MAX_PARALLEL = int(os.environ.get("FRONTIER_MAX_PARALLEL", "4"))
BODY_TIMEOUT = int(os.environ.get("FRONTIER_BODY_TIMEOUT", "900"))
# Per-invocation task count boundary (before managed mode). Concurrency stays MAX_PARALLEL;
# this caps how many provider tasks a single dispatch may schedule. Not a dollar budget.
MAX_TASKS_HARD_CEILING = 64
DEFAULT_MAX_TASKS_PER_DISPATCH = 32
# Conservative bound for --fanout file contents (fail-closed before JSON parse).
MAX_FANOUT_FILE_BYTES = 1 * 1024 * 1024
SUBCOMMANDS = {"dispatch", "arm", "disarm", "done", "verify", "config", "models", "doctor", "update",
               "install-hooks", "uninstall-hooks", "topology", "role", "consult"}

_EXECUTOR_MODEL_KEYS = {
    "codex": "codex_model",
    "claude": "claude_model",
    "grok": "grok_model",
    "gemini": "gemini_model",
    "openrouter": "openrouter_model",
}
# Provider-specific model CLI flags that must not be combined with --executor-model/--model
# for the selected executor (Codex has no separate provider model flag).
_PROVIDER_MODEL_FLAG_ATTRS = {
    "claude": ("claude_model", "--claude-model"),
    "grok": ("grok_model", "--grok-model"),
    "gemini": ("gemini_model", "--gemini-model"),
}
_PROVIDER_CLI_NAMES = {
    "codex": "codex",
    "claude": "claude",
    "grok": "grok",
    "gemini": "gemini",
    # OpenRouter is HTTP (no PATH CLI). Doctor treats key presence separately.
    "openrouter": "openrouter",
}
# Doctor JSON statement: PATH presence is not auth or entitlement.
AVAILABILITY_NOTE = (
    "CLI availability is not authentication or model entitlement."
)


def _default_executor_model_for(provider: str) -> str:
    """Static executor-model defaults (Codex empty pin = account-aware default)."""
    d = fc.defaults()
    if provider == "codex":
        return str(d.get("codex_model") or "")
    if provider == "claude":
        return str(d.get("claude_model") or "claude-sonnet-5")
    if provider == "grok":
        return str(d.get("grok_model") or "grok-4.5")
    if provider == "gemini":
        return str(d.get("gemini_model") or "gemini-3.5-flash")
    return ""


def _default_frontier_model_for(provider: str) -> str:
    """Static frontier-model defaults matching build_frontier_command / effective_frontier_model."""
    if provider == "claude":
        return "claude-fable-5"
    if provider == "codex":
        return ""  # account default semantics
    if provider == "grok":
        return "grok-4.5"
    if provider == "gemini":
        return "gemini-3.5-flash"
    if provider == "openrouter":
        return "openrouter/auto"
    return ""


def _configured_executor_model(cfg: dict, executor: str) -> str:
    key = _EXECUTOR_MODEL_KEYS.get(executor)
    if key is None:
        return ""
    value = cfg.get(key)
    if value is None:
        return _default_executor_model_for(executor)
    return str(value)


def suggest_provider_availability(
    cfg: dict,
    *,
    lookup=None,
) -> dict | None:
    """Non-mutating PATH-only availability suggestion for doctor (stdlib / shutil.which only).

    Never probes auth, network, or models, and never writes configuration.

    Returns ``None`` when the configured executor and frontier provider CLIs are both present
    (configured selection is preserved as-is). When one or both are missing, returns a
    deterministic suggestion drawn only from present supported provider executables, using static
    defaults / Codex account-default semantics for any role that must change.
    """
    which = lookup if lookup is not None else shutil.which
    present = {
        provider: bool(which(cli_name))
        for provider, cli_name in _PROVIDER_CLI_NAMES.items()
        if provider != "openrouter"
    }
    # OpenRouter is HTTP; "present" means a key is configured (not entitlement proof).
    import os as _os
    present["openrouter"] = bool((_os.environ.get("OPENROUTER_API_KEY") or "").strip())
    present_list = sorted(provider for provider, ok in present.items() if ok)

    executor = str(cfg.get("executor") or "codex").lower()
    frontier = str(cfg.get("frontier_provider") or "claude").lower()
    if executor not in _PROVIDER_CLI_NAMES:
        executor = "codex"
    if frontier not in _PROVIDER_CLI_NAMES:
        frontier = "claude"

    executor_ok = bool(present.get(executor))
    frontier_ok = bool(present.get(frontier))
    if executor_ok and frontier_ok:
        return None
    if not present_list:
        return None

    defaults = fc.defaults()
    default_executor = str(defaults.get("executor") or "codex")
    default_frontier = str(defaults.get("frontier_provider") or "claude")

    def _pick(preferred: str, preferred_ok: bool, role_default: str) -> str:
        if preferred_ok:
            return preferred
        if present.get(role_default):
            return role_default
        return present_list[0]

    suggested_executor = _pick(executor, executor_ok, default_executor)
    suggested_frontier = _pick(frontier, frontier_ok, default_frontier)

    if suggested_executor == executor and executor_ok:
        executor_model = _configured_executor_model(cfg, suggested_executor)
    else:
        executor_model = _default_executor_model_for(suggested_executor)

    if suggested_frontier == frontier and frontier_ok:
        frontier_model = str(cfg.get("frontier_model") or "")
        if not frontier_model and suggested_frontier != "codex":
            frontier_model = _default_frontier_model_for(suggested_frontier)
    else:
        frontier_model = _default_frontier_model_for(suggested_frontier)

    missing = sorted(
        {
            *( [executor] if not executor_ok else [] ),
            *( [frontier] if not frontier_ok else [] ),
        }
    )
    return {
        "profile": str(cfg.get("profile") or defaults.get("profile") or "advisor"),
        "executor": suggested_executor,
        "executor_model": executor_model,
        "frontier_provider": suggested_frontier,
        "frontier_model": frontier_model,
        "present_provider_clis": present_list,
        "missing_configured_clis": missing,
        "preserves_executor": bool(executor_ok),
        "preserves_frontier": bool(frontier_ok),
    }


# --------------------------------------------------------------------------- #
# dispatch — run selected bodies
# --------------------------------------------------------------------------- #
def _generic_executor_model(args_obj) -> str | None:
    """Resolve --executor-model / legacy --model; refuse when both are set."""
    legacy_model = getattr(args_obj, "model", None)
    explicit_model = getattr(args_obj, "executor_model", None)
    if legacy_model is not None and explicit_model is not None:
        raise ValueError("use either --model (legacy) or --executor-model, not both")
    return explicit_model if explicit_model is not None else legacy_model


def _provider_model_flag_conflict(args_obj, executor: str) -> str | None:
    """Refuse generic + selected-executor provider-specific model flags (no silent winner)."""
    generic_set = (
        getattr(args_obj, "model", None) is not None
        or getattr(args_obj, "executor_model", None) is not None
    )
    if not generic_set:
        return None
    spec = _PROVIDER_MODEL_FLAG_ATTRS.get(executor)
    if spec is None:
        return None
    attr, flag = spec
    if getattr(args_obj, attr, None) is not None:
        return (
            f"use either --executor-model/--model or {flag} for the {executor} executor, "
            "not both"
        )
    return None


def _overrides(args) -> dict:
    ov: dict = {}
    if args.fast:
        ov["fast"] = (args.fast == "on")
    if getattr(args, "executor", None):
        ov["executor"] = args.executor
    executor = getattr(args, "executor", None)
    fast = (args.fast == "on") if args.fast else None
    if executor is None or fast is None:
        shaped_executor, shaped_fast = fc.resolve_config_shape(
            overrides=ov, session_id=SESSION_ID
        )
        executor = executor or shaped_executor
        if fast is None:
            fast = shaped_fast
    if executor not in fc.KNOWN_EXECUTORS:
        raise ValueError(
            f"unknown executor {executor!r}; expected one of {sorted(fc.KNOWN_EXECUTORS)}"
        )
    conflict = _provider_model_flag_conflict(args, executor)
    if conflict:
        raise ValueError(conflict)
    selected_model = _generic_executor_model(args)
    if selected_model is not None:
        model_key = "fast_model" if fast and executor == "codex" else _EXECUTOR_MODEL_KEYS[executor]
        ov[model_key] = selected_model
    if args.effort:
        if executor not in {"codex", "grok"}:
            raise ValueError(f"--effort is not supported by the {executor} executor")
        if fast:
            if executor == "grok" and args.effort not in fc.GROK_EFFORT_LEVELS:
                raise ValueError("Grok reasoning effort must be low, medium, or high")
            ov["fast_effort"] = args.effort
        elif executor == "codex":
            ov["codex_effort"] = args.effort
        elif executor == "grok":
            if args.effort not in fc.GROK_EFFORT_LEVELS:
                raise ValueError("Grok reasoning effort must be low, medium, or high")
            ov["grok_effort"] = args.effort
    if getattr(args, "profile", None):
        ov["profile"] = args.profile
    if getattr(args, "frontier_provider", None):
        ov["frontier_provider"] = args.frontier_provider
    if getattr(args, "frontier_model", None) is not None:
        ov["frontier_model"] = args.frontier_model
    if getattr(args, "claude_model", None) is not None:
        ov["claude_model"] = args.claude_model
    if getattr(args, "grok_model", None) is not None:
        ov["grok_model"] = args.grok_model
    if getattr(args, "gemini_model", None) is not None:
        ov["gemini_model"] = args.gemini_model
    return ov


def max_tasks_per_dispatch() -> int:
    """Effective hard task-count limit for one dispatch invocation.

    Default DEFAULT_MAX_TASKS_PER_DISPATCH; FRONTIER_MAX_TASKS may raise or lower it
    within 1..MAX_TASKS_HARD_CEILING. Invalid values raise ValueError (caller refuses).
    This is not related to --budget-usd (informational only).
    """
    raw = (os.environ.get("FRONTIER_MAX_TASKS") or "").strip()
    if not raw:
        return DEFAULT_MAX_TASKS_PER_DISPATCH
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"FRONTIER_MAX_TASKS must be an integer between 1 and {MAX_TASKS_HARD_CEILING}"
        ) from exc
    if value < 1 or value > MAX_TASKS_HARD_CEILING:
        raise ValueError(
            f"FRONTIER_MAX_TASKS={value} out of range; must be 1..{MAX_TASKS_HARD_CEILING}"
        )
    return value


def _read_fanout_file_text(path: Path | str) -> str:
    """Fail-closed bounded read of a fanout JSON path (POSIX Linux/macOS).

    Opens with a nonblocking descriptor, fstats the opened fd, accepts only regular
    files, enforces MAX_FANOUT_FILE_BYTES before and during read (at most limit+1
    bytes so concurrent growth cannot bypass), and decodes UTF-8 strictly. Symlink
    targets that are regular files are allowed; FIFO/symlink-to-special refuse
    without blocking. All failures become ValueError for cmd_dispatch → exit 2.
    """
    p = Path(path)
    flags = os.O_RDONLY
    # O_NONBLOCK: open of FIFO must not block waiting for a writer (Linux/macOS).
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    # Intentionally omit O_NOFOLLOW so symlink→regular remains supported.
    try:
        fd = os.open(p, flags)
    except OSError as exc:
        raise ValueError(f"cannot read fanout file {p}: {exc}") from exc
    try:
        try:
            opened = os.fstat(fd)
        except OSError as exc:
            raise ValueError(f"cannot read fanout file {p}: {exc}") from exc
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"fanout file {p} is not a regular file")
        limit = MAX_FANOUT_FILE_BYTES
        if opened.st_size > limit:
            raise ValueError(f"fanout file {p} exceeds {limit} bytes")
        chunks: list[bytes] = []
        remaining = limit + 1  # read at most limit+1 so growth cannot bypass
        while remaining > 0:
            try:
                chunk = os.read(fd, min(64 * 1024, remaining))
            except BlockingIOError as exc:
                raise ValueError(f"cannot read fanout file {p}: {exc}") from exc
            except OSError as exc:
                raise ValueError(f"cannot read fanout file {p}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError(f"fanout file {p} exceeds {limit} bytes")
        try:
            return raw.decode("utf-8")  # strict
        except UnicodeDecodeError as exc:
            raise ValueError(f"fanout file {p} is not valid UTF-8: {exc}") from exc
    finally:
        os.close(fd)


def _collect_dispatch_tasks(args) -> list[str]:
    """Collect non-empty tasks from positional args and optional fanout file.

    Raises ValueError on malformed fanout (not a JSON list, bad item types, unreadable/
    invalid/oversized/non-regular/non-UTF-8 input, or object items missing a non-null ``task``
    field). Fail-closed: a single bad object refuses the whole fanout before config resolution,
    state mutation, run dirs, or providers. Fanout bytes are read via a nonblocking regular-file
    bound (MAX_FANOUT_FILE_BYTES). Empty/whitespace task strings remain intentionally filtered
    after collection (not errors). Does not mutate session state or call providers.
    """
    tasks: list[str] = list(args.tasks or [])
    fanout = getattr(args, "fanout", "") or ""
    if fanout:
        path = Path(fanout)
        text = _read_fanout_file_text(path)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"fanout file is not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("fanout must be a JSON list of strings or {\"task\": ...} objects")
        for index, item in enumerate(raw):
            if isinstance(item, str):
                tasks.append(item)
            elif isinstance(item, dict):
                # Fail-closed: every object must carry a present, non-null task field.
                # Missing or null must not be coerced to "" and filtered (that would allow
                # partial dispatch of remaining valid entries in a mixed fanout).
                if "task" not in item:
                    raise ValueError(
                        f"fanout item {index}: object must include a non-null 'task' field"
                    )
                task_val = item["task"]
                if task_val is None:
                    raise ValueError(
                        f"fanout item {index}: object must include a non-null 'task' field"
                    )
                if not isinstance(task_val, (str, int, float, bool)):
                    raise ValueError(
                        f"fanout item {index}: 'task' must be a string-like scalar, "
                        f"got {type(task_val).__name__}"
                    )
                tasks.append(str(task_val))
            else:
                raise ValueError(
                    f"fanout item {index} must be a string or object with 'task', "
                    f"got {type(item).__name__}"
                )
    return [t for t in tasks if isinstance(t, str) and t.strip()]


def _run_one(cmd: list[str], task: str, run_id: str, label: str, timeout: int, dry: bool) -> dict:
    if dry:
        display_cmd = cmd
        suffix = f"  <<< {task[:80]}"
        if any("{prompt_file}" in part for part in cmd):
            display_cmd = [part.replace("{prompt_file}", "<prompt-file>") for part in cmd]
        elif any("{prompt}" in part for part in cmd):
            display_cmd, _stdin = fc._apply_prompt(cmd, task[:80])
            suffix = ""
        return {"label": label, "ok": True, "note": "dry-run",
                "schema_version": fc.HANDOFF_SCHEMA_VERSION,
                "task": task[:200], "summary": "[dry-run] " + " ".join(display_cmd) + suffix,
                "artifact": "", "raw_sha256": "", "raw_bytes": 0}
    rc, out, err = fc.run_engine(cmd, task, timeout=timeout)
    text = out or err
    artifact = fc.write_artifact(fc.RUNS_DIR, run_id, label, task, text)
    card = fc.handoff_card(label, task, text, artifact, ok=(rc == 0),
                           note="" if rc == 0 else f"exit {rc}")
    try:
        fc.write_handoff_card(fc.RUNS_DIR, run_id, card)
    except OSError:
        pass  # disk card is best-effort; stdout JSON remains the primary handoff
    return card


def cmd_dispatch(args) -> int:
    # Collect + hard-cap before any config resolve, run dir, dispatch marker, or provider call.
    try:
        tasks = _collect_dispatch_tasks(args)
        limit = max_tasks_per_dispatch()
    except ValueError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 2
    if not tasks:
        print("no tasks given", file=sys.stderr)
        return 2
    if len(tasks) > limit:
        print(
            f"dispatch refused: task count {len(tasks)} exceeds hard limit of {limit} "
            f"per invocation (ceiling {MAX_TASKS_HARD_CEILING}; set FRONTIER_MAX_TASKS "
            f"within 1..{MAX_TASKS_HARD_CEILING} to adjust). "
            "This is a task-count boundary, not a dollar budget.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = fc.resolve_config(overrides=_overrides(args), session_id=SESSION_ID)
        cmd = fc.build_body_command(cfg)
    except ValueError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 2
    run_id = fc.new_run_id()
    if not args.dry_run:
        fc.mkdir_owner_only(fc.RUNS_DIR / f"frontier-{run_id}")
    if args.budget_usd:
        print(f"# soft budget: ${args.budget_usd:.2f} (informational; provider billing is external)",
              file=sys.stderr)

    parallel = args.parallel or len(tasks) > 1
    cards: list[dict] = []
    if not args.dry_run:
        try:
            fc.mark_dispatch_started(SESSION_ID, run_id, time.time())
        except ValueError as exc:
            print(f"dispatch refused: {exc}", file=sys.stderr)
            return 2
    try:
        if parallel and not args.dry_run:
            workers = max(1, min(MAX_PARALLEL, len(tasks)))
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_run_one, cmd, t, run_id, f"body-{i}", args.timeout, args.dry_run)
                        for i, t in enumerate(tasks)]
                for fut in cf.as_completed(futs):
                    cards.append(fut.result())
        else:
            for i, t in enumerate(tasks):
                cards.append(_run_one(cmd, t, run_id, f"body-{i}", args.timeout, args.dry_run))
    finally:
        if not args.dry_run:
            fc.mark_dispatch_finished(SESSION_ID, run_id, time.time())
    cards.sort(key=lambda c: c.get("label", ""))  # parallel dispatch completes out of order

    print(json.dumps({"run_id": run_id, "mode": cfg, "count": len(cards), "cards": cards}, indent=2))
    return 0 if all(c.get("ok") for c in cards) else 1


# --------------------------------------------------------------------------- #
# state toggles
# --------------------------------------------------------------------------- #
def cmd_arm(args) -> int:
    """Arm the workflow guardrail and freeze the host-approved verification command."""
    approved_gate = None
    if args.gate:
        import frontier_verify

        try:
            gate_argv = frontier_verify.parse_gate_argv(args.gate)
        except (TypeError, ValueError) as exc:
            print(f"arm refused: invalid verification command: {exc}", file=sys.stderr)
            return 2
        workspace = str(Path(args.cwd or ".").resolve())
        if not frontier_verify.is_git_worktree(workspace):
            print(
                "arm refused: a closable frozen verifier requires --cwd (or the current directory) "
                "to be inside a Git worktree",
                file=sys.stderr,
            )
            return 2
        approved_gate = {
            "gate": args.gate,
            "argv": gate_argv,
            "cwd": workspace,
        }
    fc.arm_session(SESSION_ID, approved_gate)
    if approved_gate:
        print(
            f"armed (session {SESSION_ID}) - workflow guardrail active with a frozen verification "
            "command. Delegate execution to the selected body, run `frontier-dispatch verify`, and "
            "finish only on a snapshot-bound GREEN. Kill-switch: FRONTIER_GUARDS_OFF=1."
        )
    else:
        print(
            f"armed (session {SESSION_ID}) - workflow guardrail active, but no verification command "
            "was approved. Disarm and re-arm with `--gate \"<tests/build/lint>\"` before delegating "
            "if the model must be able to verify and finish. Kill-switch: FRONTIER_GUARDS_OFF=1."
        )
    return 0


def cmd_disarm(_args) -> int:
    fc.disarm_session(SESSION_ID)
    print(f"disarmed (session {SESSION_ID}) - workflow guardrail off.")
    return 0


def cmd_done(_args) -> int:
    """Disarm ONLY on a fresh GREEN verdict — the loop's core invariant. `done` without one
    leaves the gate armed (use `disarm` explicitly to override, which is a distinct, deliberate
    escape hatch — not something `done` does silently)."""
    import frontier_verify

    with fc.advisory_lock(fc.config_lock_path(fc.GLOBAL_CONFIG)):
        st = fc.read_state(SESSION_ID)
        if st.get("active_dispatches"):
            print("done - REFUSED: an executor dispatch is still running. Guardrail stays armed.", file=sys.stderr)
            return 1
        if st.get("active_verifications"):
            print("done - REFUSED: verification is still running. Guardrail stays armed.", file=sys.stderr)
            return 1
        approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else {}
        fresh = frontier_verify.verdict_is_snapshot_fresh_green(
            st.get("verdict"),
            st.get("last_dispatch_ts", 0),
            st.get("dispatch_generation", 0),
            session_id=SESSION_ID,
            cwd=approved.get("cwd"),
            approved_gate=approved,
        )
        if not fresh:
            print("done - REFUSED: no fresh snapshot-bound GREEN verdict recorded for the last "
                  "dispatch. Guardrail stays armed. Run `frontier-dispatch verify` until GREEN, or "
                  "`frontier-dispatch disarm` from the host to override deliberately.", file=sys.stderr)
            return 1
        updated, _current = fc.consume_completion(
            SESSION_ID,
            st.get("state_revision"),
        )
    if not updated:
        print(
            "done - REFUSED: session state changed during final validation. Guardrail stays armed; "
            "wait for dispatches to finish and verify again.",
            file=sys.stderr,
        )
        return 1
    print("done - fresh snapshot-bound GREEN still matches the workspace. Guardrail disarmed.")
    return 0


# --------------------------------------------------------------------------- #
# verify (delegates to frontier_verify)
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    import frontier_verify

    st = fc.read_state(SESSION_ID)
    approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else None
    gate = args.gate
    cwd = args.cwd or "."
    if st.get("armed"):
        if not approved or not isinstance(approved.get("argv"), list):
            print(
                "verify refused: the armed session has no frozen verification command; disarm and "
                "re-arm with `frontier-dispatch arm --gate \"<command>\"`.",
                file=sys.stderr,
            )
            return 2
        if gate or args.cwd:
            print(
                "verify refused: armed verification uses the gate and workspace frozen at arm "
                "time; omit --gate and --cwd",
                file=sys.stderr,
            )
            return 2
        gate = str(approved.get("gate") or "")
        cwd = str(approved.get("cwd") or ".")
    if not gate:
        print("verify requires --gate \"<command>\" when the session is not armed", file=sys.stderr)
        return 2

    v = frontier_verify.run_gate(gate, session_id=SESSION_ID, cwd=cwd)
    print(json.dumps({k: v[k] for k in ("result", "gate", "exit_code", "diff_sha", "paths", "ts")},
                     indent=2))
    return 0 if v["result"] == "GREEN" else 1


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def cmd_config(args) -> int:
    if args.repair:
        target = fc.GLOBAL_CONFIG if args.glob else fc.state_path(SESSION_ID)
        kind = "global" if args.glob else "state"
        result = fc.repair_config_file(
            target,
            kind=kind,
            legacy_path=None if args.glob else fc.legacy_state_path(SESSION_ID),
            session_id=None if args.glob else SESSION_ID,
        )
        if result["status"] == "repaired":
            print(
                f"repaired {kind} configuration; owner-only backup: {result['backup']}",
                file=sys.stderr,
            )
            if kind == "state":
                print(
                    "session repair clears the workflow guardrail and prior verdict; re-arm with "
                    "`frontier-dispatch arm --gate \"<command>\"` and verify again before closing.",
                    file=sys.stderr,
                )
        else:
            print(f"repair not needed: {target}", file=sys.stderr)
    patch: dict = {}
    inherit_fast_model = getattr(args, "inherit_fast_model", False)
    if inherit_fast_model and (args.model is not None or args.executor_model is not None):
        print(
            "config refused: --inherit-fast-model cannot be combined with --model/--executor-model",
            file=sys.stderr,
        )
        return 2
    if inherit_fast_model:
        patch["fast_model"] = None
    if args.fast is not None:
        patch["fast"] = (args.fast == "on")
    if args.executor is not None:
        if args.executor not in fc.KNOWN_EXECUTORS:
            print(
                f"config refused: unknown executor {args.executor!r}; "
                f"expected one of {sorted(fc.KNOWN_EXECUTORS)}",
                file=sys.stderr,
            )
            return 2
        patch["executor"] = args.executor
    if args.profile is not None:
        patch["profile"] = args.profile
    if args.frontier_provider is not None:
        patch["frontier_provider"] = args.frontier_provider
    if args.frontier_model is not None:
        patch["frontier_model"] = args.frontier_model
    if args.claude_model is not None:
        patch["claude_model"] = args.claude_model
    if args.grok_model is not None:
        patch["grok_model"] = args.grok_model
    if args.gemini_model is not None:
        patch["gemini_model"] = args.gemini_model
    if getattr(args, "openrouter_model", None) is not None:
        patch["openrouter_model"] = args.openrouter_model
    if args.update_mode is not None:
        patch["update_mode"] = args.update_mode
    executor = args.executor
    fast = (args.fast == "on") if args.fast is not None else None
    if executor is None or fast is None:
        try:
            shaped_executor, shaped_fast = fc.resolve_config_shape(
                overrides=patch, session_id=SESSION_ID
            )
        except ValueError as exc:
            print(f"config refused: {exc}", file=sys.stderr)
            return 2
        executor = executor or shaped_executor
        if fast is None:
            fast = shaped_fast
    if executor not in fc.KNOWN_EXECUTORS:
        print(
            f"config refused: unknown executor {executor!r}; "
            f"expected one of {sorted(fc.KNOWN_EXECUTORS)}",
            file=sys.stderr,
        )
        return 2
    try:
        selected_model = _generic_executor_model(args)
    except ValueError as exc:
        print(f"config refused: {exc}", file=sys.stderr)
        return 2
    conflict = _provider_model_flag_conflict(args, executor)
    if conflict:
        print(f"config refused: {conflict}", file=sys.stderr)
        return 2
    if selected_model is not None:
        model_key = (
            "fast_model" if fast and executor == "codex"
            else _EXECUTOR_MODEL_KEYS[executor]
        )
        patch[model_key] = selected_model
    if args.effort is not None:
        if executor not in {"codex", "grok"}:
            print(
                f"config refused: --effort is not supported by the {executor} executor",
                file=sys.stderr,
            )
            return 2
        if fast:
            if executor == "grok" and args.effort not in fc.GROK_EFFORT_LEVELS:
                print(
                    f"config refused: Grok effort must be one of {sorted(fc.GROK_EFFORT_LEVELS)}",
                    file=sys.stderr,
                )
                return 2
            patch["fast_effort"] = args.effort
        elif executor == "codex":
            patch["codex_effort"] = args.effort
        elif executor == "grok":
            if args.effort not in fc.GROK_EFFORT_LEVELS:
                print(
                    f"config refused: Grok effort must be one of {sorted(fc.GROK_EFFORT_LEVELS)}",
                    file=sys.stderr,
                )
                return 2
            patch["grok_effort"] = args.effort
    if patch:
        try:
            fc.update_config_transaction(SESSION_ID, patch, global_scope=args.glob)
        except ValueError as exc:
            print(f"config refused: {exc}", file=sys.stderr)
            return 2
    cfg = fc.resolve_config(session_id=SESSION_ID)
    print(json.dumps(cfg, indent=2, sort_keys=True))
    scope = "global" if args.glob else "session" if patch else "effective"
    print(f"# scope: {scope}", file=sys.stderr)
    try:
        body_cmd = " ".join(fc.build_body_command(cfg))
    except ValueError as exc:
        body_cmd = f"<unavailable: {exc}>"
    print("body cmd :", body_cmd, file=sys.stderr)
    print("frontier consult cmd:", " ".join(fc.build_frontier_command(cfg)), file=sys.stderr)
    return 0


def cmd_models(args) -> int:
    import frontier_models

    providers = [args.provider] if args.provider else sorted(frontier_models.PROVIDERS)
    payload = {
        provider: frontier_models.provider_models_payload(
            provider, discover=not args.no_discover
        )
        for provider in providers
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for provider in providers:
        disc = payload[provider]["discovery"]
        if not disc["supported"]:
            disc_note = "local discovery not supported"
        elif not disc["attempted"]:
            disc_note = "local discovery skipped"
        elif disc["succeeded"]:
            disc_note = f"local discovery: {len(disc['discovered_ids'])} id(s)"
        else:
            err = disc.get("error_class") or "failed"
            disc_note = f"local discovery failed ({err})"
        print(f"{provider} ({payload[provider]['source']}; {disc_note})")
        for row in payload[provider]["models"]:
            model = row["id"] or "<account default>"
            print(f"  {model:28} {row['status']:11} {row['description']}")
        print("  <custom model ID>            account-specific; validate with the provider CLI")
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _update_result(args, *, passive: bool = False) -> dict:
    import frontier_update

    cfg = fc.resolve_config(session_id=SESSION_ID)
    mode = cfg["update_mode"]
    if passive and mode != "passive":
        return frontier_update.check_for_updates(allow_network=False, mode="off")
    return frontier_update.check_for_updates(
        allow_network=bool(getattr(args, "check_updates", False) or getattr(args, "check", False)),
        force=bool(getattr(args, "force", False)),
        mode=mode,
    )


def _update_message(result: dict) -> str:
    status = result["status"]
    if status == "update_available":
        return (
            f"UPDATE AVAILABLE {result['current_version']} -> {result['latest_version']}; "
            "Claude: `/plugin marketplace update frontierfuse` then "
            "`/plugin update frontierfuse@frontierfuse`; checkout installs: `git pull --ff-only`"
        )
    if status == "current":
        return f"CURRENT {result['current_version']}"
    if status == "ahead":
        return f"AHEAD {result['current_version']} (published: {result['latest_version']})"
    if status == "disabled":
        return "DISABLED (set update mode to passive or manual to check)"
    return "UNKNOWN (run `frontier-dispatch update --check` when online)"


def cmd_update(args) -> int:
    result = _update_result(args, passive=args.passive)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    elif args.passive and result["status"] != "update_available":
        pass
    else:
        print(_update_message(result))
    return 0


def cmd_doctor(args) -> int:
    if not fc.session_id_is_valid(SESSION_ID):
        check = {
            "component": "session identifier",
            "status": "invalid_session",
            "ok": False,
            "blocking": True,
            "detail": "FRONTIER_SESSION_ID is invalid",
            "next_step": "Set FRONTIER_SESSION_ID to a nonempty valid string, then rerun doctor.",
        }
        payload = {
            "status": "config_invalid",
            "ready": False,
            "checks": [check],
            "availability_suggestion": None,
            "availability_note": AVAILABILITY_NOTE,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("FrontierFuse doctor\n")
            print("  NO  session identifier     INVALID_SESSION")
            print(f"      next: {check['next_step']}")
        return 2
    if not _state_dir_ok(fc.STATE_DIR, SESSION_ID):
        check = {
            "component": "state dir writable",
            "status": "state_unwritable",
            "ok": False,
            "blocking": True,
            "detail": str(fc.STATE_DIR),
            "next_step": "Replace the path with a writable directory or fix its ownership and permissions.",
        }
        payload = {
            "status": "not_ready",
            "ready": False,
            "checks": [check],
            "availability_suggestion": None,
            "availability_note": AVAILABILITY_NOTE,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("FrontierFuse doctor\n")
            print(f"  --  {'state dir writable':22} STATE_UNWRITABLE: {fc.STATE_DIR}")
            print(f"      next: {check['next_step']}")
            print("\nNOT READY — FrontierFuse needs a writable state directory.")
        return 1

    try:
        cfg = fc.resolve_config(session_id=SESSION_ID)
    except ValueError as exc:
        persisted = isinstance(exc, (fc.ConfigFileError, fc.StateFileError))
        path = exc.path if persisted else "effective environment/global/session configuration"
        reason = exc.reason if persisted else str(exc)
        if isinstance(exc, fc.ConfigFileError):
            component = "global configuration"
            if "special file" in reason.lower():
                next_step = (
                    "Replace or remove the special global configuration path, then rerun doctor."
                )
            elif "unreadable" in reason.lower():
                next_step = (
                    "Fix ownership and read permissions on the global configuration file, then "
                    "rerun doctor before attempting repair."
                )
            else:
                next_step = (
                    "Run `frontier-dispatch config --repair --global`, then reapply your selections."
                )
        elif isinstance(exc, fc.StateFileError):
            component = "session configuration"
            if "special file" in reason.lower():
                next_step = (
                    "Replace or remove the special session state path, then rerun doctor."
                )
            elif "unreadable" in reason.lower():
                next_step = (
                    "Fix ownership and read permissions on the session state file, then rerun "
                    "doctor before attempting repair."
                )
            else:
                next_step = (
                    "Run `frontier-dispatch config --repair`, then reapply this session's selections."
                )
        else:
            component = "effective configuration"
            if "Grok fast mode" in reason:
                next_step = (
                    "Run `frontier-dispatch config --effort high` to heal the Grok fast preset, or "
                    "run `frontier-dispatch config --fast off`; then reapply your intended settings."
                )
            else:
                next_step = (
                    "Correct or unset invalid FRONTIER_* environment values, then apply a complete "
                    "valid selection with `frontier-dispatch config`. Repair is only for a specific "
                    "persisted file that doctor identifies as malformed."
                )
        check = {
            "component": component,
            "status": "config_invalid",
            "ok": False,
            "blocking": True,
            "detail": f"{path}: {reason}",
            "next_step": next_step,
        }
        payload = {
            "status": "config_invalid",
            "ready": False,
            "checks": [check],
            "availability_suggestion": None,
            "availability_note": AVAILABILITY_NOTE,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("FrontierFuse doctor\n")
            print(f"  --  {component:22} CONFIG_INVALID: {check['detail']}")
            print(f"      next: {check['next_step']}")
            print("\nNOT READY — repair the preserved configuration before continuing.")
        return 2

    body_err = None
    try:
        body_cmd = fc.build_body_command(cfg)
    except ValueError as exc:
        body_cmd = []
        body_err = str(exc)
    frontier_err = None
    try:
        frontier_cmd = fc.build_frontier_command(cfg)
    except ValueError as exc:
        frontier_cmd = []
        frontier_err = str(exc)
    settings = _settings_path()
    settings_error = ""
    manual_hooks_installed = False
    settings_result = fc.inspect_json_file(settings)
    if settings_result["status"] == "ready":
        try:
            hooks = _validated_hook_events(settings_result["data"])
            gate_path, verify_path = _our_script_paths()
            # Doctor marks ready only for current exec-form handlers on covering matchers
            # (legacy shell-form is not_installed until install-hooks upgrades).
            manual_hooks_installed = (
                _event_has_ready_our_hook(hooks, "PreToolUse", gate_path)
                and _event_has_ready_our_hook(hooks, "Stop", verify_path)
            )
        except ValueError as exc:
            settings_error = f"Claude settings hook structure is invalid ({exc})"
    elif settings_result["status"] != "missing":
        settings_error = f"Claude settings are {settings_result['status'].replace('_', ' ')}"
    plugin_manifest_present = (HERE / ".claude-plugin" / "plugin.json").is_file()
    running_as_plugin = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    def mark(ok):
        return "\033[32mok\033[0m" if ok else "\033[33m--\033[0m"

    if running_as_plugin:
        install_row = ("hooks", True, "ready",
                       f"auto-registered by Claude Code plugin (${{CLAUDE_PLUGIN_ROOT}}={os.environ['CLAUDE_PLUGIN_ROOT']})",
                       "")
    elif settings_error:
        install_row = (
            "hooks", False, "probe_failed", f"{settings}: {settings_error}",
            "Repair or replace the Claude settings file, then rerun `frontier-dispatch doctor`.",
        )
    elif manual_hooks_installed:
        install_row = ("hooks", True, "ready", f"manually installed (Option B) — {settings}", "")
    else:
        install_row = (
            "hooks", False, "not_installed", "Claude workflow hooks are not active",
            "In Claude Code run `/plugin marketplace add Renn-Labs/FrontierFuse` then "
            "`/plugin install frontierfuse@frontierfuse`; other harnesses may use the CLI without hooks.",
        )

    body_ok = (not body_err) and bool(body_cmd and shutil.which(body_cmd[0]))
    frontier_ok = (not frontier_err) and bool(frontier_cmd and shutil.which(frontier_cmd[0]))
    body_info = body_err if body_err else _redacted_command_detail(body_cmd, body_ok)
    frontier_info = (
        frontier_err if frontier_err else _redacted_command_detail(frontier_cmd, frontier_ok)
    )
    global_lock = fc.config_lock_path(fc.GLOBAL_CONFIG)
    global_lock_ok = _lock_path_ok(global_lock)
    body_status = "command_invalid" if body_err else "ready" if body_ok else "cli_missing"
    body_next = (
        "Fix the selected executor command override, then rerun `frontier-dispatch doctor`."
        if body_err else
        "Install the selected executor CLI, then rerun `frontier-dispatch doctor`. Authentication "
        "and model entitlement require an explicit provider-side check." if not body_ok else ""
    )
    frontier_status = (
        "command_invalid" if frontier_err else "ready" if frontier_ok else "cli_missing"
    )
    frontier_next = (
        "Fix the selected frontier-provider command override, then rerun `frontier-dispatch doctor`."
        if frontier_err else
        "Install the selected frontier-provider CLI, then rerun `frontier-dispatch doctor`. "
        "Authentication and model entitlement require an explicit provider-side check."
        if not frontier_ok else ""
    )
    rows = [
        (
            "global configuration",
            global_lock_ok,
            "ready" if global_lock_ok else "lock_unusable",
            str(fc.GLOBAL_CONFIG if global_lock_ok else global_lock),
            "Replace the global lock path with a writable regular file or fix its parent directory."
            if not global_lock_ok else "",
        ),
        (f"{cfg['executor']} body CLI", body_ok, body_status, body_info, body_next),
        (f"{cfg['frontier_provider']} frontier CLI", frontier_ok, frontier_status, frontier_info,
         frontier_next),
        ("plugin manifest", plugin_manifest_present, "ready" if plugin_manifest_present else "not_installed",
         str(HERE / ".claude-plugin" / "plugin.json"),
         "Reinstall FrontierFuse from its stable checkout." if not plugin_manifest_present else ""),
        install_row,
        ("state dir writable", True, "ready", str(fc.STATE_DIR), ""),
    ]
    update_result = _update_result(args)
    release_known = update_result["status"] in {"current", "ahead", "disabled", "update_available"}
    rows.append((
        "release status",
        release_known,
        update_result["status"],
        _update_message(update_result),
        "Run `frontier-dispatch update --check` when online." if not release_known else "",
    ))
    ready = global_lock_ok and body_ok and frontier_ok
    blocking_components = {
        "global configuration",
        f"{cfg['executor']} body CLI",
        f"{cfg['frontier_provider']} frontier CLI",
        "state dir writable",
    }
    checks = [
        {
            "component": label,
            "status": status,
            "ok": ok,
            "blocking": label in blocking_components,
            "detail": info,
            "next_step": next_step,
        }
        for label, ok, status, info, next_step in rows
    ]
    availability_suggestion = suggest_provider_availability(cfg)
    if args.json:
        print(json.dumps({
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checks": checks,
            "effective_config": cfg,
            "availability_suggestion": availability_suggestion,
            "availability_note": AVAILABILITY_NOTE,
        }, indent=2, sort_keys=True, allow_nan=False))
        return 0 if ready else 1

    print("FrontierFuse doctor\n")
    for label, ok, status, info, next_step in rows:
        print(f"  {mark(ok)}  {label:22} {status.upper()}: {info}")
        if next_step:
            print(f"      next: {next_step}")
    if availability_suggestion is not None:
        print(
            "\n  PATH availability suggestion (non-mutating; not auth/entitlement): "
            f"executor={availability_suggestion['executor']!r} "
            f"executor_model={availability_suggestion['executor_model']!r} "
            f"frontier_provider={availability_suggestion['frontier_provider']!r} "
            f"frontier_model={availability_suggestion['frontier_model']!r}"
        )
        print(f"  note: {AVAILABILITY_NOTE}")
    print(f"\n{'READY' if ready else 'NOT READY'} — offline CLI readiness only: need the "
          f"{cfg['executor']} body CLI on PATH and the {cfg['frontier_provider']} frontier CLI "
          "for managed advice. CLI availability is not authentication or model entitlement; "
          "offline tests/dry-run work regardless.")
    return 0 if ready else 1


def _state_dir_ok(p: Path, session_id: str) -> bool:
    if p.exists():
        if not p.is_dir() or p.is_symlink() or not os.access(p, os.W_OK | os.X_OK):
            return False
    elif not _nearest_existing_parent_writable(p):
        return False
    return _lock_path_ok(fc.config_lock_path(fc.state_path(session_id)))


def _nearest_existing_parent_writable(path: Path) -> bool:
    current = path
    while True:
        if current.exists():
            break
        if current.is_symlink():
            return False
        if current == current.parent:
            return False
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK | os.X_OK)


def _lock_path_ok(lock_path: Path) -> bool:
    try:
        if lock_path.exists() or lock_path.is_symlink():
            return (
                not lock_path.is_symlink()
                and stat.S_ISREG(lock_path.stat().st_mode)
                and os.access(lock_path, os.R_OK | os.W_OK)
            )
        return _nearest_existing_parent_writable(lock_path)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# install-hooks / uninstall-hooks (reversible merge into ~/.claude/settings.json)
# --------------------------------------------------------------------------- #
def _settings_path() -> Path:
    cfgdir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return cfgdir / "settings.json"


# Command-hook timeout seconds. Must match hooks/hooks.json and settings.hooks.snippet.json.
HOOK_COMMAND_TIMEOUT = 10

# Claude Code all-tools PreToolUse matcher. Empty string is equivalent; prefer "*" so
# registration surfaces (hooks.json, settings snippet, install-hooks) stay visually aligned.
PRETOOLUSE_ALL_TOOLS_MATCHER = "*"

# Official Claude Code command-hook exec form: command is the executable, args is a string list
# (no shell). Shell-form "python3 <path>" strings are legacy and are upgraded/removed on install.
HOOK_COMMAND_EXECUTABLE = "python3"

# Script basenames that identify FrontierFuse command hooks (any registration surface).
HOOK_SCRIPT_PRETOOL = "frontier_gate.py"
HOOK_SCRIPT_STOP = "frontier_verify_gate.py"
_OUR_HOOK_SCRIPTS = frozenset({HOOK_SCRIPT_PRETOOL, HOOK_SCRIPT_STOP})
_EVENT_SCRIPT = {
    "PreToolUse": HOOK_SCRIPT_PRETOOL,
    "Stop": HOOK_SCRIPT_STOP,
}


def _our_script_paths() -> tuple[Path, Path]:
    """Absolute script paths for Option B / install-hooks (current checkout)."""
    return (
        (HERE / "hooks" / HOOK_SCRIPT_PRETOOL).resolve(),
        (HERE / "hooks" / HOOK_SCRIPT_STOP).resolve(),
    )


def _our_commands() -> tuple[str, str]:
    """Deprecated shell-form strings kept only for tests that still probe legacy helpers.

    Prefer ``_exec_command_hook`` / ``_is_our_hook`` / ``_our_script_paths``. New registration
    always uses exec form (command + args).
    """
    gate, verify = _our_script_paths()
    return (f"python3 {shlex.quote(str(gate))}", f"python3 {shlex.quote(str(verify))}")


def _exec_command_hook(script: Path | str) -> dict:
    """Build an official exec-form command hook: python3 + one literal script arg + timeout."""
    return {
        "type": "command",
        "command": HOOK_COMMAND_EXECUTABLE,
        "args": [str(script)],
        "timeout": HOOK_COMMAND_TIMEOUT,
    }


def _command_hook(command: str) -> dict:
    """Back-compat shim: shell-form command string → prefer parsing into exec form when possible."""
    # Install paths no longer use this for new handlers; kept for older test call sites.
    return {"type": "command", "command": command, "timeout": HOOK_COMMAND_TIMEOUT}


def _ensure_command_hook_options(hook: dict) -> None:
    """Align handler options (timeout) with hooks.json without rewriting unrelated keys."""
    if hook.get("type") == "command" and hook.get("timeout") != HOOK_COMMAND_TIMEOUT:
        hook["timeout"] = HOOK_COMMAND_TIMEOUT


def _hook_script_name_from_path(path: str) -> str | None:
    """Return FrontierFuse script basename if ``path`` refers to one of our hook scripts."""
    if not isinstance(path, str) or not path:
        return None
    # Normalize separators; keep the path literal otherwise (spaces/$/`/;/' must survive).
    normalized = path.replace("\\", "/")
    base = Path(normalized).name
    # Strip a trailing quote fragment that can appear in broken shell-form leftovers.
    base = base.strip("'\"")
    if base in _OUR_HOOK_SCRIPTS:
        return base
    for name in _OUR_HOOK_SCRIPTS:
        if normalized.endswith(f"/hooks/{name}") or normalized.endswith(f"hooks/{name}"):
            return name
        # Placeholder forms: $CLAUDE_PLUGIN_ROOT/hooks/..., <REPO>/hooks/...
        if f"/hooks/{name}" in normalized or normalized.endswith(name):
            if name in Path(normalized).name or normalized.rstrip("/").endswith(name):
                return name
    return None


def _shell_form_script_name(command: str) -> str | None:
    """Extract our script basename from a legacy shell-form command string, if present.

    Recognizes baseline unquoted, shlex-quoted, and double-quoted path variants, including
    repo paths that embed spaces, dollar signs, command-substitution text, backticks,
    semicolons, and apostrophes. Also matches plugin-root shell fragments.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    text = command.strip()
    # Must be a python3/python invocation (interpreter may be a path).
    head = text.split(None, 1)[0]
    head_name = Path(head).name
    if head_name not in {"python", "python3"}:
        return None
    for name in _OUR_HOOK_SCRIPTS:
        if name not in text:
            continue
        # Prefer shlex when it recovers a single script token.
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = None
        if tokens and len(tokens) >= 2:
            # python3 <script>  or  python3 -u <script> (we only ship bare form; still match)
            for tok in tokens[1:]:
                found = _hook_script_name_from_path(tok)
                if found == name:
                    return name
        # Fallback for unquoted paths with spaces / metacharacters that break shlex:
        # locate .../hooks/<name> or a trailing <name> after the interpreter.
        if f"/hooks/{name}" in text or text.rstrip().endswith(name):
            return name
        # Plugin-style: python3 "$CLAUDE_PLUGIN_ROOT"/hooks/name
        if f'CLAUDE_PLUGIN_ROOT"/hooks/{name}' in text or f"CLAUDE_PLUGIN_ROOT'/hooks/{name}" in text:
            return name
        if f"CLAUDE_PLUGIN_ROOT/hooks/{name}" in text:
            return name
        if f"<REPO>/hooks/{name}" in text or f"<REPO>/hooks/{name}" in text.replace("\\", ""):
            return name
    return None


def _is_our_hook(hook: dict, script_name: str | None = None) -> bool:
    """True when a command hook is any FrontierFuse variant (legacy shell or current exec form)."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    names = {script_name} if script_name else set(_OUR_HOOK_SCRIPTS)

    command = hook.get("command")
    args = hook.get("args", None)

    # Current / target official exec form: command=python3, args=[exactly one script path].
    if isinstance(args, list):
        if len(args) == 1 and isinstance(args[0], str):
            found = _hook_script_name_from_path(args[0])
            if found in names:
                # Executable must be python3 (name or path ending in python3/python).
                if isinstance(command, str) and Path(command).name in {"python", "python3"}:
                    return True
                # Malformed executable with our script still counts as "ours" for uninstall/upgrade
                # so stale/broken entries are cleaned rather than left dangling.
                if found in names:
                    return True
        # Malformed args (wrong arity / non-strings) that still name our script → treat as ours
        # for cleanup, but doctor will not mark ready.
        if any(isinstance(a, str) and _hook_script_name_from_path(a) in names for a in args):
            return True
        return False

    # Legacy shell form: single command string, no args array.
    if isinstance(command, str) and args is None:
        found = _shell_form_script_name(command)
        return found in names
    return False


def _is_ready_exec_hook(hook: dict, script_path: Path) -> bool:
    """True when hook is the current exec form bound to this absolute script with aligned timeout."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    if hook.get("command") != HOOK_COMMAND_EXECUTABLE:
        return False
    args = hook.get("args")
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        return False
    try:
        if Path(args[0]).resolve() != script_path.resolve():
            return False
    except (OSError, ValueError):
        if args[0] != str(script_path):
            return False
    if hook.get("timeout") != HOOK_COMMAND_TIMEOUT:
        return False
    return True


def _matcher_covers(event: str, matcher: str) -> bool:
    """True when the matcher invokes our hook for every tool in the event class.

    Stop: any matcher is unrestricted. PreToolUse: only empty or ``*`` (all-tools
    semantics) so armed fail-closed policy cannot miss tool classes. A historical
    Write|Edit|… pipe list is intentionally NOT covering.
    """
    if not isinstance(matcher, str):
        return False
    if event == "Stop":
        return True
    return matcher in {"", "*"}


def _redacted_command_detail(command: list[str], present: bool) -> str:
    if not command:
        return "command unavailable"
    detail = f"executable {Path(command[0]).name!r}; {max(0, len(command) - 1)} argument(s) redacted"
    if present:
        detail += "; CLI present; authentication and model entitlement not probed offline"
    return detail


def _validated_hook_events(data: dict) -> dict:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for event in ("PreToolUse", "Stop"):
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
                raise ValueError(f"hooks.{event} contains an invalid entry")
            if not isinstance(entry.get("matcher", ""), str):
                raise ValueError(f"hooks.{event} contains an invalid matcher")
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
                    raise ValueError(f"hooks.{event} contains an invalid hook")
                if hook.get("type") != "command":
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command:
                    raise ValueError(f"hooks.{event} contains an invalid command hook")
                # Official exec form uses args: list[str]. Malformed args fail closed.
                if "args" in hook:
                    args = hook.get("args")
                    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                        raise ValueError(f"hooks.{event} contains an invalid command hook args")
    return hooks


def _strip_our_hooks_from_event(entries: list, script_name: str) -> int:
    """Remove every FrontierFuse handler (legacy or current) for ``script_name``.

    Leaves unrelated handlers and empty-matcher structure intact when other hooks remain.
    Returns the number of removed handlers.
    """
    removed = 0
    keep_entries: list = []
    for entry in list(entries):
        if not isinstance(entry, dict):
            keep_entries.append(entry)
            continue
        inner = entry.get("hooks") or []
        if not isinstance(inner, list):
            keep_entries.append(entry)
            continue
        kept = []
        for h in inner:
            if _is_our_hook(h, script_name):
                removed += 1
                continue
            kept.append(h)
        if kept:
            entry["hooks"] = kept
            keep_entries.append(entry)
        # else: drop empty matcher group entirely
    entries[:] = keep_entries
    return removed


def _event_has_ready_our_hook(hooks: dict, event: str, script_path: Path) -> bool:
    script_name = script_path.name
    for entry in hooks.get(event, []):
        if not isinstance(entry, dict):
            continue
        if not _matcher_covers(event, entry.get("matcher", "")):
            continue
        for hook in entry.get("hooks") or []:
            if _is_ready_exec_hook(hook, script_path):
                return True
            # Also accept exec form whose args path matches by basename + same resolved file
            # when the checkout moved only by symlink.
            if _is_our_hook(hook, script_name) and isinstance(hook, dict):
                args = hook.get("args")
                if (
                    hook.get("command") == HOOK_COMMAND_EXECUTABLE
                    and isinstance(args, list)
                    and len(args) == 1
                    and isinstance(args[0], str)
                    and hook.get("timeout") == HOOK_COMMAND_TIMEOUT
                ):
                    try:
                        if Path(args[0]).resolve() == script_path.resolve():
                            return True
                    except (OSError, ValueError):
                        pass
    return False


def _ensure_our_event_handler(hooks: dict, event: str, script_path: Path) -> None:
    """Upgrade path: strip every prior variant, collapse duplicates, install one exec-form handler."""
    script_name = _EVENT_SCRIPT[event]
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        hooks[event] = []
        entries = hooks[event]

    # Remove all legacy + current own handlers first (including narrow-matcher and duplicates).
    _strip_our_hooks_from_event(entries, script_name)

    # Prefer reusing an existing all-tools (PreToolUse) / any (Stop) entry when present.
    target = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if event == "PreToolUse":
            if entry.get("matcher") == PRETOOLUSE_ALL_TOOLS_MATCHER or entry.get("matcher") == "":
                target = entry
                break
        else:
            # Stop: first covering entry
            if _matcher_covers("Stop", entry.get("matcher", "")):
                target = entry
                break

    desired = _exec_command_hook(script_path)
    if target is None:
        matcher = PRETOOLUSE_ALL_TOOLS_MATCHER if event == "PreToolUse" else "*"
        entries.append({"matcher": matcher, "hooks": [desired]})
        return

    # Align matcher for PreToolUse to the preferred all-tools token.
    if event == "PreToolUse" and target.get("matcher") != PRETOOLUSE_ALL_TOOLS_MATCHER:
        target["matcher"] = PRETOOLUSE_ALL_TOOLS_MATCHER
    inner = target.setdefault("hooks", [])
    if not isinstance(inner, list):
        target["hooks"] = [desired]
        return
    # Collapse any residual own handlers and append a single desired exec-form handler.
    target["hooks"] = [h for h in inner if not _is_our_hook(h, script_name)]
    target["hooks"].append(desired)


def cmd_install_hooks(_args) -> int:
    gate_path, verify_path = _our_script_paths()
    sp = _settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    existing_text = ""
    data = {}
    if sp.exists():
        result = fc.inspect_json_file(sp)
        try:
            existing_text = fc.read_bounded_regular_text(sp)
        except (OSError, UnicodeError, ValueError, OverflowError) as exc:
            print(f"REFUSING to modify unreadable Claude settings {sp}: {exc}", file=sys.stderr)
            return 1
        fc.write_text_owner_only(sp.with_suffix(".json.bak"), existing_text)
        if result["status"] != "ready":
            print(
                f"REFUSING to modify invalid Claude settings {sp}; original preserved at "
                f"{sp.with_suffix('.json.bak')}. Repair or restore it first.",
                file=sys.stderr,
            )
            return 1
        data = result["data"]
    try:
        hooks = _validated_hook_events(data)
    except ValueError as exc:
        print(f"REFUSING to modify malformed Claude hook settings {sp}: {exc}", file=sys.stderr)
        return 1
    data.setdefault("hooks", hooks)

    # Upgrade / install: strip every prior FrontierFuse variant (shell-form unquoted/quoted,
    # double-quoted, exec form), collapse duplicates, move PreToolUse to all-tools matcher,
    # write one official exec-form handler per event with aligned timeout.
    _ensure_our_event_handler(hooks, "PreToolUse", gate_path)
    _ensure_our_event_handler(hooks, "Stop", verify_path)

    fc.write_text_owner_only(sp, json.dumps(data, indent=2) + "\n")
    print(f"installed FrontierFuse hooks into {sp} (backup: {sp.with_suffix('.json.bak')}).")
    print("  gate is INERT until you run `frontier-dispatch arm` in an orchestrator session.")
    return 0


def cmd_uninstall_hooks(_args) -> int:
    sp = _settings_path()
    if not sp.exists():
        print("no settings.json — nothing to remove.")
        return 0
    result = fc.inspect_json_file(sp)
    if result["status"] != "ready":
        print(
            f"REFUSING to modify invalid or unreadable Claude settings {sp}; repair or restore "
            "the file first.",
            file=sys.stderr,
        )
        return 1
    data = result["data"]
    try:
        hooks = _validated_hook_events(data)
    except ValueError as exc:
        print(f"REFUSING to modify malformed Claude hook settings {sp}: {exc}", file=sys.stderr)
        return 1
    removed = 0
    for event, script_name in _EVENT_SCRIPT.items():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            continue
        removed += _strip_our_hooks_from_event(entries, script_name)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    fc.write_text_owner_only(sp, json.dumps(data, indent=2) + "\n")
    print(f"removed {removed} FrontierFuse hook entr{'y' if removed == 1 else 'ies'} from {sp}.")
    return 0


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="frontier-dispatch", description="FrontierFuse orchestrator body-caller")
    ap.add_argument(
        "tasks",
        nargs="*",
        help=(
            "task string(s) to dispatch to selected bodies; combined non-empty count with "
            f"--fanout is hard-capped at {DEFAULT_MAX_TASKS_PER_DISPATCH} "
            f"(FRONTIER_MAX_TASKS, ceiling {MAX_TASKS_HARD_CEILING})"
        ),
    )
    ap.add_argument("--parallel", "-p", action="store_true", help="fan out tasks concurrently")
    ap.add_argument(
        "--fanout",
        default="",
        help=(
            "JSON list of tasks (strings or {task:...}); same hard task-count cap as positional "
            f"tasks ({DEFAULT_MAX_TASKS_PER_DISPATCH}, ceiling {MAX_TASKS_HARD_CEILING})"
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="build the command; make no engine call")
    ap.add_argument(
        "--budget-usd",
        type=float,
        default=0.0,
        help="informational soft budget note only (not enforced; task-count hard cap is separate)",
    )
    ap.add_argument("--timeout", type=int, default=BODY_TIMEOUT, help="per-body timeout seconds")
    ap.add_argument("--model", default=None, help="selected executor model; empty uses provider default [legacy]")
    ap.add_argument("--executor-model", default=None,
                    help="selected executor model; empty uses provider default")
    ap.add_argument("--inherit-fast-model", action="store_true",
                    help="config: clear fast-model pin so it inherits the regular Codex model")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default=None)
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument(
        "--profile",
        choices=sorted(fc.KNOWN_PROFILES),
        default=None,
        help=(
            "advisor (host/executor-led) or orchestrator (host-led verified orchestration with "
            "managed frontier consult/executor bodies); selecting a frontier model never makes it "
            "the host lead"
        ),
    )
    ap.add_argument("--frontier-provider", choices=sorted(fc.KNOWN_EXECUTORS), default=None)
    ap.add_argument("--frontier-model", default=None)
    ap.add_argument("--executor", choices=sorted(fc.KNOWN_EXECUTORS), default=None,
                    help="body provider (codex, claude, grok, or gemini)")
    ap.add_argument("--claude-model", dest="claude_model", default=None)
    ap.add_argument("--grok-model", dest="grok_model", default=None)
    ap.add_argument("--gemini-model", dest="gemini_model", default=None)
    ap.add_argument("--openrouter-model", dest="openrouter_model", default=None,
                    help="model when executor/frontier provider is openrouter")
    ap.add_argument("--provider", choices=sorted(fc.KNOWN_EXECUTORS), default=None,
                    help="models: filter model catalog by provider")
    ap.add_argument("--no-discover", action="store_true",
                    help="models: skip local CLI model discovery")
    ap.add_argument("--gate", default="", help="arm/verify: host-approved acceptance command")
    ap.add_argument("--cwd", default=None, help="arm/verify: verification working directory")
    ap.add_argument("--global", dest="glob", action="store_true", help="config: persist globally")
    ap.add_argument("--repair", action="store_true",
                    help="config: back up and reset malformed session/global configuration")
    ap.add_argument("--update-mode", choices=sorted(fc.UPDATE_MODES), default=None,
                    help="config: passive, manual, or off release reminders")
    ap.add_argument("--check", action="store_true", help="update: check the public release manifest")
    ap.add_argument("--check-updates", action="store_true",
                    help="doctor: check the public release manifest (otherwise offline)")
    ap.add_argument("--force", action="store_true", help="update: bypass cache and disabled mode")
    ap.add_argument("--passive", action="store_true",
                    help="update: honor passive mode and stay silent when current")
    ap.add_argument("--json", action="store_true", help="print machine-readable status when supported")
    # topology / role / consult
    ap.add_argument("--name", dest="role_name", default=None, help="role: name to set/clear")
    ap.add_argument("--kind", dest="role_kind", choices=["consult", "body"], default=None,
                    help="role set: consult or body")
    ap.add_argument("--role-provider", dest="role_provider",
                    choices=sorted(fc.KNOWN_EXECUTORS), default=None,
                    help="role set: provider backend")
    ap.add_argument("--role-model", dest="role_model", default=None, help="role set: model ID")
    ap.add_argument("--role-effort", dest="role_effort",
                    choices=["low", "medium", "high", "xhigh"], default=None,
                    help="role set: effort")
    ap.add_argument("--role", dest="consult_role", default="frontier",
                    help="consult: role name (default frontier)")
    ap.add_argument("--question", dest="consult_question", default=None,
                    help="consult: question text")
    ap.add_argument("--context", default=None, help="consult: optional context")
    return ap



def cmd_topology(args) -> int:
    """Print effective multi-role topology. Pure; never calls providers."""
    import frontier_topology as ft
    try:
        cfg = fc.resolve_config(session_id=SESSION_ID)
    except (ValueError, fc.ConfigFileError, fc.StateFileError) as exc:
        print(f"topology refused: {exc}", file=sys.stderr)
        return 2
    topo = ft.project_topology(cfg)
    if getattr(args, "json", False):
        print(json.dumps(topo, indent=2, sort_keys=True))
        return 0
    print(f"profile: {topo['profile']}")
    print(f"host: {topo['host']['note']}")
    print("native slots:")
    for slot, info in topo["native_slots"].items():
        print(f"  {slot}: {info['provider']} / {info['model']}")
    print("roles:")
    for name, binding in topo["roles"].items():
        effort = f" @{binding['effort']}" if binding.get("effort") else ""
        print(
            f"  {name}: {binding.get('kind')} "
            f"{binding.get('provider')} / {binding.get('model')}{effort} "
            f"[{binding.get('source')}]"
        )
    print("provider crossings (context may leave the machine):")
    for c in topo["provider_crossings"]:
        print(f"  - {c['role']} -> {c['provider']} ({c['kind']})")
    print("recipes: " + ", ".join(topo["recipes"].keys()))
    return 0


def cmd_role(args) -> int:
    """List or bind named roles into session/global config."""
    import frontier_topology as ft
    action = getattr(args, "role_action", None) or "list"
    try:
        if action == "list":
            cfg = fc.resolve_config(session_id=SESSION_ID)
            roles = (cfg.get("roles") or {})
            builtins = ft.builtin_roles(cfg)
            payload = {"builtin": builtins, "custom": roles}
            if getattr(args, "json", False):
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("builtin:")
                for n, b in builtins.items():
                    print(f"  {n}: {b}")
                print("custom:")
                if not roles:
                    print("  (none)")
                for n, b in sorted(roles.items()):
                    print(f"  {n}: {b}")
            return 0
        if action == "clear":
            name = getattr(args, "role_name", None)
            if not name:
                print("role clear requires --name", file=sys.stderr)
                return 2
            name = ft.validate_role_name(name)
            cfg = fc.resolve_config(session_id=SESSION_ID)
            roles = dict(cfg.get("roles") or {})
            if name not in roles:
                print(f"role clear: {name!r} not in custom roles (nothing to do)")
                return 0
            del roles[name]
            fc.update_config_transaction(
                SESSION_ID, {"roles": roles}, global_scope=bool(getattr(args, "glob", False))
            )
            print(f"cleared custom role {name!r}")
            return 0
        if action == "set":
            name = getattr(args, "role_name", None)
            kind = getattr(args, "role_kind", None)
            provider = getattr(args, "role_provider", None)
            model = getattr(args, "role_model", None) or ""
            effort = getattr(args, "role_effort", None)
            if not name or not kind or not provider:
                print(
                    "role set requires --name, --kind consult|body, --provider, and optional --model/--effort",
                    file=sys.stderr,
                )
                return 2
            name = ft.validate_role_name(name)
            binding = {"kind": kind, "provider": provider, "model": model}
            if effort:
                binding["effort"] = effort
            binding = ft.validate_role_binding(binding, source=f"role.set.{name}")
            cfg = fc.resolve_config(session_id=SESSION_ID)
            roles = dict(cfg.get("roles") or {})
            roles[name] = binding
            # validate full map
            roles = ft.validate_roles(roles, source="roles")
            fc.update_config_transaction(
                SESSION_ID, {"roles": roles}, global_scope=bool(getattr(args, "glob", False))
            )
            print(json.dumps({"ok": True, "name": name, "binding": binding}, indent=2))
            return 0
        print(f"role refused: unknown action {action!r}", file=sys.stderr)
        return 2
    except (ValueError, fc.ConfigFileError, fc.StateFileError) as exc:
        print(f"role refused: {exc}", file=sys.stderr)
        return 2


def cmd_consult(args) -> int:
    """Consult a named role (or the frontier slot). Supports --dry-run."""
    import frontier_topology as ft
    question = getattr(args, "consult_question", None) or ""
    if not str(question).strip() and not getattr(args, "dry_run", False):
        # allow remaining positional from tasks? use role_question arg
        pass
    role = getattr(args, "consult_role", None) or "frontier"
    question = getattr(args, "consult_question", None)
    if question is None:
        # fall back to first task-like arg if present
        tasks = getattr(args, "tasks", None) or []
        question = tasks[0] if tasks else ""
    try:
        cfg = fc.resolve_config(session_id=SESSION_ID)
        cfg = ft.cfg_for_role_consult(cfg, role)
        cmd = fc.build_frontier_command(cfg)
    except (ValueError, fc.ConfigFileError, fc.StateFileError) as exc:
        print(f"consult refused: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "dry_run", False):
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "role": role,
            "provider": cfg.get("frontier_provider"),
            "model": fc.effective_frontier_model(cfg),
            "command": cmd,
            "context_leaves_machine": True,
        }, indent=2))
        return 0
    if not str(question).strip():
        print("consult refused: question required (unless --dry-run)", file=sys.stderr)
        return 2
    # Build a one-shot consult using the role-mapped frontier command.
    prompt = str(question)
    ctx = getattr(args, "context", None)
    if ctx:
        prompt = f"Context:\n{ctx}\n\nQuestion:\n{question}"
    note = (
        f"Consulting role {role!r} via provider {cfg.get('frontier_provider')} "
        f"(context leaves this machine)."
    )
    print(note, file=sys.stderr)
    try:
        # Prefer prompt-file transport when command expects it.
        cmd_tokens = list(cmd)
        if "{prompt_file}" in cmd_tokens:
            # run_engine-style: write owner-only prompt file
            import tempfile
            from pathlib import Path
            runs = Path(tempfile.mkdtemp(prefix="frontier-consult-"))
            try:
                pf = runs / "prompt.txt"
                fc.write_text_owner_only(pf, prompt)
                cmd_tokens = [pf.as_posix() if t == "{prompt_file}" else t for t in cmd_tokens]
                # OpenRouter dry helper not used for live; execute argv.
                proc = subprocess.run(cmd_tokens, capture_output=True, text=True,
                                      timeout=float(getattr(args, "timeout", None) or 180))
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                if proc.returncode != 0:
                    print(err or out or f"consult failed rc={proc.returncode}", file=sys.stderr)
                    return proc.returncode or 1
                print(out)
                return 0
            finally:
                import shutil
                shutil.rmtree(runs, ignore_errors=True)
        # stdin providers (codex) / claude -p
        proc = subprocess.run(
            cmd_tokens,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=float(getattr(args, "timeout", None) or 180),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            print(err or out or f"consult failed rc={proc.returncode}", file=sys.stderr)
            return proc.returncode or 1
        print(out)
        return 0
    except subprocess.TimeoutExpired:
        print("consult refused: timeout", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"consult refused: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in SUBCOMMANDS else "dispatch"
    rest = argv[1:] if (argv and argv[0] in SUBCOMMANDS) else argv
    role_action = None
    if sub == "role" and rest and rest[0] in {"list", "set", "clear"}:
        role_action = rest[0]
        rest = rest[1:]
    args = _build_parser().parse_args(rest)
    if role_action is not None:
        args.role_action = role_action
    elif sub == "role":
        args.role_action = "list"

    handlers = {
        "dispatch": cmd_dispatch, "arm": cmd_arm, "disarm": cmd_disarm, "done": cmd_done,
        "verify": cmd_verify, "config": cmd_config, "models": cmd_models,
        "doctor": cmd_doctor, "update": cmd_update,
        "install-hooks": cmd_install_hooks, "uninstall-hooks": cmd_uninstall_hooks,
        "topology": cmd_topology, "role": cmd_role, "consult": cmd_consult,
    }
    try:
        return handlers[sub](args)
    except (ValueError, OSError) as exc:
        print(f"frontier-dispatch refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
