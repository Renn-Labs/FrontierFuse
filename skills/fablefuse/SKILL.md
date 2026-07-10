---
name: fablefuse
description: >
  FableFuse brain/body pairing — swappable Codex/Sonnet/Opus/Grok lead/body + Fable 5 (BRAIN/advisor).
  Two modes: advisor (default, executor-led) and orchestrator (controller-led with a workflow
  guardrail and frozen snapshot-bound verifier). Use on /fablefuse, "fablefuse", "fable fuse",
  "fable brain", or when pairing Fable planning with Codex/Sonnet/Opus/Grok execution.
---

# FableFuse

Pair **Fable 5** (`claude-fable-5`, brain/advisor) with a swappable **Codex / Sonnet / Opus / Grok**
lead/body. Pick a mode at session start; stay in it unless the user switches.

The host harness owns the model already driving its session; this skill cannot replace it.
FableFuse independently selects only managed body calls. Orchestrator mode therefore requires a
Fable-capable Claude host today.

**Status line — lead every reply:**

```
🔁 LOOP · fablefuse · <advisor|orchestrator> · <goal ≤8 words> · verifier: GREEN|RED|—
```

- `verifier: —` in advisor mode (no gate loop) or before first verify in orchestrator mode.
- `verifier: GREEN|RED` after `fable-dispatch verify` in orchestrator mode (snapshot-bound).

---

## Mode selection

| Mode | Main loop | Fable role | Lead/body role |
|------|-----------|------------|----------------|
| **advisor** (default) | Executor/lead (Codex unpinned default, Sonnet `claude-sonnet-5`, Opus `claude-opus-4-8`, Grok `grok-4.5`, or in-session model) | On-demand consultant | Every turn — plans, tools, edits |
| **orchestrator** | Fable (in-session controller) | Plans, routes, reviews evidence, synthesizes | Dispatched bodies only |

**Default to advisor** unless the user says orchestrator, `/fablefuse orchestrator`, or wants
guardrailed delegation with a frozen host verifier.

**Executor** (lead/body/driver) is swappable: `codex` (default, no model pin), `sonnet`, `opus`, or
`grok`. Set per-session or permanently:

```bash
fable-dispatch config --executor codex|sonnet|opus|grok [--global]
```

Grok body dispatch uses `--prompt-file` for large specs. Elevated body autonomy is **opt-in**
(see Permissions below) — defaults inherit each provider's own permission flow.

---

## Routing doctrine — body vs brain

Delegate to the **body** (work orders — the prompt reads like an assignment):

- implementation from a frozen spec; refactors; mechanical migrations
- bug fixes with a known repro; test writing; coverage fills
- CI fixes, dependency bumps, scripts/tooling
- bulk codebase exploration where raw reading ≫ the answer

Keep in the **brain** (Fable):

- design, API design, architecture, naming, UX judgment
- tasks where writing the spec *is* the work (ambiguity = design)
- tiny edits (~<20 lines, one obvious change) — delegation overhead loses (`FABLE_GATE_ALLOW_TRIVIAL=1`)
- session-tool work (MCP, secrets, browser/computer-use); destructive/irreversible ops, releases, pushes
- **review of body output — never delegated, never skipped**

Heuristic: *prompt reads as a work order → delegate; writing it forces the decisions → it's design, keep it.*

## Prompt contract (every dispatch)

The body starts with **zero** session context. Each dispatch carries: the goal, exact repo + key
paths, constraints ("don't touch X"), non-goals, the **exact proof command** (same argv the host
froze at arm), and the output shape ("report files changed + test output"). Spec quality decides
success.

## Verify — always, and yourself

Body claims are **advisory**. Read the full diff like a reviewer and run the frozen gate yourself
(`fable-dispatch verify` while armed) — never trust a summary card. After ~2 failed rounds, stop
delegating and (after host disarm if needed) do it directly.

---

## Advisor mode (default)

