# FrontierFuse

Pair a selectable frontier model with a separate coding executor. FrontierFuse supports Codex,
Claude, Grok, and Gemini providers in executor-led `advisor` and frontier-led `orchestrator`
profiles. Fable 5 is the recommended Claude frontier model, not the only brain.

Current version: **0.3.1**

## Copy This Into Your Coding Harness

Paste the following prompt into Claude Code, Codex, Grok Build, Gemini CLI, or another agentic
coding harness. It instructs the harness to **set FrontierFuse up for you**: run the installation,
configure the models, and verify readiness instead of merely describing the steps.

```text
Install or update FrontierFuse from https://github.com/Renn-Labs/FrontierFuse and configure it for
this coding harness. Work autonomously through detection, installation, verification, and setup.
Do not expose credentials, prompts, private paths, provider transcripts, or local state.

Target result:
- FrontierFuse is installed through the best supported surface for this harness.
- `frontier-dispatch doctor` has been run.
- Profile, frontier provider/model, and executor provider/model are selected as separate decisions.
- The selected provider CLIs exist and the exact model IDs are available to this account.
- Update reminders are configured.

1. Detect the host harness and operating system.

2. Install or update using the applicable path:

   Claude Code native plugin:
   /plugin marketplace add Renn-Labs/FrontierFuse
   /plugin install frontierfuse@frontierfuse

   If already installed:
   /plugin marketplace update frontierfuse
   /plugin update frontierfuse@frontierfuse

   Use /reload-plugins after skill-only changes. Restart Claude Code after installing or updating
   hooks or MCP code. The plugin provides /frontierfuse and /frontierfuse-config.

   Codex or Grok Build shared checkout:
   export FRONTIERFUSE_HOME="$HOME/.local/share/FrontierFuse"
   if [ -d "$FRONTIERFUSE_HOME/.git" ]; then
     (cd "$FRONTIERFUSE_HOME" && git pull --ff-only)
   else
     git clone https://github.com/Renn-Labs/FrontierFuse.git "$FRONTIERFUSE_HOME"
   fi
   export PATH="$FRONTIERFUSE_HOME/bin:$PATH"

   Register only the MCP integrations supported by the current host:
   codex mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
   grok mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"

   Gemini CLI or another harness without a packaged FrontierFuse plugin:
   use the same stable checkout and `frontier-dispatch` / `ask-frontier` CLIs. Do not claim a native
   marketplace plugin exists. Register `frontier_advisor_mcp.py` only if the harness supports stdio
   MCP servers and you can verify its native registration syntax.

3. Run diagnostics without making live model calls:
   frontier-dispatch doctor
   frontier-dispatch doctor --check-updates
   frontier-dispatch update --check

4. Show the current config:
   frontier-dispatch config

5. Explain and ask for the profile as its own decision:

   advisor (default):
   user -> executor -> frontier advice only when needed -> executor -> tests
   Usually lower frontier token use and less coordination overhead.

   orchestrator:
   user -> host/frontier controller -> executor bodies -> synthesis -> frozen verifier
   Usually more frontier tokens/calls, with stronger coordination for complex work.

   The current host model cannot be hot-swapped by a plugin. Profile controls the role contract and
   managed calls.

6. Ask for the frontier provider separately: codex, claude, grok, or gemini. Then run:
   frontier-dispatch models --provider <provider>

   Ask for the frontier model in a separate question. Use the returned source-backed catalog and
   local discoveries. Allow a custom exact model ID only after the provider CLI verifies it. Never
   invent a model ID. Fable, Sonnet, and Opus are Claude models, not provider names.

7. Ask for the executor provider separately: codex, claude, grok, or gemini. Then run:
   frontier-dispatch models --provider <executor>

   Ask for the executor model separately. For Codex, the account-aware CLI default is recommended.
   This checkout currently verifies grok-4.5; do not add grok-4.3 or another requested version to
   the static catalog unless official documentation or `grok models` exposes it.

8. Ask effort (high/medium/low), fast mode (off/on), update reminders
   (passive/manual/off), and scope (session/global). Apply only through:

   frontier-dispatch config \
     --profile <advisor|orchestrator> \
     --frontier-provider <codex|claude|grok|gemini> \
     --frontier-model <exact-model-id> \
     --executor <codex|claude|grok|gemini> \
     --model <exact-model-id-or-empty> \
     --effort <low|medium|high> \
     --fast <on|off> \
     --update-mode <passive|manual|off> \
     [--global]

9. Run `frontier-dispatch config` and `frontier-dispatch doctor` again. Report the effective
   profile, frontier provider/model, executor provider/model, readiness, and any exact missing CLI
   or authentication step. Do not make a live inference call unless I ask.

10. For orchestrator profile, initialize each work loop with a host-approved verifier:
    frontier-dispatch arm --gate "<single test/build/lint argv command>" --cwd "$PWD"

    Dispatch implementation through `frontier-dispatch`, review the raw diff, then run:
    frontier-dispatch verify
    frontier-dispatch done

    `done` is allowed only after fresh snapshot-bound GREEN. The hooks are a workflow guardrail,
    not an OS sandbox. Never enable YOLO/bypass permissions without explicit user direction.
```

## Model Sources

`frontier-dispatch models` combines a maintained catalog with local CLI discovery where supported.
The catalog includes current and useful previous models, including GPT-5.6 Sol/Terra/Luna,
Fable/Sonnet/Opus, Grok 4.5, and Gemini 3.5/3.1/2.5 options.

- [OpenAI models](https://developers.openai.com/api/docs/models/all)
- [Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- Grok account availability: `grok models`

## Operational Contract

| Piece | Purpose |
|-|-|
| `/frontierfuse` | Main Claude Code skill |
| `/frontierfuse-config` | Sequential guided configuration |
| `ask_frontier` / `ask-frontier` | On-demand frontier advice |
| `frontier-dispatch models` | Model catalog and local discovery |
| `frontier-dispatch doctor` | Offline readiness report |
| `frontier-dispatch arm --gate` | Freeze an orchestrator verifier |
| `frontier-dispatch verify` | Run the frozen verifier |
| `frontier-dispatch update --check` | Privacy-preserving cached release check |

Configuration precedence is per-call > session > `~/.config/frontier-fuse/config.json` >
environment > defaults. Passive update checks run at most weekly during explicit FrontierFuse use,
send no machine or project data, and never install automatically.

Provider permissions are inherited by default. Elevated autonomy is explicit only:

```bash
export FRONTIER_CODEX_YOLO=1
export FRONTIER_GROK_YOLO=1
# or FRONTIER_GROK_PERMISSION_MODE=<mode>
```

Cross-provider prompts leave the machine and are subject to provider terms. Never commit `runs/`,
`verdict.json`, provider transcripts, `.omx/`, `.omc/`, credentials, or local state.

## Maintainer Verification

```bash
git config core.hooksPath githooks
python3 tests/run_contracts.py
claude plugin validate .
scripts/pre-push-check.sh
python3 scripts/public-release-scrub.py --all-history
```

The release gate checks synchronized versions, public-data scrub rules, model-name policy, Python
3.10/3.12 contracts, plugin validation, portable shims, provider dry-runs, and doctor output.

MIT licensed. Scrub and handoff helpers are adapted from
[FleetFuse](https://github.com/Renn-Labs/FleetFuse); see `NOTICE`.
