# FableFuse Agent Guidance

FableFuse is a Claude Code plugin that pairs **Fable 5** (brain/advisor) with a swappable **lead/
body/executor**: **Codex** by default (no model version pinned, see README "Staying current on model
names"), **Sonnet 5** / **Opus 4.8** through the Claude CLI, or **Grok 4.5** through Grok Build CLI.
Keep changes aligned with the core
promise: two selectable control flows (advisor default, orchestrator), a **deterministic** verify
gate, a narrowed & kill-switchable hard gate, pluggable executor, and local-first setup — all
stdlib-only and offline-testable.

## Architecture (don't drift from this)

- `fable_common.py` — the shared contract: config toggles + precedence, per-session state, verdict
  schema, command builders (`build_body_command` dispatches on `executor=codex|sonnet|opus|grok`), artifact/handoff-card
  helpers, kill-switch. Everything imports it; don't fork its logic.
- `fable_advisor.py` / `fable_advisor_mcp.py` — advisor mode (`ask_fable`): executor drives, Fable
  advises on-demand.
- `fable_dispatch.py` — orchestrator body-caller + control CLI (arm/dispatch/verify/config/doctor/
  install-hooks). Uses `build_body_command` so the executor is swappable.
- `fable_verify.py` — deterministic gate → `verdict.json` (GREEN iff the gate's exit code is 0).
- `hooks/fable_gate.py` (PreToolUse) + `hooks/fable_verify_gate.py` (Stop) — the hard gate. Inert
  unless armed; honour `FABLE_GUARDS_OFF=1` / `CLAUDE_GUARDS_OFF=1`.
- `skills/fablefuse-config/SKILL.md` — interactive mid-flight config; must only ever call
  `fable-dispatch config` (never invent a second config storage path).

## Packaging (primary path — keep in sync with the manual fallback)

- `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` are the primary install surface
  (self-referential marketplace — verified against the real `Renn-Labs/loopprint` pattern, not
  assumed). `hooks/hooks.json` auto-registers the two hooks via `$CLAUDE_PLUGIN_ROOT`-relative
  commands; skills auto-discover from `skills/`.
- `fable_dispatch.py install-hooks`/`uninstall-hooks` remain as a documented manual fallback
  (Option B) for environments without marketplace access. If you change what the hooks do or where
  they live, update **both** paths — `hooks/hooks.json` and `settings.hooks.snippet.json` — so they
  don't drift apart.
- Bump `version` in `plugin.json` **and** `marketplace.json` together; keep them equal.

## Invariants

- **stdlib-only, Python 3.10+.** No third-party imports in the shipped modules.
- **The loop closes only on a fresh deterministic GREEN** (verdict stamped after the last dispatch).
  A prose verdict must never be able to satisfy the Stop gate.
- **The hard gate is narrowed** — mutation tools + a Bash allowlist, not a blanket block. Keep the
  trivial-edit escape and kill-switch.
- **Body invocation** stays robust for large specs: Codex uses stdin (`codex exec --yolo -c
  model_reasoning_effort=<e> -`), Grok uses `--prompt-file`, and all engines stay overridable via
  `FABLE_*_CMD`.
- **Market model names must be verified against official provider docs before shipping.** Do not
  infer unreleased family names. As of the `0.2.4` correction, Opus means `claude-opus-4-8`; do not
  write an Opus major-version model ID unless official Anthropic docs list it.
  As of `0.2.5`, Grok means `grok-4.5` through the Grok Build CLI; verify xAI model IDs against
  official xAI docs before changing defaults or claims.

## Verification

Before claiming a change works: `python3 tests/fable_contracts.py` must print PASS, and drive the
real CLI for anything with runtime behaviour (dispatch dry-run, gate hook with synthetic JSON,
`verify --gate`). Tests must stay keyless/offline (dummy `FABLE_CODEX_CMD`/`FABLE_ADVISOR_CMD`).

## Public launch boundary

- Do **not** create the GitHub remote, push, tag a release, publish packages, or make the repo
  public without explicit maintainer approval.
- Keep claims precise: FableFuse coordinates a body engine and preserves deterministic verification
  artifacts. It is not a proven autonomous workforce; do not claim superiority over prior art it
  builds on (steipete/agent-scripts `codex-first`).
- CI stays keyless and offline unless a maintainer explicitly approves a live-provider gate.
- Never commit secrets, provider logs, generated `runs/`, or `verdict.json`.

## Public release scrub memory

- This rule is cross-agent project memory. Keep it aligned with `AGENTS.md` and
  `docs/PUBLIC_RELEASE_CHECKLIST.md` so Claude Code, Codex, Grok, and other agents see the same gate.
- Before public push, tag, release, marketplace update, or repo-publication work, run
  `scripts/pre-push-check.sh`; before first public exposure or after history rewrites, also run
  `scripts/public-release-scrub.py --all-history`.
- Do not print matched secret values. Report only file, line, commit scope, and finding type.
- Test fixtures must not contain complete token-shaped literals; build fake values from pieces.
- If a real secret appears in files or history, stop release work, rotate/revoke it, and scrub local
  history before pushing.
