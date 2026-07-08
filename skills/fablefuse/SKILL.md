---
name: fablefuse
description: >
  FableFuse brain/body pairing — Codex 5.5-high (BODY/executor) + Fable 5 (BRAIN/advisor).
  Two modes: advisor (default, cost-optimal) and orchestrator (hard-gated dispatch loop).
  Use on /fablefuse, "fablefuse", "fable fuse", "fable brain", or when pairing Fable planning
  with Codex execution.
---

# FableFuse

Pair **Fable 5** (brain) with **Codex 5.5-high** (body). Pick a mode at session start; stay in it unless the user switches.

**Status line — lead every reply:**

```
🔁 LOOP · fablefuse · <advisor|orchestrator> · <goal ≤8 words> · verifier: GREEN|RED|—
```

- `verifier: —` in advisor mode (no gate loop) or before first verify in orchestrator mode.
- `verifier: GREEN|RED` after `fable-dispatch verify` in orchestrator mode.

---

## Mode selection

| Mode | Main loop | Fable role | Codex role | Cost profile |
|------|-----------|------------|------------|--------------|
| **advisor** (default) | Executor (Codex 5.5-high or in-session model) | On-demand consultant | Every turn — plans, tools, edits | Most tokens at cheaper executor rate |
| **orchestrator** | Fable (in-session brain) | Plans, routes, verifies, synthesizes | Dispatched bodies only | Fable tokens + bounded body cards |

**Default to advisor** unless the user says orchestrator, `/fablefuse orchestrator`, or wants hard-gated delegation.

**Executor** (the body/driver) is swappable: `codex` (Codex 5.5-high, default) or `sonnet` (Sonnet 5).
Set per-session or permanently: `fable-dispatch config --executor codex|sonnet [--global]`.

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
paths, constraints ("don't touch X"), non-goals, the **exact proof command** (e.g. `pytest -q`), and
the output shape ("report files changed + test output"). Spec quality decides success.

## Verify — always, and yourself

Body claims are **advisory**. Read the full diff like a reviewer and run the gate yourself
(`fable-dispatch verify --gate …`) — never trust a summary card. After ~2 failed rounds, stop
delegating and do it directly.

---

## Advisor mode (default)

Cost-optimal, Anthropic-blessed pattern: the **executor** runs the main loop every turn. Consult **Fable 5 only when you need** planning, hard decisions, architecture tradeoffs, or independent verification.

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
5. **No `fable-dispatch arm`.** Hard gate stays off; you are not blocked from direct execution.

### Advisor config (optional)

Model/effort for Codex bodies when you spawn them manually:

```bash
fable-dispatch config [--executor codex|sonnet] [--model MODEL] [--effort low|medium|high] [--fast on|off] [--global]
```

Effective defaults: no pinned model (Codex's own current default) @ `high` effort; pin a specific
release with `--model`/`FABLE_CODEX_MODEL` if you want to lock a version. `--fast on` → lower
effort (and optional lighter model).

---

## Orchestrator mode

Fable is the **in-session brain**. Codex is the **sole body** — all execution, research, tool use, and MCP gathering goes through dispatch. A **hard gate** blocks direct mutation while armed.

### Arm (session start)

```bash
fable-dispatch arm
```

After arming: **never** Write/Edit/MultiEdit/NotebookEdit or mutating Bash directly. Read-only inspection (`git diff`, `rg`, `cat`, …) and `fable-dispatch` are allowed. Kill-switch: `FABLE_GUARDS_OFF=1` or `CLAUDE_GUARDS_OFF=1`.

### Delegate everything executable

```bash
# Single coherent body
fable-dispatch "Precise task spec — files, constraints, done criteria"

# Independent chunks (cap by judgment; default max 4)
fable-dispatch --parallel "task A" "task B" "task C"
```

**Routing:** trivial/routine → `--fast on` or `--effort low`; coherent multi-file work → single body @ default high; embarrassingly parallel → `--parallel`.

Each dispatch returns a **bounded handoff card** (~1800 chars) + raw artifact under `runs/fable-<runid>/`. Read the card; open the artifact to verify claims against raw output.

### Verify (deterministic — prose alone never closes the loop)

```bash
fable-dispatch verify --gate "pytest -q"
# or: npm test, make check, ./scripts/lint.sh, etc.
```

Writes `verdict.json`: `GREEN` iff gate exit code == 0, with `diff_sha`, `paths`, `ts`, `after_dispatch_ts`. **You** judge against raw diff + gate stdout, not lossy summaries.

| Verdict | Action |
|---------|--------|
| **RED** | Dispatch fix bodies with concrete failure notes; re-verify |
| **GREEN** | Fresh (`ts >= last_dispatch_ts`); proceed to done |
| stale / missing | Re-run verify after last dispatch |

### Close session

```bash
fable-dispatch done
```

Only after fresh **GREEN**. Disarms guards.

### Orchestrator config

```bash
fable-dispatch config [--executor codex|sonnet] [--model MODEL] [--effort low|medium|high] [--fast on|off] [--global]
fable-dispatch config          # print effective config
```

Precedence: per-call flag > session config > `~/.config/fable-fuse/config.json` > env.

**Mid-flight:** run `/fablefuse-config` for an interactive prompt instead of typing flags — same
underlying command, applies to the *next* dispatch, no restart needed.

---

## Shared reference

| Piece | Role |
|-------|------|
| `fable_common` (`fc`) | Config, state, verdict schema, command builders, artifacts |
| `fable-dispatch` | arm · dispatch · verify · done · config · doctor |
| `ask_fable` / `ask-fable` | Advisor-mode on-demand Fable consult |
| `/fablefuse-config` | Interactive mid-flight config (executor/model/effort/fast) — either mode |
| `runs/fable-*` | Raw body transcripts |
| `~/.config/fable-fuse/state/<session>.json` | armed, last_dispatch_ts, verdict |

**Doctor:** `fable-dispatch doctor` — codex/claude on PATH, plugin manifest, hooks, writable state.

---

## Quick start

**Advisor:** Skip arm. Execute normally; call `ask_fable` / `ask-fable` for hard calls only.

**Orchestrator:**

```bash
fable-dispatch arm
fable-dispatch config --fast off --effort high
fable-dispatch "Implement X per spec; run relevant checks"
fable-dispatch verify --gate "pytest -q"
# RED → fix dispatch → verify again
fable-dispatch done
```

---

## Doctrine

- **Advisor:** executor owns the loop; Fable is a specialist consult, not a co-pilot on every turn.
- **Orchestrator:** brain plans and verifies; body executes; gate enforces separation.
- **Never trust summary cards alone** — verify against raw diff + gate stdout.
- **Route effort by difficulty** — don't burn high-effort fan-out on trivia.
- **One status line per reply** — mode, goal, verifier state visible at a glance.