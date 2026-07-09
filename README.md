# FableFuse

**Fable 5 (brain/advisor) + a swappable lead/body model, fused into one Claude Code workflow.**

FableFuse pairs a frontier *advisor/planner* model (Claude **Fable 5**) with a swappable
**executor/lead** (**Codex 5.5-high**, **Sonnet 5**, **Opus 4.8**, or **Grok 4.5**) and gives you two ways to run them — the cost-optimal
**advisor** pattern by default, and a hard-gated **orchestrator** loop when you want enforced
separation. It ships as a Claude Code plugin: a skill, a thin dispatch helper, and two hooks.

FableFuse is a companion to [FleetFuse](https://github.com/Renn-Labs/FleetFuse); it copies a couple
of FleetFuse's small helpers so it stands alone (see `NOTICE`).

> Status: early. The offline test suite is green and the CLIs are verified end-to-end, but this is
> young software. Treat model output as advisory and verify it — that's the whole point of the
> deterministic gate below.

---

## Two modes

| Mode | Main loop (runs every turn) | Fable's role | Cost profile |
|-|-|-|
| **advisor** (default) | the **executor/lead** (Codex 5.5-high, Sonnet 5, Opus 4.8, or Grok 4.5) | on-demand consultant via `ask_fable` | most tokens at the lead rate |
| **orchestrator** | **Fable** (in-session brain) | plans, routes, verifies, synthesizes | Fable tokens + bounded body cards |

The advisor pattern is the one Anthropic's ClaudeDevs describe: *an executor calls Fable for
guidance; most tokens are billed at the lower executor rate.*

```
advisor (default)                         orchestrator
  Executor ── main loop, every turn         Fable ── main loop (brain)
     │  ↑ ask_fable(question)                  │  ↓ fable-dispatch "<spec>"
     │  ↓ advice                             Codex/Sonnet/Opus/Grok body ── executes
  Fable ── on-demand advisor                  │  ↑ bounded card + raw artifact
                                            Fable verifies vs raw diff + gate stdout
                                              └─ hard gate blocks direct mutation until GREEN
```

---

## Zero-key quickstart (no model calls, no keys)

```bash
git clone <your-fork-url> FableFuse && cd FableFuse
python3 fable_common.py            # print effective config + built commands
python3 tests/fable_contracts.py   # offline contract suite (should print PASS)
python3 fable_dispatch.py doctor   # readiness table
```

## Install

FableFuse ships as a real Claude Code plugin (`.claude-plugin/plugin.json`) — hooks auto-register,
skills auto-discover, no config-file editing required.

**Inside a Claude Code session:**

```
/plugin marketplace add Renn-Labs/FableFuse
/plugin install fablefuse@fablefuse
```

That's it — `/fablefuse` and `/fablefuse-config` are now available, and the orchestrator-mode hard
gate is registered (still **inert** until you run `fable-dispatch arm`; honours
`FABLE_GUARDS_OFF=1`/`CLAUDE_GUARDS_OFF=1`).

**Upgrade an existing install:**

```
/plugin marketplace add Renn-Labs/FableFuse
/plugin install fablefuse@fablefuse
/reload-plugins
```

After upgrading to `0.2.5`, `fable-dispatch config --executor grok` selects Grok 4.5 as the
lead/body executor. `fable-dispatch config --executor opus` selects Opus 4.8. In both cases Fable
remains the advisor/brain (`FABLE_MODEL`, default `claude-fable-5`).

**Developing/testing locally**, before or without publishing to a marketplace:

```bash
git clone https://github.com/Renn-Labs/FableFuse.git && cd FableFuse
git config core.hooksPath githooks
claude plugin validate .        # manifest schema check
claude --plugin-dir .            # load it for this session only, no marketplace needed
# after editing hooks/skills: /reload-plugins inside the session picks up changes, no restart
```

The tracked pre-push hook runs `scripts/pre-push-check.sh` before public branch pushes. It checks the
version bump, changelog, install docs, CI branch coverage, public scrub candidates, generated-file
hygiene, compile, offline contracts, plugin validation, and an Opus-lead dry-run. Run it manually
with:

```bash
scripts/pre-push-check.sh
```

Before first public exposure, also run the full-history scrub gate:

```bash
scripts/public-release-scrub.py --all-history
```

**Option B — manual install (no marketplace, e.g. hardened/offline environments):**

```bash
python3 fable_dispatch.py install-hooks     # reversible; backs up settings.json
# add bin/ to PATH for the `fable-dispatch` / `ask-fable` shims, or call the .py files directly
```

`install-hooks` merges the two hooks into `~/.claude/settings.json` (respects `$CLAUDE_CONFIG_DIR`)
and writes a `.json.bak`. Remove them any time with `uninstall-hooks`. Note: this path does **not**
register the skills — `/fablefuse`/`/fablefuse-config` still need the plugin install (or a manual
symlink into `~/.claude/skills/`) to actually trigger. `fable-dispatch doctor` reports which install
path (if any) is active.

## Advisor mode (default)

Run your executor/lead as usual; consult Fable only for the hard calls. For a reverse-advisor setup
where **Opus is the lead** and Fable is the specialist advisor, select Opus as the executor:

```bash
ask-fable "Is an outbox pattern overkill here, or the right call?"      # CLI
# or register the on-demand tool with a compatible executor (Codex shown):
codex mcp add fable-advisor -- python3 "$PWD/fable_advisor_mcp.py"      # exposes ask_fable
fable-dispatch config --executor opus                                   # Opus lead + Fable advisor
```

For a Grok Build setup where **Grok 4.5 is the lead** and Fable is the specialist advisor:

```bash
grok --model grok-4.5 "Work normally, and call ask-fable for hard architecture/review calls"
fable-dispatch config --executor grok                                   # Grok lead + Fable advisor
ask-fable "What is the risky part of this plan?"
```

The first command starts an interactive Grok lead session. Dispatched orchestrator body work uses the
`fable-dispatch` command shape described below.

## Orchestrator mode

```bash
fable-dispatch arm
fable-dispatch config --executor codex --effort high        # or --executor sonnet|opus|grok
fable-dispatch "Implement X per spec: files, constraints, non-goals, and the exact test command"
fable-dispatch verify --gate "pytest -q"                    # deterministic: GREEN iff exit 0
# RED → dispatch fixes with concrete failure notes → verify again
fable-dispatch done                                         # only closes on a fresh GREEN
```

## Configure (session or permanent — including mid-flight)

**Mid-session, interactively:** run `/fablefuse-config` any time — asks scope/executor/effort/fast
via a short question flow and applies it immediately (no restart). It only runs when you explicitly
invoke it (`disable-model-invocation: true`), so the brain never reconfigures itself on its own.

**Directly, from the CLI:** precedence is **per-call flag > session config >
`~/.config/fable-fuse/config.json` > env > default.** Persist per-session with `fable-dispatch
config …`, or permanently with `--global`. Either way, a change takes effect on the *next*
`fable-dispatch` call — it does not affect a body that's already running.
`--effort` is a shared speed/quality knob for Codex and Grok persisted config, so switching between
those two executors keeps the same effort class unless you override the engine-specific env/config
key directly.

| toggle | env | default | purpose |
|-|-|-|
| `--executor` | `FABLE_EXECUTOR` | `codex` | body/driver/lead engine: `codex` \| `sonnet` \| `opus` \| `grok` |
| `--model` | `FABLE_CODEX_MODEL` | *(unset)* | pin a specific Codex body model; unset = Codex CLI's own current default |
| `--effort` | `FABLE_CODEX_EFFORT`, `FABLE_GROK_EFFORT` | `high` | Codex/Grok reasoning effort |
| `--fast on\|off` | `FABLE_CODEX_FAST` | `off` | speed preset for Codex/Grok effort → `FABLE_CODEX_FAST_EFFORT` (`low`) |
| `--sonnet-model` | `FABLE_SONNET_MODEL` | `claude-sonnet-5` | model when `executor=sonnet` |
| `--opus-model` | `FABLE_OPUS_MODEL` | `claude-opus-4-8` | model when `executor=opus` |
| `--grok-model` | `FABLE_GROK_MODEL` | `grok-4.5` | model when `executor=grok` |
| — | `FABLE_MODEL` | `claude-fable-5` | Fable advisor/brain model |
| — | `FABLE_CODEX_YOLO` | `1` | let the Codex body run commands/tests (`--yolo`) |
| — | `FABLE_GROK_YOLO` | `1` | let the Grok body use `--permission-mode bypassPermissions`; set `0` to use Grok's default permission flow |
| — | `FABLE_GROK_PERMISSION_MODE` | `bypassPermissions` when `FABLE_GROK_YOLO=1` | explicit Grok CLI permission mode for dispatched body tasks |

Whole-command overrides: `FABLE_BODY_CMD` / `FABLE_EXECUTOR_CMD` (body/lead), `FABLE_CODEX_CMD`,
`FABLE_SONNET_CMD`, `FABLE_OPUS_CMD`, `FABLE_GROK_CMD`, `FABLE_ADVISOR_CMD` (Fable advisor/brain).

## Staying current on model names

OpenAI ships new Codex-capable models every few weeks (gpt-5 → 5.1(-codex) → 5.2(-codex) →
5.3-codex → 5.4 → 5.5, and more since). A hardcoded default version is wrong within weeks — this
project's own first draft shipped an invented `gpt-5.5-codex` default that never existed. So
FableFuse **does not pin a Codex model by default**: `--model`/`FABLE_CODEX_MODEL` is unset unless
you explicitly set it, and `codex exec` then uses whatever model your account's Codex CLI currently
defaults to. Pin a specific release only when you deliberately want to lock a version. To check
what's current: [Codex models](https://developers.openai.com/codex/models) and the
[Codex changelog](https://developers.openai.com/codex/changelog). `fable-dispatch doctor` reports
the exact command that will be run (including any pinned model) without making a live call.

Claude model names are also release-sensitive. Do not infer that a family number exists. The current
Opus executor default is `claude-opus-4-8`, based on Anthropic's official models overview and Opus
4.8 release notes. Check the official Anthropic model docs before changing any Claude default,
README claim, skill text, or marketplace metadata.

xAI model names are checked the same way. The current Grok executor default is `grok-4.5`, based on
xAI's official model docs and Grok Build docs, which describe Grok 4.5 as the model for code,
agentic tasks, and knowledge work. Check official xAI docs before changing Grok defaults or claims.

## How it works (design in one screen)

- **Body invocation** uses the selected executor. Codex follows steipete's proven `codex-first`
  pattern: `codex exec --yolo -c model_reasoning_effort=<e>`, prompt fed on **stdin** (robust for
  large specs). Sonnet/Opus use `claude -p --model <model>`. Grok writes the prompt to a managed
  temp file and runs `grok --model grok-4.5 --reasoning-effort <effort> --permission-mode bypassPermissions --prompt-file <managed-temp-file>`; the temp prompt file is outside the repo and deleted after the run.
- **Deterministic verdict** — the loop can only close on a real external gate. `verify --gate "<cmd>"`
  runs the command, records its **exit code** + a `git diff` sha into `verdict.json`; GREEN iff exit
  0. A prose "looks good" from the brain never closes the loop.
- **Narrowed hard gate** — while armed, the PreToolUse hook blocks the brain's own
  `Write/Edit/MultiEdit/NotebookEdit` and non-allowlisted `Bash` (read-only inspection stays
  allowed), and the Stop hook blocks finishing until a fresh GREEN verdict exists. Tunable
  (`FABLE_BASH_ALLOW`), escapable (`FABLE_GATE_ALLOW_TRIVIAL` for <20-line edits), kill-switchable.
- **Context hygiene** — each body run's raw transcript is written under `runs/`; only a bounded
  summary card returns, so fanning out several bodies never floods the brain's context.

## Honest limits

FableFuse coordinates a body engine and preserves deterministic verification artifacts. It does
**not** guarantee model output is correct, safe, or complete — bodies can fabricate, miss bugs, and
consume provider quota. You are responsible for the review. Live runs need the selected body CLI
(**Codex CLI** for `executor=codex`, **`claude` CLI** for `executor=sonnet|opus`, **Grok Build CLI**
for `executor=grok`) and a **`claude` CLI** with a Fable-capable model for advisor/brain calls.
Offline tests, dry-runs, and the doctor work without live provider calls.

## Credits

- The advisor pattern and the `codex exec --yolo -c model_reasoning_effort=high` (stdin) invocation
  and delegate-vs-keep routing doctrine build on
  [steipete/agent-scripts `codex-first`](https://github.com/steipete/agent-scripts/blob/main/skills/codex-first/SKILL.md).
- `fable_scrub.py` and the artifact/handoff helpers in `fable_common.py` are adapted from
  [FleetFuse](https://github.com/Renn-Labs/FleetFuse) (MIT). See `NOTICE`.

## License

MIT (`LICENSE`).
