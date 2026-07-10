# FrontierFuse

**Combine a selectable frontier model with a separate coding executor.**

FrontierFuse lets users choose:

1. A **profile**: executor-led `advisor` or frontier-led `orchestrator`.
2. A **frontier provider/model**: Codex/OpenAI, Claude, Grok, or Gemini.
3. An **executor provider/model**: Codex, Claude, Grok, or Gemini.

Fable 5 remains the recommended Claude frontier model and part of the product story, but it is no
longer hard-wired. A user can pair Fable, GPT-5.6 Sol/Terra/Luna, Claude Opus/Sonnet, Grok, and
Gemini models in either supported role when the relevant provider CLI exposes the exact model ID.

FrontierFuse is packaged as a Claude Code plugin. Codex and Grok Build can use the same stdlib CLI
and `ask_frontier` MCP server from a shared checkout. Version: **0.3.0**.

## Profiles

```text
advisor (default)
  user -> executor -> frontier advice (only when needed) -> executor -> tests

orchestrator
  user -> current host/frontier controller -> executor bodies -> synthesis -> frozen verifier
```

| Concern | `advisor` | `orchestrator` |
|-|-|-|
| Driver | selected executor | current host/frontier controller |
| Frontier model | on-demand consultant | planner, router, reviewer, synthesizer |
| Executor | plans, edits, and uses tools | runs dispatched work bodies |
| Typical calls | executor calls plus occasional advice | controller turns plus body calls and verification |
| Typical token impact | lower frontier usage | higher frontier usage for stronger coordination |
| Guardrail | none | Claude Code workflow guardrail while armed |

Actual tokens, latency, and cost depend on prompt size, retries, provider pricing, and model access.
The table is a qualitative guide, not a benchmark.

The host harness owns the model already driving its current conversation. FrontierFuse cannot
replace that running host model; it configures managed frontier calls, managed executor calls, and
their role contract.

## Supported Providers

| Provider key | CLI | Example models |
|-|-|-|
| `codex` | `codex` | account default, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, older GPT models |
| `claude` | `claude` | `claude-fable-5`, `claude-opus-4-8`, `claude-sonnet-5`, previous Claude models |
| `grok` | `grok` | `grok-4.5` plus models discovered from the installed Grok Build CLI |
| `gemini` | `gemini` | `gemini-3.5-flash`, `gemini-3.1-pro-preview`, Gemini 2.5 models |

Sonnet and Opus are Claude **models**, not executor/provider names. Use `--executor claude` and
choose the model separately.

List the maintained catalog and account-aware local discoveries:

```bash
frontier-dispatch models
frontier-dispatch models --provider grok
frontier-dispatch models --provider gemini --json
```

The static catalog uses official provider references. It accepts a custom exact ID for
account-specific availability, but never invents one. This checkout's Grok CLI exposes
`grok-4.5`; an unverified Grok version is not added merely because it was requested.