**Executor-led host loop:** the selected executor runs every turn. Consult **Fable 5 only when you
need** planning, hard decisions, architecture tradeoffs, or independent verification.

### When to consult Fable

- Ambiguous requirements or multi-path design
- High-stakes correctness / security / compliance judgment
- Stuck after failed attempts — need a second brain
- Pre-ship review of approach (not a substitute for tests)

### How to consult Fable

**Preferred (MCP):** `ask_fable` tool with a focused question and minimal context.

**CLI fallback:**

```bash
ask-fable "Your focused question — include only decision-relevant context"
```

### Advisor rules

1. **You execute.** Run tools, edit files, research, iterate — do not offload routine work to Fable.
2. **Consult sparingly.** One tight question beats a wall of context; paste summaries, not full transcripts.
3. **Apply Fable's answer.** Treat it as advisory input; you still own execution and verification.
4. **Verify with real gates.** Run tests/build/lint yourself; Fable does not stamp GREEN.
5. **No `fable-dispatch arm`.** Workflow guardrail stays off; you are not blocked from direct execution.

### Advisor config (optional)

```bash
fable-dispatch config [--executor codex|sonnet|opus|grok] \
  [--model MODEL] [--sonnet-model MODEL] [--opus-model MODEL] [--grok-model MODEL] \
  [--effort low|medium|high] [--fast on|off] [--global]
```

