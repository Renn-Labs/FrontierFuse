# FableFuse — Fable (brain) + Codex 5.5-high (body) · standalone OSS repo

## Context

The user wants a new companion to FleetFuse called **FableFuse** with a fixed two-role shape:
**Fable (Claude Fable 5) is the in-session BRAIN** (plans, decides execution width on the fly,
verifies, synthesizes — never executes); **Codex 5.5-high is the sole BODY** (all execution,
agentic work, research, tool-calling, MCP-gathering) via
`codex exec --model gpt-5.5-codex -c model_reasoning_effort=high`.

Confirmed decisions:
- **Router = free-form in-session** (Fable chooses one vs many Codex bodies per turn; no separate
  orchestrator process). Keep it, but persist a lightweight **verification contract**.
- **Packaging = Claude Code plugin**: `/fablefuse` SKILL + thin `fable-dispatch` helper + hooks.
- **Enforcement = hard gate**, but **narrowed** (see council revisions).
- **Codex default = no pinned model**, `high` effort, with runtime toggles for model / effort / fast.
  (Amended post-build: the original `gpt-5.5-codex` default never existed — it was invented from
  the initial "Codex 5.5 high" framing. Live smoke confirmed it fails; the real, fast-moving
  gpt-5.x/*-codex lineage makes any hardcoded default wrong within weeks. FableFuse now omits
  `--model` unless the user explicitly pins one, so Codex's own current account-aware default is
  used. See README "Staying current on model names".)
- **Repo = its own standalone, SELF-CONTAINED OSS repo** at `~/repos/FableFuse` (copies the small
  FleetFuse helpers in; no runtime dependency on FleetFuse). GitHub publish stays human-gated.

**Council revisions (from `/esat-fleet`: Claude critic + Grok consensus) — baked in:**
1. **Deterministic verifier, not a prose GREEN.** The Stop gate must validate a machine-readable
   `verdict.json` produced by an *external* gate (tests/build/lint/repro) that actually ran and
   passed. Fable verifies against **raw diffs + gate stdout**, not lossy summary cards. Reuse
   FleetFuse's `fleet_verify.py`. (Fixes maker≠checker collapse — the biggest risk.)
2. **Narrow the hard-gate.** Blanket-blocking "heavy Bash" false-positives on `git commit`, test
   runs, `grep`. Block file-mutation tools + a Bash *allowlist*; add a trivial-edit escape hatch,
   kill-switch, per-session arming.
3. **Route by difficulty, not blanket Codex-high** — the model/effort/fast toggles carry this;
   high-effort fan-out on trivial bodies burns budget.
4. Reuse FleetFuse's governance (budget / dry-run / artifact handoff / scrub) by **copying** those
   helpers — do not ship a bespoke helper that lacks them (council's "weaker governance" catch).

## Bootstrap (this session moves to the new repo)

1. `mkdir -p ~/repos/FableFuse` and `git init` there. Do **not** modify the FleetFuse repo.
2. Bring only the FableFuse work over: copy this design into `~/repos/FableFuse/docs/DESIGN.md`;
   write a FableFuse `CLAUDE.md` (agent guidance) and `.gitignore`. All further work uses absolute
   paths under `~/repos/FableFuse` — that folder is the project from here on.
3. Copy the needed FleetFuse helpers into the new repo (self-contained): `fleet_scrub.py`,
   `fleet_verify.py`, plus the ~40-line artifact/summary helpers from `fleet_mcp.py`. Rename/trim
   as `fable_*` where it clarifies ownership; keep MIT `NOTICE` attribution.

## Architecture

```
/fablefuse  (skill primes Fable + arms guards)
Fable (in-session brain) ── plans, decides width, verifies vs raw diffs+stdout, synthesizes
   ├─ fable-dispatch "task"                 → 1 Codex-5.5-high body
   ├─ fable-dispatch --parallel "t1" "t2"…  → N concurrent bodies (budget/dry-run/concurrency caps)
   │     each body → raw transcript artifact; only a bounded summary card returns
   ├─ fable-dispatch verify --gate "pytest -q"   → runs the EXTERNAL gate, writes verdict.json
   │     {result: GREEN iff exit==0, gate, exit_code, diff_sha, paths[], ts, after_dispatch_ts}
   └─ fable-dispatch done                   → disarms

Narrowed hard gate (per-session marker; kill-switch FABLE_GUARDS_OFF=1 / CLAUDE_GUARDS_OFF=1):
   PreToolUse → if armed: block Write/Edit/MultiEdit/NotebookEdit + non-allowlisted mutating Bash
                (allow fable-dispatch/codex + read-only inspectors + git status/diff/log);
                trivial-edit escape hatch; message: "delegate execution to Codex"
   Stop       → if armed: block finish unless verdict.json result==GREEN AND ts >= last_dispatch_ts
                (inclusive: a verdict stamped in the same instant as the last dispatch still
                counts as fresh — verify always runs strictly after dispatch completes in
                practice, so this only matters for coincident timestamps in tests)
```

## Files to create (all under `~/repos/FableFuse/`)

| path | role |
|-|-|
| `README.md`, `LICENSE` (MIT), `CLAUDE.md`, `.gitignore`, `NOTICE` | OSS scaffold + attribution |
| `docs/DESIGN.md` | this design (moved in) |
| `skills/fablefuse/SKILL.md` | brain/body operating manual + triggers (`/fablefuse`, "fablefuse", "fable fuse") |
| `fable_dispatch.py` | body-caller: single + parallel Codex, toggles/config, artifact capture + bounded cards, `arm`/`disarm`/`verify`/`config`/`doctor`/`install-hooks` subcommands. Importable (underscore) for tests. |
| `bin/fable-dispatch` | executable shim → `python3 fable_dispatch.py "$@"` |
| `fable_verify.py` | deterministic gate runner (copied/adapted from `fleet_verify.py`): runs the acceptance command, records exit code + changed-file diff sha → `verdict.json` |
| `fable_scrub.py` | copied `fleet_scrub.py` (optional redaction; Codex is first-party/local → off by default) |
| `hooks/fable_gate.py` | narrowed PreToolUse gate (stdlib) |
| `hooks/fable_verify_gate.py` | Stop gate validating `verdict.json` (stdlib) |
| `settings.hooks.snippet.json` | hook entries to merge into `~/.claude/settings.json` (via reversible `install-hooks`) |
| `tests/fable_contracts.py` | offline contract tests (no live claude/codex) |
| `.github/workflows/offline.yml` | keyless offline CI (mirrors FleetFuse) |

## Key mechanics

**Toggles / config** (precedence: per-call flag > session config > `~/.config/fable-fuse/config.json`
> env default):
- `--model` / `FABLE_CODEX_MODEL` (default `gpt-5.5-codex`)
- `--effort low|medium|high` / `FABLE_CODEX_EFFORT` (default `high`)
- `--fast on|off` / `FABLE_CODEX_FAST` — **body speed preset**: fast=on builds the command with a
  fast profile (`FABLE_CODEX_FAST_EFFORT`, default `low`, + optional `FABLE_CODEX_FAST_MODEL`),
  overriding effort for quick/trivial bodies. The Codex command is *built* from these so a toggle
  changes the next dispatch. Brain-side fast mode (`/fast`) isn't hook-settable — the SKILL surfaces
  a reminder instead. `fable-dispatch config [--model … --effort … --fast …]` prints/persists;
  effective config is echoed in the `🔁 LOOP` status line.

**Dispatch**: single = one Codex body; `--parallel`/`--fanout` = `ThreadPoolExecutor` capped by
`FABLE_MAX_PARALLEL` (default 4), `--dry-run` and `--budget-usd` supported (copied governance). Each
worker → `runs/fable-<runid>/worker-<i>.md` + sha256; returns a bounded card
(`FABLE_MAX_RETURN_CHARS`, default 1800). Codex first-party → no scrub by default.

**Deterministic verify**: `fable-dispatch verify --gate "<cmd>"` runs `<cmd>`, captures exit code +
`git diff` sha of changed paths, writes `verdict.json` (GREEN only iff exit 0). The Stop gate reads
that file — a prose "GREEN" alone can never close the loop.

**Narrowed gate**: block `Write/Edit/MultiEdit/NotebookEdit` when armed; Bash allowlist
(`FABLE_BASH_ALLOW`, default: `fable-dispatch`, `codex`, `git status|diff|log`, `ls`, `cat`, `rg`,
`grep`, `find`, `head`, `tail`, `wc`, `python3 fable_verify`); trivial-edit escape via
`FABLE_GATE_ALLOW_TRIVIAL`. Inert unless armed or when kill-switch set. Session-scoped marker at
`~/.config/fable-fuse/state/<session>.json` (tracks `armed`, `last_dispatch_ts`, `verdict`).

**SKILL doctrine**: arm → you are the brain, never mutate/execute directly, delegate every
execution/research/tool/MCP task to Codex; single for coherent jobs, `--parallel` for independent
chunks; route effort by difficulty; read summary cards, open raw artifacts to verify; run a real
gate (`verify --gate …`) and confirm against raw diff+stdout; loop on RED with fix notes; only then
is GREEN stamped and `done`. Lead replies with the `🔁 LOOP · fablefuse · …` status line.

## Verification

Offline (keyless, CI-safe — `FABLE_CODEX_CMD=echo`-style dummy so no real Codex/Claude call):
- `python3 tests/fable_contracts.py` asserts: Codex command built correctly from model/effort/fast
  toggles + precedence; single vs parallel routing; bounded card shape; scrub-off default;
  **verdict.json determinism** (GREEN only on gate exit 0; staleness vs last_dispatch); **gate
  logic** (armed + Write → block; allowlisted Bash → allow; unarmed → allow; kill-switch → allow;
  Stop blocks without fresh GREEN, allows with it).
- `python3 fable_dispatch.py doctor` — readiness table (codex/claude present, model resolves, hooks
  installed, state dir writable).
- Gate smoke: pipe fake PreToolUse JSON (armed, `tool_name=Write`) into `hooks/fable_gate.py` →
  expect block; unarmed → allow.

Live smoke (user-initiated, one real call): `fable-dispatch "print hello world and stop"` → Codex
body runs, artifact + card land in `runs/`. End-to-end: `/fablefuse` → small goal → gate blocks a
direct Write, Codex does the edit, `verify --gate` passes, Stop unblocks on GREEN.

## Boundaries
- New repo only; FleetFuse repo untouched; no commits pushed / no GitHub remote / not made public
  without explicit human go.
- Guards opt-in (armed only by `/fablefuse`), session-scoped, kill-switchable, reversible installer.
- Honest limits in README: FableFuse coordinates a Codex body and preserves deterministic
  verification artifacts; Codex output correctness is still the user's responsibility; requires a
  logged-in Codex CLI (any current model — no version is pinned by default) and a Fable-capable
  Claude Code session.
