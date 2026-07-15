---
name: frontierfuse
description: >
  FrontierFuse pairs a selectable frontier model with a separate Codex, Claude, Grok, or Gemini
  executor. Supports executor-led advisor mode and host-led orchestrator mode with a frozen,
  snapshot-bound verifier. Use on /frontierfuse, "frontierfuse", or "frontier fuse".
---

# FrontierFuse

FrontierFuse separates independent decisions. Ask them one at a time; never combine profile with
provider/model selection:

1. **Profile / workflow**: who drives the loop.
2. **Frontier provider**: which provider supplies frontier reasoning or advice.
3. **Frontier model**: exact model ID for that provider.
4. **Executor provider**: which provider performs implementation and tool work.
5. **Executor model**: exact model ID (Codex may stay empty for account default).
6. **Effort** (Codex/Grok only), **fast mode**, **update mode**.

**Providers are not models.** Sonnet, Opus, and Fable are Claude models, not providers. Never invent
model IDs; use `frontier-dispatch models` and accept only exact IDs the provider CLI can verify.

Fable 5 is the recommended Claude frontier model and remains part of the product story, but it is
not hard-wired. GPT-5.6 Sol/Terra/Luna, Claude, Grok, and Gemini models can occupy either supported
role when the relevant CLI and account expose the exact model ID.

## Host-bound limitation

The host harness owns the model already driving its conversation. A plugin **cannot hot-swap** that
host model. FrontierFuse configures **managed** frontier consults and executor bodies plus the role
contract. Until a managed controller process exists, orchestrator planning remains host-owned; the
configured frontier is managed consult capacity, not an automatic host replacement. Hooks are a
workflow guardrail, not an OS sandbox.

Before selecting a profile or arming, run the quiet cached update reminder once:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" update --check --passive
```

Use `frontier-dispatch` from `PATH` or the repository when `$CLAUDE_PLUGIN_ROOT` is unavailable.

Lead replies during an active FrontierFuse loop with:

```text
LOOP - frontierfuse - <advisor|orchestrator> - <short goal> - verifier: GREEN|RED|-
```

## Three Practical Working Patterns

### 1. Host / executor-led advisor (default)

```text
user
  -> host executor (plans, edits, tools)
       -> managed frontier advice (only when needed)
  -> host executor continues
  -> tests / review
```

Lowest frontier-token use and coordination cost. No arm/disarm loop.

### 2. Host-led verified orchestration (managed executor bodies)

```text
user
  -> host controller (plans, reviews, synthesizes)
       -> frontier-dispatch arm --gate "…"
       -> frontier-dispatch (managed executor bodies)
       -> host reviews raw handoff / diff
       -> frontier-dispatch verify
       -> frontier-dispatch done   # only after fresh GREEN
```

Higher latency/coordination. Claude Code hooks (marketplace or Option B) guard the armed loop only
on that host surface.

### 3. Premium host + deep frontier advisor + cheaper executor bodies (pattern, not a profile)

```text
user
  -> premium host model (harness-selected; plugin cannot swap it)
       -> managed deep frontier consults (ask_frontier / ask-frontier)
       -> cheaper managed executor bodies (frontier-dispatch)
  -> host integrates evidence
  -> real tests / frozen verify when orchestrating
```

This is not a third `profile` value. Select `advisor` first for occasional managed consults, or
select `orchestrator` when the same pattern also needs guarded body dispatch and frozen verification.
Use it when judgment stays on a strong host model, deep advice is occasional, and bulk coding can use
a cheaper executor.

### Comparison

| Working pattern | Frontier-token use | Latency / coordination | Choose when |
|-|-|-|-|
| Advisor | Low (on-demand consults) | Lowest | Default coding loop |
| Host orchestrator | Medium–high | Higher (arm/dispatch/verify) | Multi-step work needing GREEN receipts |
| Premium host + deep frontier + cheap bodies | Higher if consults are frequent; body cost can stay low | Highest setup care | Hard judgment + cost control on implementation |

| Profile | Driver | Frontier role | Executor role |
|-|-|-|-|
| `advisor` (default) | Host executor | Consulted only when needed | Plans, edits, tools |
| `orchestrator` | Current host controller | Managed consult capacity (not a host swap) | Dispatched work bodies |

Default to `advisor` unless the user explicitly requests orchestrator behavior or guarded
delegation. `/frontierfuse-config` asks profile, frontier provider/model, and executor
provider/model separately.

## Select Models

Providers are `codex`, `claude`, `grok`, and `gemini`. List the source-backed catalog and locally
discovered models before asking:

```bash
frontier-dispatch models
frontier-dispatch models --provider claude
frontier-dispatch models --provider grok --json
```

Catalog membership and local discovery are **availability-oriented suggestions only** — not proof
of authentication, billing, or model entitlement. A custom exact ID is allowed for account-specific
availability; validate it with the provider CLI instead of guessing.

Apply profile, frontier, and executor choices independently:

```bash
frontier-dispatch config \
  --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor codex --executor-model ""
```

Examples:

```bash
# Sonnet executor with Fable as frontier advisor
frontier-dispatch config --executor claude --executor-model claude-sonnet-5 \
  --frontier-provider claude --frontier-model claude-fable-5

