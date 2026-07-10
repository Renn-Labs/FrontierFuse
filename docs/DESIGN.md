# FableFuse architecture (0.2.6)

Current design specification for the shipped Claude Code plugin. This is **not** a build plan
and not the FrontierFuse roadmap (see `docs/FRONTIERFUSE_EXECUTION_PLAN.md` for future work).

## Purpose

FableFuse pairs **Fable 5** (`claude-fable-5`) as brain/advisor with a swappable **body/executor**:

| Role | Default engine / model ID |
|-|-|
| Advisor / brain | Fable — `claude-fable-5` |
| Body (default) | Codex — **no model pin** (Codex CLI account-aware default) |
| Body option | Sonnet — `claude-sonnet-5` |
| Body option | Opus — `claude-opus-4-8` |
| Body option | Grok — `grok-4.5` (Grok Build CLI) |

Product surface today is the **Claude Code plugin** (skill + dispatch CLI + hooks). Separate
Codex/Grok *plugin packages* are not claimed as shipping.

Bindings are explicit. The host harness owns the model already driving its session, and a plugin
cannot replace that host model. FableFuse can select a model independently only for a managed
provider call. Controller-led mode therefore requires a Fable-capable Claude host today.

## Two control flows

### Advisor mode (default) — executor-led host loop

The selected executor/lead owns the session main loop every turn. It consults Fable **on demand**
via `ask_fable` / `ask-fable` for planning, hard decisions, architecture, or independent review.
No arm/disarm; host hooks stay inert. The executor still owns verification (tests/build/lint).

```
Executor (Codex / Sonnet / Opus / Grok / in-session model)
   │  ↑ ask_fable(question)     — on demand only
   │  ↓ advice
Fable 5  — consultant, not the driver
```

### Orchestrator mode — controller-led Fable loop

Fable (in-session controller) owns planning, routing, and synthesis. Mutation and tool-heavy
execution go to the selected body through `fable-dispatch`. Claude Code host hooks act as a
**workflow guardrail** while the session is armed — not as sandbox isolation.

```
Fable (controller) ── plans, delegates, reviews evidence
   ├─ fable-dispatch "spec"            → 1 body
   ├─ fable-dispatch --parallel …      → N bodies (capped)
   ├─ fable-dispatch verify            → frozen gate → snapshot-bound verdict.json
   └─ fable-dispatch done              → only on fresh snapshot-bound GREEN
```

## Module map

| Path | Role |
|-|-|
| `fable_common.py` | Shared contract: config precedence, session state, command builders (`build_body_command` for `codex\|sonnet\|opus\|grok`), artifacts/handoff cards, kill-switch, owner-only writes |
| `fable_advisor.py` / `fable_advisor_mcp.py` | Advisor consult (`ask_fable` / `ask-fable`) |
| `fable_dispatch.py` | Orchestrator CLI: arm / disarm / dispatch / verify / done / config / doctor / install-hooks |
| `fable_verify.py` | Deterministic gate runner → `verdict.json` (schema v2, snapshot-bound) |
| `hooks/fable_gate.py` | PreToolUse workflow guardrail (inert unless armed) |
| `hooks/fable_verify_gate.py` | Stop gate: finish only on fresh snapshot-bound GREEN |
| `hooks/hooks.json` | Plugin auto-registration of the two hooks |
| `skills/fablefuse/` | Operating manual for both modes |
| `skills/fablefuse-config/` | Interactive mid-flight config (calls `fable-dispatch config` only) |
| `fable_scrub.py` | Optional redaction (off by default for first-party bodies) |

**stdlib-only, Python 3.10+.** No third-party imports in shipped modules.

## Frozen verifier (host-approved)

At arm time the **host** freezes the exact acceptance argv and workspace:

```bash
fable-dispatch arm --gate "<single argv command>" [--cwd PATH]
```

- Gate string is parsed with `shlex` into argv; empty/invalid gates are refused.
- Shell operators, redirection, substitution, and newlines are refused on the argv path; use the
  explicitly unsafe legacy-shell compatibility path only when needed.
- A closable arm requires the frozen cwd to be inside a Git worktree.
- State stores `approved_gate`: `{gate, argv, cwd}`.
- While **armed**, `fable-dispatch verify` uses the frozen gate/cwd. Any `--gate`/`--cwd`
  restatement or replacement is refused; the controller must call `verify` without those flags.
- Unarmed (or non-orchestrator) verify still requires an explicit `--gate`.

### Default vs legacy shell

| Mode | How | Close eligibility |
|-|-|-|
| **Default (`gate_mode=argv`)** | `subprocess` with `shell=False` | Can produce hardened GREEN |
| **Legacy shell** | `--legacy-shell` or `FABLE_VERIFY_LEGACY_SHELL=1` (`shell=True`) | Verdict marked `unsafe`; **cannot** close |

