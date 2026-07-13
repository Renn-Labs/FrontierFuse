---
name: frontierfuse-config
description: >
  Interactively configure FrontierFuse profile, frontier provider/model, executor provider/model,
  effort, fast mode, and update reminders. Triggers on /frontierfuse-config, "configure
  frontierfuse", "change frontierfuse model", and "frontierfuse settings".
disable-model-invocation: true
---

# FrontierFuse Config

Configure FrontierFuse without restarting the host. Profile, frontier model, and executor are
separate decisions. Ask each decision as its own step; never combine orchestrator and executor in
one question.

## Steps

1. Show the current effective configuration:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" config
   ```

   If `$CLAUDE_PLUGIN_ROOT` is unavailable, resolve `frontier-dispatch` from `PATH` or the repository.
   If configuration is invalid, do not overwrite it directly. Run `frontier-dispatch doctor --json`
   and follow that check's exact `next_step`: session recovery uses `frontier-dispatch config
   --repair`, while global recovery adds `--global`. Either command creates an owner-only timestamped
   backup before resetting the malformed document. Reapply valid selections from the backup.

2. Ask **scope**: this session (default) or global. Global maps to `--global`.

3. Ask **profile**:

   - `advisor` (default): the executor drives the task and consults the frontier model on demand.
     This usually uses fewer frontier tokens and keeps tool execution in the selected executor.
   - `orchestrator`: the current host model plans, delegates every body of work, evaluates handoffs,
     and closes only after verification. This usually uses more frontier tokens and can improve
     coordination on complex work.

   Explain the flow visually:

   ```text
   advisor:      user -> executor -> frontier advice (when needed) -> executor -> verifier
   orchestrator: user -> frontier orchestrator -> executor bodies -> frontier synthesis -> verifier
   ```

   In a host-bound session, selecting `orchestrator` does not replace the model already running the
   host conversation. It configures FrontierFuse's role contract and managed frontier calls.

4. Ask **frontier provider**: Codex/OpenAI, Claude, Grok, or Gemini. This is the model used for
   managed frontier advice. Then run the catalog for that provider:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" models --provider <provider>
   ```

   Ask **frontier model** in a separate question using the returned list. Always include a custom
   model ID option for account-specific availability. Do not invent model IDs.

5. Ask **executor provider** separately: `codex`, `claude`, `grok`, or `gemini`. The executor performs
   implementation, tool use, and dispatched bodies. Then run `models --provider <executor>` and ask
   **executor model** in a separate question. For Codex, `<account default>` is the recommended
   account-aware choice. Sonnet and Opus are Claude models, not executor/provider names.

   `models` combines the source-backed catalog with local CLI discovery where supported. If a user
   asks for an unlisted model such as an account-specific Grok release, accept the exact ID only
   after the relevant provider CLI confirms it.

6. Ask the remaining controls:

   - Effort for Codex/Grok only: `high` (default), `medium`, or `low`; Codex also supports
     `xhigh`. Omit `--effort` for Claude/Gemini because their executor commands do not expose it.
   - Fast mode: `off` (default) or `on`.
   - Update reminders: `passive` (cached weekly during explicit use), `manual`, or `off`.

7. Apply all selections through the tested configuration command. Do not edit configuration files:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" config \
     --profile <advisor|orchestrator> \
     --frontier-provider <codex|claude|grok|gemini> \
     --frontier-model <model-id> \
     --executor <codex|claude|grok|gemini> \
     --model <model-id-or-empty> \
     [--effort <low|medium|high|xhigh>] --fast <on|off> \
     --update-mode <passive|manual|off> [--global]
   ```

8. Print the effective configuration again and state: "Applied. It takes effect on the next
   `frontier-dispatch` call; it does not change a body already running."

   If the user wants Codex fast mode to follow the regular Codex model again, run a separate reset
   without `--model`:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" config --inherit-fast-model [--global]
   ```

## Permission Defaults

Configuration never enables elevated permissions. These remain explicit host environment opt-ins:

| Environment variable | Effect |
|-|-|
| `FRONTIER_CODEX_YOLO=1` | Adds Codex `--yolo` |
| `FRONTIER_GROK_YOLO=1` | Adds Grok `--permission-mode bypassPermissions` |
| `FRONTIER_GROK_PERMISSION_MODE=<mode>` | Selects an explicit Grok permission mode |

## Notes

- Precedence: per-call flag > session config > `~/.config/frontier-fuse/config.json` > environment >
  built-in default.
- `xhigh` effort is valid for Codex/fast lanes; Grok effort remains low, medium, or high.
- `--inherit-fast-model` clears only the Codex fast-model override; it cannot be combined with
  `--model` in the same command.
- Config changes do not alter verdicts, arm/disarm state, the frozen gate, or hook behavior.
- Explicit repair preserves the malformed original in a timestamped owner-only backup.
- A config change after arming changes the snapshot hash. Re-run verification before closing.
- If the user only asks to view settings, print the effective config and stop.
