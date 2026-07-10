---
name: frontierfuse
description: >
  FrontierFuse pairs a selectable frontier model with a separate Codex, Claude, Grok, or Gemini
  executor. Supports executor-led advisor mode and frontier-led orchestrator mode with a frozen,
  snapshot-bound verifier. Use on /frontierfuse, "frontierfuse", or "frontier fuse".
---

# FrontierFuse

FrontierFuse separates three choices:

1. **Profile**: who drives the loop.
2. **Frontier provider/model**: who supplies frontier reasoning or advice.
3. **Executor provider/model**: who performs implementation and tool work.

Fable 5 is the recommended Claude frontier model and remains part of the product story, but it is
not hard-wired. GPT-5.6 Sol/Terra/Luna, Claude, Grok, and Gemini models can occupy either supported
role when the relevant CLI and account expose the exact model ID.

The host harness owns the model already driving its conversation. FrontierFuse cannot hot-swap that
host model; it configures managed frontier and executor calls plus the role contract.

Before selecting a profile or arming, run the quiet cached update reminder once:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/frontier_dispatch.py" update --check --passive
```

Use `frontier-dispatch` from `PATH` or the repository when `$CLAUDE_PLUGIN_ROOT` is unavailable.

Lead replies during an active FrontierFuse loop with:

```text
LOOP - frontierfuse - <advisor|orchestrator> - <short goal> - verifier: GREEN|RED|-
```

## Choose The Profile

| Profile | Driver | Frontier role | Executor role | Typical token impact |
|---|---|---|---|---|
| `advisor` (default) | Executor | Consulted only when needed | Plans, edits, and uses tools | Lower frontier usage |
| `orchestrator` | Current host/frontier controller | Plans, routes, reviews, synthesizes | Runs dispatched work bodies | Higher frontier usage; stronger coordination |

```text
advisor:      user -> executor -> frontier advice (as needed) -> executor -> verifier
orchestrator: user -> frontier orchestrator -> executor bodies -> synthesis -> verifier
```

Default to `advisor` unless the user explicitly requests orchestrator behavior or guarded delegation.
Do not combine the profile question with executor selection. `/frontierfuse-config` asks profile,
frontier model, executor provider, and executor model separately.

## Select Models

Providers are `codex`, `claude`, `grok`, and `gemini`. Sonnet and Opus are Claude model choices,
not provider names. List the source-backed catalog and locally discovered models before asking:

```bash
frontier-dispatch models
frontier-dispatch models --provider claude
frontier-dispatch models --provider grok --json
```

The catalog includes current and useful previous releases. A custom exact ID is allowed for
account-specific availability; validate it with the provider CLI instead of guessing.

Apply profile, frontier, and executor choices independently:

```bash
frontier-dispatch config \
  --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor codex --model ""
```

Examples:

```bash
# Sonnet executor with Fable as frontier advisor
frontier-dispatch config --executor claude --model claude-sonnet-5 \
  --frontier-provider claude --frontier-model claude-fable-5

# Opus executor with GPT-5.6 Sol as frontier advisor
frontier-dispatch config --executor claude --model claude-opus-4-8 \
  --frontier-provider codex --frontier-model gpt-5.6-sol

# Grok executor with GPT-5.6 Terra as frontier advisor
frontier-dispatch config --executor grok --model grok-4.5 \
  --frontier-provider codex --frontier-model gpt-5.6-terra

# Gemini executor
frontier-dispatch config --executor gemini --model gemini-3.5-flash
```

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

The current host/frontier controller plans and reviews; the selected executor runs bodies. Claude
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
frontier-dispatch doctor                     # offline readiness
frontier-dispatch doctor --check-updates     # readiness plus explicit release check
frontier-dispatch update --check             # cached release check
```

Configuration precedence: per-call flag > session config >
`~/.config/frontier-fuse/config.json` > environment > built-in defaults. Changes apply to the next
managed call. Re-verify after changing configuration in an armed loop.
