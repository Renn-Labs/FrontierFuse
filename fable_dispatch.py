#!/usr/bin/env python3
"""fable_dispatch.py — orchestrator-mode body-caller + control CLI for FableFuse.

In orchestrator mode Fable (the in-session brain) never executes directly. It delegates every
execution/research/tool/MCP task to Codex 5.5-high bodies through this CLI, reads the bounded
handoff cards, verifies against raw diff + gate stdout, and only closes on a fresh GREEN verdict.

Subcommands:
  dispatch "task" [...]        run one Codex body (or several with --parallel)
  --parallel / -p t...         fan out N concurrent bodies (cap FABLE_MAX_PARALLEL, default 4)
  --fanout tasks.json          fan out tasks from a JSON list (strings or {"task": ...})
  arm | disarm | done          toggle the per-session hard-gate marker
  verify --gate "pytest -q"     run a deterministic acceptance gate -> verdict.json
  config [--model --effort --fast on|off --global]   print/persist toggles
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
# dispatch — run Codex bodies
# --------------------------------------------------------------------------- #
def _overrides(args) -> dict:
    ov: dict = {}
    if args.model:
        ov["codex_model"] = args.model
    if args.effort:
        ov["codex_effort"] = args.effort
    if args.fast:
        ov["fast"] = (args.fast == "on")
    if getattr(args, "executor", None):
        ov["executor"] = args.executor
    if getattr(args, "sonnet_model", None):
        ov["sonnet_model"] = args.sonnet_model
    return ov


def _run_one(cmd: list[str], task: str, run_id: str, label: str, timeout: int, dry: bool) -> dict:
    if dry:
        return {"label": label, "ok": True, "note": "dry-run",
                "task": task[:200], "summary": "[dry-run] " + " ".join(cmd) + f"  <<< {task[:80]}",
                "artifact": "", "raw_sha256": "", "raw_bytes": 0}
    rc, out, err = fc.run_engine(cmd, task, timeout=timeout)
    text = out or err
    artifact = fc.write_artifact(fc.RUNS_DIR, run_id, label, task, text)
    return fc.handoff_card(label, task, text, artifact, ok=(rc == 0),
                           note="" if rc == 0 else f"exit {rc}")


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
    cmd = fc.build_body_command(cfg)
    run_id = fc.new_run_id()
    if args.budget_usd:
        print(f"# soft budget: ${args.budget_usd:.2f} (informational; Codex billing is external)",
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
def cmd_arm(_args) -> int:
    fc.write_state(SESSION_ID, armed=True)
    print(f"armed (session {SESSION_ID}) — hard gate active. Delegate execution to Codex; "
          f"finish only on a fresh GREEN verdict. Kill-switch: FABLE_GUARDS_OFF=1.")
    return 0


def cmd_disarm(_args) -> int:
    fc.write_state(SESSION_ID, armed=False)
    print(f"disarmed (session {SESSION_ID}) — hard gate off.")
    return 0


def cmd_done(_args) -> int:
    """Disarm ONLY on a fresh GREEN verdict — the loop's core invariant. `done` without one
    leaves the gate armed (use `disarm` explicitly to override, which is a distinct, deliberate
    escape hatch — not something `done` does silently)."""
    st = fc.read_state(SESSION_ID)
    fresh = fc.verdict_is_fresh_green(st.get("verdict"), st.get("last_dispatch_ts", 0))
    if not fresh:
        print("done — REFUSED: no fresh GREEN verdict recorded for the last dispatch. Gate "
              "stays armed. Run `fable-dispatch verify --gate \"<cmd>\"` until GREEN, or "
              "`fable-dispatch disarm` to override deliberately.", file=sys.stderr)
        return 1
    fc.write_state(SESSION_ID, armed=False)
    print("done — fresh GREEN verdict on record. Gate disarmed.")
    return 0


# --------------------------------------------------------------------------- #
# verify (delegates to fable_verify)
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    import fable_verify
    v = fable_verify.run_gate(args.gate, session_id=SESSION_ID, cwd=args.cwd)
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
    if args.fast is not None:
        patch["fast"] = (args.fast == "on")
    if args.executor is not None:
        patch["executor"] = args.executor
    if args.sonnet_model is not None:
        patch["sonnet_model"] = args.sonnet_model
    if patch:
        if args.glob:
            fc.save_global_config(patch)
        else:
            fc.write_state(SESSION_ID, config=patch)
    cfg = fc.resolve_config(session_id=SESSION_ID)
    print(json.dumps(cfg, indent=2, sort_keys=True))
    scope = "global" if args.glob else "session" if patch else "effective"
    print(f"# scope: {scope}", file=sys.stderr)
    print("body cmd :", " ".join(fc.build_body_command(cfg)), file=sys.stderr)
    print("brain cmd:", " ".join(fc.build_fable_command(cfg)), file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(_args) -> int:
    cfg = fc.resolve_config(session_id=SESSION_ID)
    body_cmd = fc.build_body_command(cfg)
    fable_cmd = fc.build_fable_command(cfg)
    settings = Path.home() / ".claude" / "settings.json"
    hooks_installed = settings.is_file() and "fable_gate.py" in settings.read_text()
    state_ok = os.access(fc.STATE_DIR.parent if not fc.STATE_DIR.exists() else fc.STATE_DIR,
                         os.W_OK) or _mkdir_ok(fc.STATE_DIR)

    def mark(ok):
        return "\033[32mok\033[0m" if ok else "\033[33m--\033[0m"

    rows = [
        (f"{cfg['executor']} body CLI", bool(shutil.which(body_cmd[0])), " ".join(body_cmd)),
        ("fable brain CLI", bool(shutil.which(fable_cmd[0])), " ".join(fable_cmd)),
        ("hooks installed", hooks_installed, str(settings)),
        ("state dir writable", state_ok, str(fc.STATE_DIR)),
    ]
    print("FableFuse doctor\n")
    for label, ok, info in rows:
        print(f"  {mark(ok)}  {label:22} {info}")
    ready = bool(shutil.which(body_cmd[0]))
    print(f"\n{'READY' if ready else 'NOT READY'} — need the {cfg['executor']} body CLI on PATH for "
          f"live runs (offline tests/dry-run work regardless).")
    return 0 if ready else 1


def _mkdir_ok(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
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
    ap.add_argument("tasks", nargs="*", help="task string(s) to dispatch to Codex bodies")
    ap.add_argument("--parallel", "-p", action="store_true", help="fan out tasks concurrently")
    ap.add_argument("--fanout", default="", help="JSON file of tasks (strings or {task:...})")
    ap.add_argument("--dry-run", action="store_true", help="build the command; make no engine call")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="informational soft budget note")
    ap.add_argument("--timeout", type=int, default=BODY_TIMEOUT, help="per-body timeout seconds")
    ap.add_argument("--model", default=None, help="override codex body model for this run")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument("--executor", choices=["codex", "sonnet"], default=None,
                    help="body/driver engine (codex 5.5-high or sonnet 5)")
    ap.add_argument("--sonnet-model", dest="sonnet_model", default=None)
    ap.add_argument("--gate", default="", help="verify: acceptance command")
    ap.add_argument("--cwd", default=".", help="verify: working dir")
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
    if sub == "verify" and not args.gate:
        print("verify requires --gate \"<command>\"", file=sys.stderr)
        return 2
    return handlers[sub](args)


if __name__ == "__main__":
    raise SystemExit(main())
