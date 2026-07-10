# FableFuse

**Fable 5 (brain/advisor) + a swappable lead/body model, fused into one Claude Code workflow.**

FableFuse pairs a frontier *advisor/planner* model (Claude **Fable 5**) with a swappable
**executor/lead** (**Codex**, **Sonnet 5**, **Opus 4.8**, or **Grok 4.5**) and gives you two
compatibility modes:

| Mode | Who drives | What Fable does |
|-|-|-|
| **advisor** (default) | executor/lead main loop | on-demand consultant via `ask_fable` |
| **orchestrator** | Fable as controller | plans, routes via `fable-dispatch`, verifies, synthesizes |

Advisor mode has **lower coordination overhead** (the executor runs; Fable is called only when you
ask). Actual token use and cost still depend on prompts, retries, and provider pricing — not a
fixed savings claim.

The shipped package is a **Claude Code plugin**. Codex can use the advisor MCP and the same core
CLIs manually; there is **no published Codex or Grok plugin package** yet.

Harness binding matters: a plugin cannot replace the model already driving its host session.
Select the desired host model in Claude Code, Codex, or Grok first; FableFuse can independently
select only models it launches through a managed adapter. Controller-led mode therefore requires
a Fable-capable Claude host today.

FableFuse is a companion to [FleetFuse](https://github.com/Renn-Labs/FleetFuse); it copies a couple
of FleetFuse's small helpers so it stands alone (see `NOTICE`).

> Status: early. The offline contract suite is green and the CLIs are verified end-to-end, but this
> is young software. Treat model output as advisory and verify it — that is the point of the
> deterministic gate below.

---

## Two modes

```
advisor (default)                              orchestrator
  Executor ── main loop, every turn              Fable ── controller (brain)
     │  ↑ ask_fable(question)                       │  ↓ fable-dispatch "<spec>"
     │  ↓ advice                                  Codex/Sonnet/Opus/Grok body
  Fable ── on-demand advisor                        │  ↑ bounded card + raw artifact
                                                  Host-frozen gate → snapshot-bound GREEN
                                                    └─ workflow guardrail (not a hard sandbox)
```

| Concern | advisor | orchestrator |
|-|-|-|
| Main loop | executor/lead | Fable (controller) |
| Coordination | lower (on-demand advice) | higher (dispatch + verify loop) |
| Token/call impact | most work on the lead; Fable only when consulted | Fable turns + body runs + verify |
| Enforcement | none from FableFuse | workflow guardrail while armed |

Numbers depend on your prompts, retry rate, and pricing. Do not treat the table as a benchmark.

---

## Prerequisites

| Need | Why |
|-|-|
| **Python 3.10+** | shipped modules are stdlib-only |
| **Git worktree** | required for a closable controller-led verification loop |
| **Claude Code** | primary install surface (plugin marketplace) |
| **Body CLI for the executor you select** | **Codex CLI** (`codex`), **Claude CLI** (`claude`) for Sonnet/Opus, and/or **Grok Build CLI** (`grok`) for Grok 4.5 |
| **Fable-capable `claude` access** | advisor/brain calls (`FABLE_MODEL`, default `claude-fable-5`) |
| Provider auth for live runs | offline tests/doctor/dry-run need no keys |

---

## Install (Claude Code marketplace)

Inside a Claude Code session:

```
/plugin marketplace add Renn-Labs/FableFuse
/plugin install fablefuse@fablefuse
```

Then **reload or restart** Claude Code so skills and hooks load. After install you get `/fablefuse`
and `/fablefuse-config`. Orchestrator workflow-guardrail hooks register but stay **inert** until
`fable-dispatch arm`; they honour `FABLE_GUARDS_OFF=1` / `CLAUDE_GUARDS_OFF=1`.

### Update

```
/plugin marketplace update fablefuse
/plugin update fablefuse@fablefuse
```

**Restart Claude Code after a plugin update** so hooks and skills pick up the new version.

### Rollback / uninstall

Use Claude Code's plugin UI or uninstall flow for `fablefuse@fablefuse`, then restart. If you used
Option B (manual hooks), run `python3 fable_dispatch.py uninstall-hooks` to remove the merged
settings hooks.

### Local checkout (development)

```bash
git clone https://github.com/Renn-Labs/FableFuse.git && cd FableFuse
git config core.hooksPath githooks
claude plugin validate .        # manifest schema check
claude --plugin-dir .           # load for this session only (no marketplace)
# after editing hooks/skills: /reload-plugins may pick up skill changes;
# full hook updates still need a session restart
```

### Option B — manual hooks (no marketplace)

```bash
python3 fable_dispatch.py install-hooks     # reversible; backs up settings.json
# add bin/ to PATH for the `fable-dispatch` / `ask-fable` shims, or call the .py files directly
```

This merges the two hooks into Claude Code settings and writes a `.json.bak`. It does **not**
register skills — `/fablefuse` / `/fablefuse-config` need the plugin install (or a manual skills
symlink). `fable-dispatch doctor` reports which install path is active.

### Zero-key smoke (no model calls)

```bash
git clone <your-fork-url> FableFuse && cd FableFuse
python3 fable_common.py            # effective config + built commands
python3 tests/run_contracts.py     # aggregate offline suites (PASS)
python3 fable_dispatch.py doctor   # readiness table
```

Tracked pre-push: `scripts/pre-push-check.sh`. Before first public exposure:
`scripts/public-release-scrub.py --all-history`.

---

## Advisor mode (default)

Run your executor/lead as usual; consult Fable for the hard calls.

```bash
ask-fable "Is an outbox pattern overkill here, or the right call?"
# optional: expose ask_fable to a compatible executor (Codex example — not a published plugin)
codex mcp add fable-advisor -- python3 "$PWD/fable_advisor_mcp.py"
fable-dispatch config --executor opus    # Opus lead + Fable advisor
```

Grok 4.5 as lead + Fable advisor:

```bash
fable-dispatch config --executor grok
grok mcp add fable-advisor -- python3 "$PWD/fable_advisor_mcp.py"
grok --model grok-4.5 "Work normally; call ask-fable for hard architecture/review calls"
ask-fable "What is the risky part of this plan?"
```

Start a new Grok session after registering the MCP server so the `ask_fable` tool is available.

---

## Orchestrator mode (workflow guardrail)

The host freezes verification **before** delegation. While armed, the model runs
`fable-dispatch verify` with **no replacement gate**. Closing the loop requires a
**snapshot-bound GREEN**.

```bash
# Host: freeze a single argv command (no shell pipelines or chaining)
fable-dispatch arm --gate "pytest -q"                 # optional: --cwd "$PWD"
fable-dispatch config --executor codex --effort high  # or sonnet|opus|grok

# Controller/model: dispatch body work, then verify the frozen gate only
fable-dispatch "Implement X per spec: files, constraints, non-goals, exact tests"
fable-dispatch verify                                 # uses the arm-time gate; cannot swap it
# RED → dispatch fixes with concrete failure notes → verify again
fable-dispatch done                                   # only on snapshot-bound GREEN
```

**Gate rules (0.2.6):**

- Command is executed as **argv** with `shell=False` (via `shlex.split`).
- Shell pipelines, chaining (`&&` / `;`), and redirection are **not** accepted for the hardened
  close path. Prefer one executable + args: `"pytest -q"`, `"python3 -m unittest"`.
- A closable arm requires the selected `--cwd` (or current directory) to be inside a Git worktree.
  The snapshot covers Git HEAD/index/diffs plus bounded **non-ignored** untracked files; ignored
  build/cache paths are deliberately outside the identity.
- Explicit legacy shell compatibility (`--legacy-shell` / `FABLE_VERIFY_LEGACY_SHELL`) is **unsafe**
  and **cannot** close the hardened Stop hook.
- Kill-switch: `FABLE_GUARDS_OFF=1` / `CLAUDE_GUARDS_OFF=1`. Deliberate host override: `disarm`.

This is a **workflow guardrail**, not a hard OS sandbox: it steers the in-session controller away
from direct mutation while armed; bodies still run under their own CLI permission models.

---

## Configure (session or permanent)

**Mid-session:** `/fablefuse-config` (explicit invoke only). **CLI:** per-call flag > session >
global config > env > default. Persist with `fable-dispatch config …` or `--global`. Changes apply
to the *next* dispatch. `--effort` is shared for Codex and Grok unless you override
engine-specific env/config.

| toggle | env | default | purpose |
|-|-|-|-|
| `--executor` | `FABLE_EXECUTOR` | `codex` | body/lead: `codex` \| `sonnet` \| `opus` \| `grok` |
| `--model` | `FABLE_CODEX_MODEL` | *(unset)* | pin Codex model; unset = Codex CLI account-aware default |
| `--effort` | `FABLE_CODEX_EFFORT`, `FABLE_GROK_EFFORT` | `high` | Codex/Grok reasoning effort |
| `--fast on\|off` | `FABLE_CODEX_FAST` | `off` | maps effort → `FABLE_CODEX_FAST_EFFORT` (`low`) |
| `--sonnet-model` | `FABLE_SONNET_MODEL` | `claude-sonnet-5` | when `executor=sonnet` |
| `--opus-model` | `FABLE_OPUS_MODEL` | `claude-opus-4-8` | when `executor=opus` |
| `--grok-model` | `FABLE_GROK_MODEL` | `grok-4.5` | when `executor=grok` |
| — | `FABLE_MODEL` | `claude-fable-5` | Fable advisor/brain |
| — | `FABLE_CODEX_YOLO` | *unset / off* | **opt-in** Codex `--yolo` autonomy |
| — | `FABLE_GROK_YOLO` | *unset / off* | **opt-in** Grok `bypassPermissions` |
| — | `FABLE_GROK_PERMISSION_MODE` | *unset* | explicit Grok permission mode when set |

Whole-command overrides: `FABLE_BODY_CMD` / `FABLE_EXECUTOR_CMD`, `FABLE_CODEX_CMD`,
`FABLE_SONNET_CMD`, `FABLE_OPUS_CMD`, `FABLE_GROK_CMD`, `FABLE_ADVISOR_CMD`.

### Model / executor examples

```bash
# Codex: account-aware default model, high effort (default effort)
fable-dispatch config --executor codex --effort high

# Optional exact OpenAI preview pin (only if your org has access — see below)
fable-dispatch config --executor codex --model gpt-5.6-terra --effort high

# Sonnet 5 / Opus 4.8 / Grok 4.5 as body
fable-dispatch config --executor sonnet
fable-dispatch config --executor opus
fable-dispatch config --executor grok --grok-model grok-4.5 --effort high

# Explicit autonomy opt-in (only on trusted repos)
export FABLE_CODEX_YOLO=1
export FABLE_GROK_YOLO=1
```

### Codex and Grok harness boundary (0.2.6)

| Engine | Default permissions | Autonomy opt-in |
|-|-|-|
| **Codex** | provider / Codex CLI defaults (no `--yolo`) | `FABLE_CODEX_YOLO=1` → `--yolo` |
| **Grok** | provider / Grok defaults (no `--permission-mode`) | `FABLE_GROK_YOLO=1` → `bypassPermissions`; or set `FABLE_GROK_PERMISSION_MODE` |

Prompt transport: Codex uses **stdin**; Grok uses a managed owner-only **prompt file** deleted after
the run. Provider processes run in their own process group and are terminated as a group on
timeout/interrupt.

**Privacy:** cross-provider prompts leave the local machine for the selected providers. Local
config, state, and run artifacts are written **owner-only**.

---

## Staying current on model names

### Codex (not pinned by default)

FableFuse **does not pin** a Codex model unless you set `--model` / `FABLE_CODEX_MODEL`. The Codex
CLI then uses its **account-aware default**. Effort defaults to **high**.

Check current Codex models: [Codex models](https://developers.openai.com/codex/models) and the
[Codex changelog](https://developers.openai.com/codex/changelog). `fable-dispatch doctor` prints the
exact command that would run without making a live call.

### Optional OpenAI preview IDs (limited access)

As of **2026-07-09**, these exact IDs are **limited preview for approved organizations** through the
API and Codex. They are **not** in ChatGPT. **Do not assume universal access** — only pin them if
your org is approved:

| ID | Role (qualitative) |
|-|-|
| `gpt-5.6-sol` | flagship |
| `gpt-5.6-terra` | balanced |
| `gpt-5.6-luna` | fast / affordable |

Official sources:

- https://help.openai.com/en/articles/20001325-a-preview-of-gpt-5-6-sol-terra-and-luna
- https://openai.com/index/previewing-gpt-5-6-sol/

### Other verified defaults

| Role | Exact ID | Notes |
|-|-|-|
| Sonnet body | `claude-sonnet-5` | Anthropic model docs |
| Opus body | `claude-opus-4-8` | Anthropic model docs / Opus 4.8 notes |
| Grok body | `grok-4.5` | Grok Build local CLI default/available; FableFuse can select it as executor/lead |
| Fable brain | `claude-fable-5` | advisor/orchestrator controller |

Verify Claude IDs against [Anthropic models](https://docs.anthropic.com/en/docs/about-claude/models)
before changing defaults. Verify Grok IDs against official xAI / Grok Build docs.

---

## How it works (design in one screen)

- **Body invocation** — selected executor via `build_body_command`. Codex:
  `codex exec [--yolo] -c model_reasoning_effort=<e> -` (stdin). Sonnet/Opus: `claude -p --model …`.
  Grok: `grok --model grok-4.5 --reasoning-effort <e> [--permission-mode …] --prompt-file …`.
- **Host-frozen, snapshot-bound verdict** — arm freezes argv + cwd; verify runs that gate with
  `shell=False`; GREEN requires exit 0, a supported complete Git snapshot, and a stable post-gate
  workspace snapshot. `done`/Stop bind the receipt back to the arm-time argv + cwd. Prose from the
  brain never closes the loop.
- **Workflow guardrail** — while armed, PreToolUse blocks the controller's direct mutation and
  non-allowlisted Bash; Stop blocks finish without a fresh snapshot-bound GREEN. Tunable allowlist,
  trivial-edit escape, kill-switch.
- **Context hygiene** — raw body output is stored as local artifacts; only a bounded handoff card
  returns to the controller.

---

## Honest limits

FableFuse coordinates a body engine and preserves deterministic verification artifacts. It does
**not** guarantee model output is correct, safe, or complete — bodies can fabricate, miss bugs, and
consume provider quota. You are responsible for review. Live runs need the selected body CLI and a
Fable-capable `claude` for advisor/brain calls. Offline tests, dry-runs, and doctor work without
live provider calls.

The workflow guardrail is **not** a hard sandbox. Bodies run with their own permission models.
Only point executors at repositories you trust.

---

## Credits

- Advisor pattern and Codex-first invocation doctrine build on
  [steipete/agent-scripts `codex-first`](https://github.com/steipete/agent-scripts/blob/main/skills/codex-first/SKILL.md).
- `fable_scrub.py` and artifact/handoff helpers in `fable_common.py` are adapted from
  [FleetFuse](https://github.com/Renn-Labs/FleetFuse) (MIT). See `NOTICE`.

## License

MIT (`LICENSE`).