# Opus executor with GPT-5.6 Sol as frontier advisor
frontier-dispatch config --executor claude --executor-model claude-opus-4-8 \
  --frontier-provider codex --frontier-model gpt-5.6-sol

# Grok executor with GPT-5.6 Terra as frontier advisor
frontier-dispatch config --executor grok --executor-model grok-4.5 \
  --frontier-provider codex --frontier-model gpt-5.6-terra

# Gemini executor
frontier-dispatch config --executor gemini --executor-model gemini-3.5-flash
```

`--model` remains available as a legacy alias for `--executor-model`. Do not pass both.

## Advisor Profile

The executor owns the loop. Consult the configured frontier model only for ambiguity, architecture,
high-stakes judgment, a stuck implementation, or a pre-ship second opinion.

Preferred MCP tool: `ask_frontier`. CLI fallback:

```bash
ask-frontier "Focused question with only decision-relevant context"
```

Rules:

1. The executor performs tools, edits, and routine research.
2. Keep consultations focused and treat the response as advice, not proof.
3. Run real tests, builds, lint, and security checks in the executor loop.
4. Do not arm the orchestrator guardrail in advisor mode.

## Orchestrator Profile

The current host controller plans and reviews; the selected executor runs managed bodies. Claude
Code hooks provide a workflow guardrail while armed. They are not a sandbox and do not constrain an
unhooked shell or other hosts.

Freeze one host-approved verifier before delegation:

```bash
frontier-dispatch arm --gate "pytest -q" --cwd "$PWD"
```

The gate is parsed as argv and runs with `shell=False`. Shell operators, substitutions, and
redirection are refused. The cwd must be inside a Git worktree. After arming, route mutation through
the dispatcher until the frozen verifier is GREEN or the host explicitly disarms.

Dispatch complete work orders that include goal, repository, files, constraints, non-goals, exact
proof command, and expected report shape:

```bash
frontier-dispatch "Implement X in files A/B; do not touch C; proof: pytest -q"
frontier-dispatch --parallel "independent task A" "independent task B"
```

Each dispatch returns a bounded handoff card and stores owner-only raw output under
`runs/frontier-<run-id>/`. Review the raw diff and evidence; body claims are not verification.

Run the frozen gate without replacement arguments:

```bash
frontier-dispatch verify
```

GREEN requires exit code 0, a stable snapshot during the gate, matching HEAD/index/worktree/config,
and a receipt matching the frozen argv and cwd. A legacy shell verdict cannot close the hardened
loop. RED or snapshot drift requires another fix/verify iteration.

Close only after a fresh matching GREEN:

```bash
frontier-dispatch done
```

Explicit host override: `frontier-dispatch disarm`. Kill switches:
`FRONTIER_GUARDS_OFF=1` or `CLAUDE_GUARDS_OFF=1`.

## Routing

Delegate implementation from a settled spec, mechanical refactors, known-repro fixes, tests, CI,
dependency maintenance, and broad codebase exploration. Keep architecture, API design, naming, UX
judgment, destructive operations, releases, pushes, and review of body output in the controller.

After roughly two failed body rounds, stop repeating the same delegation. Isolate the cause, refine
the spec, or explicitly disarm and repair directly.

## Safe Defaults

Bodies inherit provider permission defaults. Elevated autonomy is opt-in:

| Environment variable | Effect |
|-|-|
| `FRONTIER_CODEX_YOLO=1` | Adds Codex `--yolo` |
| `FRONTIER_GROK_YOLO=1` | Adds Grok `--permission-mode bypassPermissions` |
| `FRONTIER_GROK_PERMISSION_MODE=<mode>` | Selects an explicit Grok permission mode |

Cross-provider prompts leave the machine and are subject to provider retention and terms. Never
commit `runs/`, `verdict.json`, provider transcripts, or local state.

## Operations

```bash
frontier-dispatch config                     # effective settings
frontier-dispatch doctor                     # offline readiness (no network)
frontier-dispatch doctor --json              # typed readiness and recovery actions
frontier-dispatch config --repair --global   # backed-up malformed-config recovery
frontier-dispatch config --repair            # backed-up current-session recovery
frontier-dispatch doctor --check-updates     # readiness plus explicit release check
frontier-dispatch update --check             # cached release check only
```

**Doctor is offline by default** and does not create an update cache. Exit codes:

| Code | Meaning |
|-|-|
| `0` | READY — blocking body + frontier CLIs present; optional hooks/release rows may still show gaps |
| `1` | NOT READY — missing blocking CLI, unusable lock, unwritable state, etc. |
| `2` | CONFIG_INVALID or invalid session id — repair/fix identity before continuing |

CLI presence is not authentication, billing, entitlement, or live compatibility proof.
`doctor --check-updates` is the only doctor network path; `update --check` is the standalone
release-metadata path. Passive reminders use an owner-only seven-day cache, stay silent when current
or offline, and never install automatically.

Exit `0` means only local CLI/state readiness. It does not mean every configuration choice has been
made or that a selected model is authorized.

After MCP or hook install/update, reload/restart the host harness so the new surface loads.

Configuration precedence: per-call flag > session config >
`~/.config/frontier-fuse/config.json` > environment > built-in defaults. Changes apply to the next
managed call. Re-verify after changing configuration in an armed loop.
