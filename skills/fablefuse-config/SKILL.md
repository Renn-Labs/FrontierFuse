---
name: fablefuse-config
description: >
  Interactively configure FableFuse's executor/lead (codex/sonnet/opus/grok), model, effort, and fast-mode
  settings — for this session only, or permanently. Triggers on /fablefuse-config, "configure
  fablefuse", "change fablefuse executor/model/effort", "fablefuse settings".
disable-model-invocation: true
---

# FableFuse Config

Change FableFuse's body/executor settings **mid-flight** — no session restart needed, safe to run
at any point whether you're in advisor or orchestrator mode. `disable-model-invocation: true` means
this only runs when the user explicitly asks for it (`/fablefuse-config` or the phrases above) —
never auto-triggered mid-task, so the controller never silently reconfigures itself behind the
user's back.

## Steps

1. **Show the current effective config** so the user has a baseline:
   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/fable_dispatch.py" config
   ```
   (Falls back to `fable_dispatch.py` on `PATH`, or the repo-relative path, if
   `$CLAUDE_PLUGIN_ROOT` isn't set — e.g. `echo $CLAUDE_PLUGIN_ROOT` to check, or resolve via
   `bin/fable-dispatch` if you added it to `PATH`.)

2. **Ask, via `AskUserQuestion`, in one batch:**
   - **Scope** — "This session only" (default) vs. "Permanently (all future sessions)". Maps to
     omitting vs. passing `--global`.
   - **Executor/lead** — "Codex (default, unpinned model)" vs. "Sonnet 5 (`claude-sonnet-5`)" vs.
     "Opus 4.8 (`claude-opus-4-8`)" vs. "Grok 4.5 (`grok-4.5`)". Maps to
     `--executor codex|sonnet|opus|grok`.
   - **Effort** — "high (default)" / "medium" / "low". Maps to `--effort`.
   - **Fast mode** — "off (default)" vs. "on (lower effort, quicker bodies)". Maps to
     `--fast on|off`.
   Use the "Other" free-text option (always available) if the user wants to pin a specific model
   instead of the default (`--model <id>` for Codex, `--sonnet-model <id>` for Sonnet,
   `--opus-model <id>` for Opus, or `--grok-model <id>` for Grok).

   **Do not** offer GPT-5.6 as a default choice. The exact limited-preview IDs are
   `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` for entitled organizations — never imply
   general or ChatGPT availability. Offer an optional pin only when the user explicitly requests
   one and knows their entitlement.

3. **Apply it** by calling the existing, tested config command — this is the *only* place settings
   are written; do not hand-edit config files directly:
   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/fable_dispatch.py" config \
     --executor <codex|sonnet|opus|grok> --effort <low|medium|high> --fast <on|off> \
     [--model <id>] [--sonnet-model <id>] [--opus-model <id>] [--grok-model <id>] [--global]
   ```

4. **Confirm** — print the effective config again (step 1's command) and tell the user plainly:
   **"Applied. Takes effect on the next `fable-dispatch` call — it does not change a body that's
   already running."**

## Safe permission defaults (remind when relevant)

This skill only changes executor/model/effort/fast. Elevated autonomy is **not** stored in config —
it is host env opt-in:

| Env | When set |
|-|-|
| `FABLE_CODEX_YOLO=1` | Codex body gets `--yolo` |
| `FABLE_GROK_YOLO=1` | Grok body gets `--permission-mode bypassPermissions` |
| `FABLE_GROK_PERMISSION_MODE=<mode>` | explicit Grok permission mode |

Default (0.2.6+): inherit provider permission defaults (no automatic YOLO/bypass). Grok dispatch
still uses `--prompt-file` for large specs regardless of permission mode.

## Notes

- Scope precedence (see `fable_common.resolve_config`): per-call flag > session config >
  `~/.config/fable-fuse/config.json` (global, `--global`) > env > built-in default. Session-only
  changes here mean the *current* FableFuse session (`$FABLE_SESSION_ID` /
  `$CLAUDE_CODE_SESSION_ID`), not "the next time you type a command."
- This skill only ever calls `fable-dispatch config` — it never mutates `verdict.json`, session
  arm/disarm state, frozen `approved_gate`, or hook behavior. Use `/fablefuse` (the main skill)
  for orchestrator-mode arm (`--gate` freeze) / dispatch / verify doctrine.
- Config changes after arm affect the **config hash** inside the workspace snapshot — a mid-loop
  config flip can make a prior GREEN fail the snapshot match on Stop. Prefer setting executor before
  arm, or re-verify after config changes.
- If the user only wants to *see* the current config with no changes, just run step 1 and stop —
  don't force the question flow.