Sources: [OpenAI models](https://developers.openai.com/api/docs/models/all),
[Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview), and
[Gemini models](https://ai.google.dev/gemini-api/docs/models).

## Prerequisites

| Requirement | Why |
|-|-|
| Python 3.10+ | Runtime and tests are stdlib-only |
| Git worktree | Required for a closable snapshot-bound orchestrator loop |
| Claude Code | Native plugin and slash-command surface |
| Selected provider CLIs | Managed frontier and executor calls |
| Provider authentication | Live inference only; tests, dry-run, and offline doctor are keyless |

## Install

### Claude Code Plugin

Inside Claude Code:

```text
/plugin marketplace add Renn-Labs/FrontierFuse
/plugin install frontierfuse@frontierfuse
```

Restart Claude Code after installation. The plugin provides `/frontierfuse`,
`/frontierfuse-config`, `ask_frontier`, and guarded orchestrator hooks. Hooks remain inert until
`frontier-dispatch arm`.

After skill-only development changes, `/reload-plugins` can refresh the plugin. Restart the session
after hook or MCP changes.

For a pre-0.3 installation, remove the prior plugin entry in Claude Code's plugin manager, install
the new `frontierfuse@frontierfuse` ID, and restart. Existing local run/state artifacts should not be
copied into the new configuration directory.

### Codex And Grok Build

Install one stable checkout and register the advisor MCP only in harnesses you use:

```bash
export FRONTIERFUSE_HOME="$HOME/.local/share/FrontierFuse"
git clone https://github.com/Renn-Labs/FrontierFuse.git "$FRONTIERFUSE_HOME"
export PATH="$FRONTIERFUSE_HOME/bin:$PATH"

codex mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
grok mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"

frontier-dispatch doctor
```

Start a new Codex or Grok session after MCP registration. The host can then call `ask_frontier`;
`frontier-dispatch` remains available for model selection and managed bodies. There is no published
Codex or Grok marketplace plugin in 0.3.0.

### Development Checkout

```bash
git clone https://github.com/Renn-Labs/FrontierFuse.git
cd FrontierFuse
git config core.hooksPath githooks
claude plugin validate .
python3 tests/run_contracts.py
claude --plugin-dir .
```

### Manual Claude Hooks

```bash
python3 frontier_dispatch.py install-hooks
```

This reversibly merges hooks into `~/.claude/settings.json` and creates a backup. It does not
install slash commands; use the plugin for `/frontierfuse` and `/frontierfuse-config`.

## Configure

Use `/frontierfuse-config` for the guided flow. It asks profile, frontier provider/model, executor
provider/model, effort, fast mode, reminders, and scope as separate decisions.

CLI equivalent:

```bash
frontier-dispatch config \
  --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor codex --model "" \
  --effort high --fast off --update-mode passive
```

Examples:

```bash
# Sonnet drives; Fable advises
frontier-dispatch config --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor claude --model claude-sonnet-5

# Opus drives; Fable advises
frontier-dispatch config --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor claude --model claude-opus-4-8

# Grok executes; GPT-5.6 Terra advises
frontier-dispatch config --profile advisor \
  --frontier-provider codex --frontier-model gpt-5.6-terra \
  --executor grok --model grok-4.5

# Gemini executes; GPT-5.6 Sol is the managed frontier model
frontier-dispatch config --profile orchestrator \
  --frontier-provider codex --frontier-model gpt-5.6-sol \
  --executor gemini --model gemini-3.5-flash
```

| Flag | Environment | Default | Purpose |
|-|-|-|-|
| `--profile` | `FRONTIER_PROFILE` | `advisor` | `advisor` or `orchestrator` role contract |
| `--frontier-provider` | `FRONTIER_PROVIDER` | `claude` | managed frontier provider |
| `--frontier-model` | `FRONTIER_MODEL` | `claude-fable-5` | managed frontier model |
| `--executor` | `FRONTIER_EXECUTOR` | `codex` | body provider: Codex, Claude, Grok, or Gemini |
| `--model` | provider-specific model setting | account default for Codex | selected executor model |
| `--claude-model` | `FRONTIER_CLAUDE_MODEL` | `claude-sonnet-5` | explicit Claude executor model |
| `--grok-model` | `FRONTIER_GROK_MODEL` | `grok-4.5` | explicit Grok executor model |
| `--gemini-model` | `FRONTIER_GEMINI_MODEL` | `gemini-3.5-flash` | explicit Gemini executor model |
| `--effort` | `FRONTIER_CODEX_EFFORT`, `FRONTIER_GROK_EFFORT` | `high` | Codex/Grok reasoning effort |
| `--fast` | `FRONTIER_CODEX_FAST` | `off` | use fast effort/model settings |
| `--update-mode` | `FRONTIER_UPDATE_MODE` | `passive` | `passive`, `manual`, or `off` |

Whole-command compatibility overrides: `FRONTIER_BODY_CMD`, `FRONTIER_EXECUTOR_CMD`,
`FRONTIER_ADVISOR_CMD`, `FRONTIER_CODEX_CMD`, `FRONTIER_CLAUDE_CMD`, `FRONTIER_GROK_CMD`, and
`FRONTIER_GEMINI_CMD`.

Precedence: per-call flag > session config > `~/.config/frontier-fuse/config.json` > environment >
built-in defaults. `--global` persists a selection. Changes apply to the next managed call.

## Advisor Usage

Run your selected executor normally and consult the frontier model only for hard decisions:

```bash
ask-frontier "Is an outbox pattern justified here? Include the main tradeoffs and verification risk."
```

The MCP tool is `ask_frontier`. Advice is not proof; the executor remains responsible for tools,
edits, tests, and final verification.

## Orchestrator Usage

Freeze verification before delegation:

```bash
frontier-dispatch arm --gate "pytest -q" --cwd "$PWD"
frontier-dispatch config --profile orchestrator --executor grok --model grok-4.5
frontier-dispatch "Implement X in files A/B; do not touch C; proof: pytest -q"
frontier-dispatch verify
# RED: dispatch a focused fix, then verify again
frontier-dispatch done
```

The verifier runs one argv-style command with `shell=False`. Shell pipelines, chaining,
redirection, and substitutions cannot close the hardened loop. GREEN requires exit code 0, a stable
Git snapshot during the gate, and a receipt matching the frozen argv/cwd and current workspace.

The hooks are a workflow guardrail, not an OS sandbox. Kill switches are
`FRONTIER_GUARDS_OFF=1` and `CLAUDE_GUARDS_OFF=1`; explicit host override is
`frontier-dispatch disarm`.

## Permissions And Privacy

Provider permission defaults are inherited. Elevated autonomy is never enabled automatically:

```bash
export FRONTIER_CODEX_YOLO=1
export FRONTIER_GROK_YOLO=1
# or: export FRONTIER_GROK_PERMISSION_MODE=<mode>
```

Codex and Claude prompts use stdin. Gemini appends stdin to an empty `--prompt` argument. Grok
prompts use an owner-only temporary file that is deleted after use. Provider processes run in their
own process group and are terminated as a group on timeout.

Cross-provider prompts leave the local machine and are subject to provider terms and retention.
Config, state, cached update data, prompts, and raw run artifacts are owner-only. Never commit
`runs/`, `verdict.json`, provider transcripts, `.omx/`, `.omc/`, credentials, or local state.

## Doctor And Updates

```bash
frontier-dispatch doctor                  # offline readiness and cached release status
frontier-dispatch doctor --check-updates  # readiness plus opt-in release check
frontier-dispatch update --check          # cached explicit check
frontier-dispatch update --check --force  # bypass cache and update-mode setting
```

Doctor checks the selected executor CLI, selected frontier CLI, plugin/hooks state, writable state,
and release cache. It does not call live inference or prove model entitlement.

Passive reminders check the public plugin manifest at most every seven days during explicit
FrontierFuse use. They never install automatically and send no machine ID, repository data, prompt,
credential, or usage telemetry. The owner-only cache is
`~/.config/frontier-fuse/update-check.json`.

```bash
frontier-dispatch config --update-mode passive --global
frontier-dispatch config --update-mode manual --global
frontier-dispatch config --update-mode off --global
```

### Update Claude Code

```text
/plugin marketplace update frontierfuse
/plugin update frontierfuse@frontierfuse
```

Restart Claude Code after updating.

### Update Checkout Installs

```bash
cd "$FRONTIERFUSE_HOME"
git pull --ff-only
frontier-dispatch doctor
```

Start a new harness session after updating MCP code.

### Roll Back Or Uninstall

Claude Code: `/plugin uninstall frontierfuse@frontierfuse`, optionally
`/plugin marketplace remove frontierfuse`, then restart. Manual hooks:
`python3 frontier_dispatch.py uninstall-hooks`.

Checkout installs:

```bash
codex mcp remove frontier-advisor
grok mcp remove frontier-advisor
rm -rf "$FRONTIERFUSE_HOME"
```

For rollback, check out a known release tag or commit before restarting the harness.

## Release Safety

Local development uses the tracked pre-push hook:

```bash
git config core.hooksPath githooks
scripts/pre-push-check.sh
python3 scripts/public-release-scrub.py --all-history
```

The release gate checks synchronized metadata, public scrub rules, model-name policy, whitespace,
byte compilation, all offline contracts, plugin validation, provider dry-runs, and doctor output.

## Limits

FrontierFuse does not guarantee model correctness, safety, availability, or cost. Models can miss
bugs, fabricate, and consume quota. Review diffs and run independent deterministic checks. The
workflow guardrail is not isolation; bodies run under their own CLI permissions.

## Credits

The advisor pattern and Codex-first invocation doctrine build on
[steipete/agent-scripts](https://github.com/steipete/agent-scripts). Scrub and handoff helpers are
adapted from [FleetFuse](https://github.com/Renn-Labs/FleetFuse) under MIT; see `NOTICE`.

## License

MIT (`LICENSE`).
