#!/usr/bin/env python3
"""frontier_dispatch.py — orchestrator-mode body-caller + control CLI for FrontierFuse.

In orchestrator mode Fable (the in-session brain) never executes directly. It delegates every
execution/research/tool/MCP task to the selected body/lead executor through this CLI, reads the bounded
handoff cards, verifies against raw diff + gate stdout, and only closes on a fresh GREEN verdict.

Subcommands:
  dispatch "task" [...]        run one selected body/lead executor (or several with --parallel)
  --parallel / -p t...         fan out N concurrent bodies (cap FRONTIER_MAX_PARALLEL, default 4)
  --fanout tasks.json          fan out tasks from a JSON list (strings or {"task": ...})
  arm --gate "pytest -q"       arm and freeze a host-approved acceptance gate
  disarm | done                explicitly override, or close on snapshot-bound GREEN
  verify                       run the frozen gate while armed -> verdict.json
  config [--profile advisor|orchestrator --frontier-provider PROVIDER --frontier-model MODEL
          --executor PROVIDER --model MODEL --effort --fast on|off --global]
                                print/persist toggles
  models [--provider PROVIDER]  verified catalog plus local CLI discoveries
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
import shutil
import stat
import sys
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
SUBCOMMANDS = {"dispatch", "arm", "disarm", "done", "verify", "config", "models", "doctor", "update",
               "install-hooks", "uninstall-hooks"}

_EXECUTOR_MODEL_KEYS = {
    "codex": "codex_model",
    "claude": "claude_model",
    "grok": "grok_model",
    "gemini": "gemini_model",
}


# --------------------------------------------------------------------------- #
# dispatch — run selected bodies
# --------------------------------------------------------------------------- #
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
    if args.model is not None:
        model_key = "fast_model" if fast and executor == "codex" else _EXECUTOR_MODEL_KEYS[executor]
        ov[model_key] = args.model
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
    if getattr(args, "frontier_model", None):
        ov["frontier_model"] = args.frontier_model
    if getattr(args, "claude_model", None):
        ov["claude_model"] = args.claude_model
    if getattr(args, "grok_model", None):
        ov["grok_model"] = args.grok_model
    if getattr(args, "gemini_model", None):
        ov["gemini_model"] = args.gemini_model
    return ov


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
    tasks = list(args.tasks)
    if args.fanout:
        raw = json.loads(Path(args.fanout).read_text())
        tasks += [t if isinstance(t, str) else str(t.get("task", "")) for t in raw]
    tasks = [t for t in tasks if t.strip()]
    if not tasks:
        print("no tasks given", file=sys.stderr)
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
    if inherit_fast_model and args.model is not None:
        print(
            "config refused: --inherit-fast-model cannot be combined with --model",
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
    if args.model is not None:
        model_key = (
            "fast_model" if fast and executor == "codex"
            else _EXECUTOR_MODEL_KEYS[executor]
        )
        patch[model_key] = args.model
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
    print("brain cmd:", " ".join(fc.build_frontier_command(cfg)), file=sys.stderr)
    return 0


def cmd_models(args) -> int:
    import frontier_models

    providers = [args.provider] if args.provider else sorted(frontier_models.PROVIDERS)
    payload = {
        provider: {
            "source": frontier_models.SOURCES[provider],
            "models": frontier_models.models_for(provider, discover=not args.no_discover),
            "custom_model_allowed": True,
        }
        for provider in providers
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for provider in providers:
        print(f"{provider} ({payload[provider]['source']})")
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
        payload = {"status": "config_invalid", "ready": False, "checks": [check]}
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
        payload = {"status": "not_ready", "ready": False, "checks": [check]}
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
        payload = {"status": "config_invalid", "ready": False, "checks": [check]}
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
            pre_cmd, stop_cmd = _our_commands()

            def _has_hook(event: str, command: str) -> bool:
                for entry in hooks.get(event, []):
                    if not _matcher_covers(event, entry.get("matcher", "")):
                        continue
                    for hook in entry.get("hooks", []):
                        if hook.get("type") == "command" and hook.get("command") == command:
                            return True
                return False

            manual_hooks_installed = (
                _has_hook("PreToolUse", pre_cmd) and _has_hook("Stop", stop_cmd)
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
    if args.json:
        print(json.dumps({
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checks": checks,
            "effective_config": cfg,
        }, indent=2, sort_keys=True, allow_nan=False))
        return 0 if ready else 1

    print("FrontierFuse doctor\n")
    for label, ok, status, info, next_step in rows:
        print(f"  {mark(ok)}  {label:22} {status.upper()}: {info}")
        if next_step:
            print(f"      next: {next_step}")
    print(f"\n{'READY' if ready else 'NOT READY'} — offline CLI readiness only: need the "
          f"{cfg['executor']} body CLI on PATH and the {cfg['frontier_provider']} frontier CLI "
          "for managed advice. Authentication and model entitlement are not probed; offline "
          "tests/dry-run work regardless.")
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


def _our_commands() -> tuple[str, str]:
    return (f"python3 {HERE / 'hooks' / 'frontier_gate.py'}",
            f"python3 {HERE / 'hooks' / 'frontier_verify_gate.py'}")


def _matcher_covers(event: str, matcher: str) -> bool:
    if not isinstance(matcher, str):
        return False
    if event == "Stop":
        return True
    if matcher in {"", "*"}:
        return True
    required = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
    return required.issubset({part.strip() for part in matcher.split("|") if part.strip()})


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
                if hook.get("type") == "command" and (
                    not isinstance(hook.get("command"), str) or not hook.get("command")
                ):
                    raise ValueError(f"hooks.{event} contains an invalid command hook")
    return hooks


def cmd_install_hooks(_args) -> int:
    pre_cmd, stop_cmd = _our_commands()
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

    def _has(event: str, command: str) -> bool:
        for entry in hooks.get(event, []):
            if not _matcher_covers(event, entry.get("matcher", "")):
                continue
            for h in entry.get("hooks", []):
                if h.get("type") == "command" and h.get("command") == command:
                    return True
        return False

    if not _has("PreToolUse", pre_cmd):
        hooks.setdefault("PreToolUse", []).append(
            {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
             "hooks": [{"type": "command", "command": pre_cmd}]})
    if not _has("Stop", stop_cmd):
        hooks.setdefault("Stop", []).append(
            {"matcher": "*", "hooks": [{"type": "command", "command": stop_cmd}]})

    fc.write_text_owner_only(sp, json.dumps(data, indent=2) + "\n")
    print(f"installed FrontierFuse hooks into {sp} (backup: {sp.with_suffix('.json.bak')}).")
    print("  gate is INERT until you run `frontier-dispatch arm` in an orchestrator session.")
    return 0


def cmd_uninstall_hooks(_args) -> int:
    pre_cmd, stop_cmd = _our_commands()
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
    for event in ("PreToolUse", "Stop"):
        keep = []
        for entry in hooks.get(event, []):
            inner = [h for h in entry.get("hooks", []) if h.get("command") not in (pre_cmd, stop_cmd)]
            removed += len(entry.get("hooks", [])) - len(inner)
            if inner:
                entry["hooks"] = inner
                keep.append(entry)
        if keep:
            hooks[event] = keep
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
    ap.add_argument("tasks", nargs="*", help="task string(s) to dispatch to selected bodies")
    ap.add_argument("--parallel", "-p", action="store_true", help="fan out tasks concurrently")
    ap.add_argument("--fanout", default="", help="JSON file of tasks (strings or {task:...})")
    ap.add_argument("--dry-run", action="store_true", help="build the command; make no engine call")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="informational soft budget note")
    ap.add_argument("--timeout", type=int, default=BODY_TIMEOUT, help="per-body timeout seconds")
    ap.add_argument("--model", default=None, help="selected executor model; empty uses provider default")
    ap.add_argument("--inherit-fast-model", action="store_true",
                    help="config: clear fast-model pin so it inherits the regular Codex model")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default=None)
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument("--profile", choices=sorted(fc.KNOWN_PROFILES), default=None,
                    help="advisor (executor-led) or orchestrator (frontier-led)")
    ap.add_argument("--frontier-provider", choices=sorted(fc.KNOWN_EXECUTORS), default=None)
    ap.add_argument("--frontier-model", default=None)
    ap.add_argument("--executor", choices=sorted(fc.KNOWN_EXECUTORS), default=None,
                    help="body provider (codex, claude, grok, or gemini)")
    ap.add_argument("--claude-model", dest="claude_model", default=None)
    ap.add_argument("--grok-model", dest="grok_model", default=None)
    ap.add_argument("--gemini-model", dest="gemini_model", default=None)
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
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in SUBCOMMANDS else "dispatch"
    rest = argv[1:] if (argv and argv[0] in SUBCOMMANDS) else argv
    args = _build_parser().parse_args(rest)

    handlers = {
        "dispatch": cmd_dispatch, "arm": cmd_arm, "disarm": cmd_disarm, "done": cmd_done,
        "verify": cmd_verify, "config": cmd_config, "models": cmd_models,
        "doctor": cmd_doctor, "update": cmd_update,
        "install-hooks": cmd_install_hooks, "uninstall-hooks": cmd_uninstall_hooks,
    }
    try:
        return handlers[sub](args)
    except (ValueError, OSError) as exc:
        print(f"frontier-dispatch refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