## Workspace snapshot (schema)

Each verify captures a versioned snapshot including:

- resolved workspace root (`cwd`)
- git `HEAD`
- index tree (`git write-tree`)
- unstaged and staged diff hashes
- bounded untracked content fingerprints (+ overflow metadata beyond the full-hash cap)
- effective config hash
- gate argv + gate mode + gate identity

**GREEN** requires:

1. gate **exit code 0**
2. **stable** snapshot across pre/post gate (`snapshot_stable`)
3. snapshot-bound schema (`schema_version` ≥ 2)
4. **not** `unsafe` (legacy shell cannot satisfy Stop / `done`)
5. verdict timestamp ≥ `last_dispatch_ts`
6. live recompute still **matches** the recorded verified snapshot (workspace drift fails close)
7. recorded gate argv + cwd still match the host-approved arm record
8. Git worktree snapshot coverage is complete (truncated untracked metadata refuses GREEN)

Legacy (pre-snapshot) verdicts remain readable but never close the loop.

Git-ignored paths are deliberately excluded from snapshot identity so build/cache output does not
invalidate every gate. They are not evidence for a hardened close; keep acceptance-relevant files
tracked or non-ignored.

## Workflow guardrail (Claude Code hooks)

Inert unless `fable-dispatch arm` and kill-switches are off.

**PreToolUse (armed):** denies controller mutation tools and non-allowlisted Bash; denies direct
body CLIs (`codex` / `claude` / `grok` and common wrappers); allows read-only inspection and
the required `fable-dispatch` loop commands. `config` is read-only while armed. Bash policy uses
parsed argv + rejection of shell metacharacters. Trivial-edit escape:
`FABLE_GATE_ALLOW_TRIVIAL=1`.

**Stop (armed):** exit 2 unless `verdict_is_snapshot_fresh_green` holds.

**Host limitations (explicit):** the user/host can kill-switch, disarm, edit hooks/state, or run
outside the hooked surface. This is a session workflow aid, not process sandboxing.

## Permissions & body invocation

- **Default (0.2.6+):** inherit provider permission defaults (no automatic Codex `--yolo`, no
  automatic Grok `bypassPermissions`).
- **Opt-in autonomy:** `FABLE_CODEX_YOLO=1`, `FABLE_GROK_YOLO=1`, optional
  `FABLE_GROK_PERMISSION_MODE`.
- Codex large specs: prompt on **stdin** (`codex exec … -`).
- Grok large specs: managed **`--prompt-file`** (temp file outside the repo, deleted after run).
- All engines overridable via `FABLE_*_CMD`.

## Config

Precedence: per-call flag > session config > `~/.config/fable-fuse/config.json` > env > built-in
defaults.

Key knobs: `executor`, Codex/Grok effort, `fast`, engine-specific model pins, Fable model.
Interactive path: `/fablefuse-config` → only `fable-dispatch config` (no second storage).

## Artifacts & privacy

- Body runs → `runs/fable-<runid>/` raw transcripts + bounded handoff cards to the controller.
- Local config/state/artifacts use owner-only modes where written by FableFuse.
- Cross-provider prompts leave the machine; provider terms/retention apply.
- Never commit `runs/`, `verdict.json`, provider logs, credentials, or private absolute paths.

## Packaging

- Primary: `.claude-plugin/plugin.json` + self-referential `marketplace.json`; hooks via
  `hooks/hooks.json`; skills under `skills/`.
- Fallback: `fable_dispatch.py install-hooks` merges `settings.hooks.snippet.json` into Claude
  Code settings (does not register skills alone).
- Keep `hooks/hooks.json` and the manual snippet aligned when hook behavior changes.
- Version bumps: `plugin.json` and `marketplace.json` versions stay equal.

## Verification (development)

- `python3 tests/run_contracts.py` — aggregate offline/keyless suites; must PASS.
- `python3 tests/run_contracts.py --self-test` — proves the aggregate runner fails on a broken
  discovered suite.
- Drive real CLI for runtime behaviour (arm freeze, verify match, gate hooks with synthetic JSON).
- Dummy `FABLE_CODEX_CMD` / `FABLE_ADVISOR_CMD` for offline tests.

## Public boundaries

- No push/tag/release/public repo without maintainer approval.
- Honest claims: coordinates a body engine and preserves deterministic verification artifacts; not
  a proven autonomous workforce; does not claim superiority over prior art (e.g. steipete
  `codex-first`).
- CI stays keyless unless a maintainer approves a live-provider gate.
- Model IDs must be verified against official provider docs before shipping defaults or claims
  (see `AGENTS.md` Market Model Accuracy).
