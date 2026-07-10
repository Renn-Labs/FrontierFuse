#!/usr/bin/env python3
"""fable_dispatch.py — orchestrator-mode body-caller + control CLI for FableFuse.

In orchestrator mode Fable (the in-session brain) never executes directly. It delegates every
execution/research/tool/MCP task to the selected body/lead executor through this CLI, reads the bounded
handoff cards, verifies against raw diff + gate stdout, and only closes on a fresh GREEN verdict.

Subcommands:
  dispatch "task" [...]        run one selected body/lead executor (or several with --parallel)
  --parallel / -p t...         fan out N concurrent bodies (cap FABLE_MAX_PARALLEL, default 4)
  --fanout tasks.json          fan out tasks from a JSON list (strings or {"task": ...})
  arm --gate "pytest -q"       arm and freeze a host-approved acceptance gate
  disarm | done                explicitly override, or close on snapshot-bound GREEN
  verify                       run the frozen gate while armed -> verdict.json
  config [--executor codex|sonnet|opus|grok --model --sonnet-model --opus-model --grok-model --effort --fast on|off --global]
                                print/persist toggles
  doctor                       readiness table
  install-hooks | uninstall-hooks   reversible merge of the hooks into ~/.claude/settings.json

stdlib-only, Python 3.10+, importable.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fable_common as fc

SESSION_ID = (os.environ.get("FABLE_SESSION_ID")
              or os.environ.get("CLAUDE_CODE_SESSION_ID")
              or "default")
MAX_PARALLEL = int(os.environ.get("FABLE_MAX_PARALLEL", "4"))
BODY_TIMEOUT = int(os.environ.get("FABLE_BODY_TIMEOUT", "900"))
SUBCOMMANDS = {"dispatch", "arm", "disarm", "done", "verify", "config", "doctor",
               "install-hooks", "uninstall-hooks"}


# --------------------------------------------------------------------------- #
# dispatch — run selected bodies
# --------------------------------------------------------------------------- #
def _overrides(args) -> dict:
    ov: dict = {}
    if args.model:
        ov["codex_model"] = args.model
    if args.effort:
        ov["codex_effort"] = args.effort
        ov["grok_effort"] = args.effort
    if args.fast:
        ov["fast"] = (args.fast == "on")
    if getattr(args, "executor", None):
        ov["executor"] = args.executor
    if getattr(args, "sonnet_model", None):
        ov["sonnet_model"] = args.sonnet_model
    if getattr(args, "opus_model", None):
        ov["opus_model"] = args.opus_model
    if getattr(args, "grok_model", None):
        ov["grok_model"] = args.grok_model
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

    cfg = fc.resolve_config(overrides=_overrides(args), session_id=SESSION_ID)
    try:
        cmd = fc.build_body_command(cfg)
    except ValueError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 2
    run_id = fc.new_run_id()
    if not args.dry_run:
        fc.mkdir_owner_only(fc.RUNS_DIR / f"fable-{run_id}")
    if args.budget_usd:
        print(f"# soft budget: ${args.budget_usd:.2f} (informational; provider billing is external)",
              file=sys.stderr)

    parallel = args.parallel or len(tasks) > 1
    cards: list[dict] = []
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
    cards.sort(key=lambda c: c.get("label", ""))  # parallel dispatch completes out of order

    # a dispatch happened -> any prior verdict is now stale (must re-verify)
    if not args.dry_run:
        fc.write_state(SESSION_ID, last_dispatch_ts=time.time())
    print(json.dumps({"run_id": run_id, "mode": cfg, "count": len(cards), "cards": cards}, indent=2))
    return 0 if all(c.get("ok") for c in cards) else 1


# --------------------------------------------------------------------------- #
# state toggles
# --------------------------------------------------------------------------- #
def cmd_arm(args) -> int:
    """Arm the workflow guardrail and freeze the host-approved verification command."""
    approved_gate = None
    if args.gate:
        import fable_verify

        try:
            gate_argv = fable_verify.parse_gate_argv(args.gate)
        except (TypeError, ValueError) as exc:
            print(f"arm refused: invalid verification command: {exc}", file=sys.stderr)
            return 2
        workspace = str(Path(args.cwd or ".").resolve())
        if not fable_verify.is_git_worktree(workspace):
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
    fc.write_state(SESSION_ID, armed=True, approved_gate=approved_gate, verdict=None)
    if approved_gate:
        print(
            f"armed (session {SESSION_ID}) - workflow guardrail active with a frozen verification "
            "command. Delegate execution to the selected body, run `fable-dispatch verify`, and "
            "finish only on a snapshot-bound GREEN. Kill-switch: FABLE_GUARDS_OFF=1."
        )
    else:
        print(
            f"armed (session {SESSION_ID}) - workflow guardrail active, but no verification command "
            "was approved. Disarm and re-arm with `--gate \"<tests/build/lint>\"` before delegating "
            "if the model must be able to verify and finish. Kill-switch: FABLE_GUARDS_OFF=1."
        )
    return 0


def cmd_disarm(_args) -> int:
    fc.write_state(SESSION_ID, armed=False, approved_gate=None)
    print(f"disarmed (session {SESSION_ID}) - workflow guardrail off.")
    return 0


def cmd_done(_args) -> int:
    """Disarm ONLY on a fresh GREEN verdict — the loop's core invariant. `done` without one
    leaves the gate armed (use `disarm` explicitly to override, which is a distinct, deliberate
    escape hatch — not something `done` does silently)."""
    import fable_verify

    st = fc.read_state(SESSION_ID)
    approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else {}
    fresh = fable_verify.verdict_is_snapshot_fresh_green(
        st.get("verdict"),
        st.get("last_dispatch_ts", 0),
        session_id=SESSION_ID,
        cwd=approved.get("cwd"),
        approved_gate=approved,
    )
    if not fresh:
        print("done - REFUSED: no fresh snapshot-bound GREEN verdict recorded for the last "
              "dispatch. Guardrail stays armed. Run `fable-dispatch verify` until GREEN, or "
              "`fable-dispatch disarm` from the host to override deliberately.", file=sys.stderr)
        return 1
    fc.write_state(SESSION_ID, armed=False, approved_gate=None)
    print("done - fresh snapshot-bound GREEN still matches the workspace. Guardrail disarmed.")
    return 0


# --------------------------------------------------------------------------- #
# verify (delegates to fable_verify)
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    import fable_verify

    st = fc.read_state(SESSION_ID)
    approved = st.get("approved_gate") if isinstance(st.get("approved_gate"), dict) else None
    gate = args.gate
    cwd = args.cwd or "."
    if st.get("armed"):
        if not approved or not isinstance(approved.get("argv"), list):
            print(
                "verify refused: the armed session has no frozen verification command; disarm and "
                "re-arm with `fable-dispatch arm --gate \"<command>\"`.",
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

    v = fable_verify.run_gate(gate, session_id=SESSION_ID, cwd=cwd)
    print(json.dumps({k: v[k] for k in ("result", "gate", "exit_code", "diff_sha", "paths", "ts")},
                     indent=2))
    return 0 if v["result"] == "GREEN" else 1


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def cmd_config(args) -> int:
    patch: dict = {}
    if args.model is not None:
        patch["codex_model"] = args.model
    if args.effort is not None:
        patch["codex_effort"] = args.effort
        patch["grok_effort"] = args.effort
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
    if args.sonnet_model is not None:
        patch["sonnet_model"] = args.sonnet_model
    if args.opus_model is not None:
        patch["opus_model"] = args.opus_model
    if args.grok_model is not None:
        patch["grok_model"] = args.grok_model
    if patch:
        if args.glob:
            fc.save_global_config(patch)
        else:
            fc.write_state(SESSION_ID, config=patch)
    cfg = fc.resolve_config(session_id=SESSION_ID)
    print(json.dumps(cfg, indent=2, sort_keys=True))
    scope = "global" if args.glob else "session" if patch else "effective"
    print(f"# scope: {scope}", file=sys.stderr)
    try:
        body_cmd = " ".join(fc.build_body_command(cfg))
    except ValueError as exc:
        body_cmd = f"<unavailable: {exc}>"
    print("body cmd :", body_cmd, file=sys.stderr)
    print("brain cmd:", " ".join(fc.build_fable_command(cfg)), file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(_args) -> int:
    cfg = fc.resolve_config(session_id=SESSION_ID)
    body_err = None
    try:
        body_cmd = fc.build_body_command(cfg)
    except ValueError as exc:
        body_cmd = []
        body_err = str(exc)
    fable_cmd = fc.build_fable_command(cfg)
    settings = Path.home() / ".claude" / "settings.json"
    manual_hooks_installed = settings.is_file() and "fable_gate.py" in settings.read_text()
    plugin_manifest_present = (HERE / ".claude-plugin" / "plugin.json").is_file()
    running_as_plugin = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    state_ok = os.access(fc.STATE_DIR.parent if not fc.STATE_DIR.exists() else fc.STATE_DIR,
                         os.W_OK) or _mkdir_ok(fc.STATE_DIR)

    def mark(ok):
        return "\033[32mok\033[0m" if ok else "\033[33m--\033[0m"

    if running_as_plugin:
        install_row = ("hooks", True, f"auto-registered by Claude Code plugin (${{CLAUDE_PLUGIN_ROOT}}={os.environ['CLAUDE_PLUGIN_ROOT']})")
    elif manual_hooks_installed:
        install_row = ("hooks", True, f"manually installed (Option B) — {settings}")
    else:
        install_row = ("hooks", False,
                       "not installed — run `/plugin marketplace add Renn-Labs/FableFuse` then "
                       "`/plugin install fablefuse@fablefuse` (or `install-hooks` for the manual path)")

    grok_cmd = fc.build_grok_command(cfg)
    body_info = body_err if body_err else " ".join(body_cmd)
    body_ok = (not body_err) and bool(body_cmd and shutil.which(body_cmd[0]))
    rows = [
        (f"{cfg['executor']} body CLI", body_ok, body_info),
        ("grok build CLI", bool(shutil.which(grok_cmd[0])), " ".join(grok_cmd)),
        ("fable brain CLI", bool(shutil.which(fable_cmd[0])), " ".join(fable_cmd)),
        ("plugin manifest", plugin_manifest_present, str(HERE / ".claude-plugin" / "plugin.json")),
        install_row,
        ("state dir writable", state_ok, str(fc.STATE_DIR)),
    ]
    print("FableFuse doctor\n")
    for label, ok, info in rows:
        print(f"  {mark(ok)}  {label:22} {info}")
    ready = body_ok
    print(f"\n{'READY' if ready else 'NOT READY'} — need the {cfg['executor']} body CLI on PATH for "
          f"live runs (offline tests/dry-run work regardless).")
    return 0 if ready else 1


def _mkdir_ok(p: Path) -> bool:
    try:
        fc.mkdir_owner_only(p)
        return os.access(p, os.W_OK)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# install-hooks / uninstall-hooks (reversible merge into ~/.claude/settings.json)
# --------------------------------------------------------------------------- #
def _settings_path() -> Path:
    cfgdir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return cfgdir / "settings.json"


def _our_commands() -> tuple[str, str]:
    return (f"python3 {HERE / 'hooks' / 'fable_gate.py'}",
            f"python3 {HERE / 'hooks' / 'fable_verify_gate.py'}")


def cmd_install_hooks(_args) -> int:
    pre_cmd, stop_cmd = _our_commands()
    sp = _settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    existing_text = sp.read_text() if sp.exists() else ""
    data = fc._read_json(sp) if sp.exists() else {}
    if sp.exists():
        sp.with_suffix(".json.bak").write_text(existing_text)
        if existing_text.strip() and not data:
            print(f"WARNING: {sp} did not parse as valid JSON — it will be REPLACED "
                  f"(original preserved at {sp.with_suffix('.json.bak')}). Restore it manually "
                  f"if this settings.json had content you need.", file=sys.stderr)
    hooks = data.setdefault("hooks", {})

    def _has(event: str, command: str) -> bool:
        for entry in hooks.get(event, []):
            for h in entry.get("hooks", []):
                if h.get("command") == command:
                    return True
        return False

    if not _has("PreToolUse", pre_cmd):
        hooks.setdefault("PreToolUse", []).append(
            {"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
             "hooks": [{"type": "command", "command": pre_cmd}]})
    if not _has("Stop", stop_cmd):
        hooks.setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": stop_cmd}]})

    sp.write_text(json.dumps(data, indent=2) + "\n")
    print(f"installed FableFuse hooks into {sp} (backup: {sp.with_suffix('.json.bak')}).")
    print("  gate is INERT until you run `fable-dispatch arm` in an orchestrator session.")
    return 0


def cmd_uninstall_hooks(_args) -> int:
    pre_cmd, stop_cmd = _our_commands()
    sp = _settings_path()
    if not sp.exists():
        print("no settings.json — nothing to remove.")
        return 0
    data = fc._read_json(sp)
    hooks = data.get("hooks", {})
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
    sp.write_text(json.dumps(data, indent=2) + "\n")
    print(f"removed {removed} FableFuse hook entr{'y' if removed == 1 else 'ies'} from {sp}.")
    return 0


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fable-dispatch", description="FableFuse orchestrator body-caller")
    ap.add_argument("tasks", nargs="*", help="task string(s) to dispatch to selected bodies")
    ap.add_argument("--parallel", "-p", action="store_true", help="fan out tasks concurrently")
    ap.add_argument("--fanout", default="", help="JSON file of tasks (strings or {task:...})")
    ap.add_argument("--dry-run", action="store_true", help="build the command; make no engine call")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="informational soft budget note")
    ap.add_argument("--timeout", type=int, default=BODY_TIMEOUT, help="per-body timeout seconds")
    ap.add_argument("--model", default=None, help="override codex body model for this run")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument("--executor", choices=["codex", "sonnet", "opus", "grok"], default=None,
                    help="body/driver engine (codex, sonnet, opus, or grok)")
    ap.add_argument("--sonnet-model", dest="sonnet_model", default=None)
    ap.add_argument("--opus-model", dest="opus_model", default=None)
    ap.add_argument("--grok-model", dest="grok_model", default=None)
    ap.add_argument("--gate", default="", help="arm/verify: host-approved acceptance command")
    ap.add_argument("--cwd", default=None, help="arm/verify: verification working directory")
    ap.add_argument("--global", dest="glob", action="store_true", help="config: persist globally")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in SUBCOMMANDS else "dispatch"
    rest = argv[1:] if (argv and argv[0] in SUBCOMMANDS) else argv
    args = _build_parser().parse_args(rest)

    handlers = {
        "dispatch": cmd_dispatch, "arm": cmd_arm, "disarm": cmd_disarm, "done": cmd_done,
        "verify": cmd_verify, "config": cmd_config, "doctor": cmd_doctor,
        "install-hooks": cmd_install_hooks, "uninstall-hooks": cmd_uninstall_hooks,
    }
    return handlers[sub](args)


if __name__ == "__main__":
    raise SystemExit(main())