Effective defaults: no pinned Codex model (CLI's current default) @ `high` effort; Sonnet/Opus/Grok
use the verified IDs above unless overridden. `--fast on` → lower effort (and optional lighter model).
`--effort` is a shared persisted knob for Codex and Grok.

---

## Orchestrator mode

Fable is the **in-session controller**. The selected executor is the **body**. Host Claude Code
hooks provide a **workflow guardrail** while armed — not a sandbox. Direct controller mutation and
direct body CLI invocation are denied **only while armed on the Claude hook surface**. Read-only
inspection and `fable-dispatch` remain. The user/host can always kill-switch, disarm from an
unhooked shell, alter hooks/state, or run outside the hooked surface.

### Arm (session start) — freeze the verifier

The **host** freezes exact verifier argv and cwd. Prefer arming with a gate:

```bash
fable-dispatch arm --gate "pytest -q"
# optional workspace root:
fable-dispatch arm --gate "pytest -q" --cwd "$PWD"
```

- Single argv-style command (parsed with `shlex`; default verify uses `shell=False`).
- The frozen cwd must be inside a Git worktree for a closable loop. Shell syntax (`&&`, `|`,
  redirection, substitutions) is refused on this argv path.
- After a proper arm: **never** Write/Edit/MultiEdit/NotebookEdit or mutating Bash directly.
- Kill-switch: `FABLE_GUARDS_OFF=1` or `CLAUDE_GUARDS_OFF=1`.

Arm without `--gate` arms the guardrail but **blocks** verify until you disarm and re-arm with a
gate — always arm with `--gate` for a closable loop.

### Delegate everything executable

```bash
# Single coherent body
fable-dispatch "Precise task spec — files, constraints, done criteria, proof command"

# Independent chunks (default max parallel 4)
fable-dispatch --parallel "task A" "task B" "task C"
```

**Routing:** trivial/routine → `--fast on` or `--effort low`; coherent multi-file work → single body
@ default high; embarrassingly parallel → `--parallel`.

Each dispatch returns a **bounded handoff card** (~1800 chars) + raw artifact under
`runs/fable-<runid>/`. Read the card; open the artifact to verify claims against raw output.

### Verify (deterministic — frozen gate; prose never closes)

While armed, call verify **without** replacement args (frozen at arm):

```bash
fable-dispatch verify
```

Writes `verdict.json` (schema v2). **GREEN** requires:

1. gate exit code **0**
2. **stable** workspace snapshot across the gate
3. snapshot still matches on Stop/`done` recompute (HEAD, index tree, staged/unstaged hashes,
   bounded non-ignored untracked fingerprints, config hash, cwd, gate identity)
4. not legacy / not `unsafe` shell
5. receipt argv + cwd still match the host-approved arm record

Default gate path is argv (`shell=False`). Legacy shell (`fable_verify.py --legacy-shell` or
`FABLE_VERIFY_LEGACY_SHELL=1`) is explicit unsafe compatibility and **cannot** satisfy hardened close.

| Verdict | Action |
|---------|--------|
| **RED** | Dispatch fix bodies with concrete failure notes; re-verify |
| **GREEN** | Fresh + snapshot-bound + matching; proceed to done |
| stale / missing / unsafe / drifted | Re-run verify after last dispatch; fix workspace drift |

### Close session

```bash
fable-dispatch done
```

Only after fresh **snapshot-bound GREEN**. Disarms guardrail. Explicit host override:

```bash
fable-dispatch disarm
```

### Orchestrator config

```bash
fable-dispatch config [--executor codex|sonnet|opus|grok] \
  [--model MODEL] [--sonnet-model MODEL] [--opus-model MODEL] [--grok-model MODEL] \
  [--effort low|medium|high] [--fast on|off] [--global]
fable-dispatch config          # print effective config
```

Precedence: per-call flag > session config > `~/.config/fable-fuse/config.json` > env.

**Mid-flight:** `/fablefuse-config` — same underlying command, next dispatch only.

---

## Permissions (safe defaults)

By default (0.2.6+), bodies inherit **provider defaults**. Do **not** assume YOLO/bypass:

| Env (host opt-in) | Effect |
|-|-|
| `FABLE_CODEX_YOLO=1` | Codex `--yolo` |
| `FABLE_GROK_YOLO=1` | Grok `--permission-mode bypassPermissions` |
| `FABLE_GROK_PERMISSION_MODE=<mode>` | explicit Grok permission mode |

Cross-provider prompts leave the local machine; provider terms/retention apply. Local state,
`runs/`, and `verdict.json` are owner-only artifacts — do not commit them.

---

## Shared reference

| Piece | Role |
|-------|------|
| `fable_common` (`fc`) | Config, state, verdict schema, command builders, artifacts |
| `fable-dispatch` | arm · dispatch · verify · done · config · doctor |
| `ask_fable` / `ask-fable` | Advisor-mode on-demand Fable consult |
| `/fablefuse-config` | Interactive mid-flight config — either mode |
| `runs/fable-*` | Raw body transcripts (local, owner-only; do not commit) |
| Session state | armed, approved_gate, last_dispatch_ts, verdict |

**Doctor:** `fable-dispatch doctor` — codex/claude/grok readiness, plugin manifest, hooks, writable state.

---

## Quick start

**Advisor:** Skip arm. Execute normally; call `ask_fable` / `ask-fable` for hard calls only.

**Orchestrator:**

```bash
fable-dispatch arm --gate "pytest -q"
fable-dispatch config --fast off --effort high
fable-dispatch config --executor opus     # optional: Opus body
fable-dispatch config --executor grok     # optional: Grok 4.5 body
fable-dispatch "Implement X per spec; proof: pytest -q"
fable-dispatch verify
# RED → fix dispatch → verify again
fable-dispatch done
```

---

## Doctrine

- **Advisor:** executor owns the loop; Fable is a specialist consult, not a co-pilot on every turn.
- **Orchestrator:** controller plans and reviews; body executes; host hooks guard the workflow;
  host freezes the verifier at arm time.
- **Never trust summary cards alone** — verify against raw diff + gate stdout + snapshot match.
- **Route effort by difficulty** — don't burn high-effort fan-out on trivia.
- **One status line per reply** — mode, goal, verifier state visible at a glance.
- **Harness limits:** hooks are a workflow guardrail, not isolation. Never describe them as a sandbox.
