# FrontierFuse Agent Guidance

FrontierFuse is a Claude Code plugin that separates a selectable frontier provider/model from a
selectable executor provider/model. Supported providers are Codex, Claude, Grok, and Gemini. Fable
5 remains the recommended Claude frontier default. Keep changes aligned with two profiles (advisor
default and orchestrator), a deterministic snapshot-bound verify gate, a narrowed and
kill-switchable workflow guardrail, and stdlib-only offline-testable setup.

## Architecture (don't drift from this)

- `frontier_common.py` — the shared contract: config toggles + precedence, per-session state, verdict
  schema, command builders (`build_body_command` dispatches on `executor=codex|claude|grok|gemini`),
  artifact/handoff-card helpers, kill-switch, owner-only writes. Everything imports it; don't fork
  its logic.
- `frontier_models.py` - source-backed model catalog plus local provider discovery.
- `frontier_advisor.py` / `frontier_advisor_mcp.py` - advisor mode (`ask_frontier`): executor-led host
  loop; the selected frontier model advises on demand.
- `frontier_dispatch.py` — orchestrator body-caller + control CLI (arm/dispatch/verify/config/doctor/
  update/install-hooks). Uses `build_body_command` so the executor is swappable. **Host freezes** gate
  argv + cwd at `arm --gate "…" [--cwd PATH]`; armed `verify` uses that freeze.
- `frontier_verify.py` — deterministic gate → `verdict.json` (schema v2). GREEN iff gate exit 0 **and**
  workspace snapshot stable/matching; default `shell=False`; legacy shell is unsafe and cannot close.
- `hooks/frontier_gate.py` (PreToolUse) + `hooks/frontier_verify_gate.py` (Stop) — workflow guardrail on
  the Claude hook surface. Inert unless armed; honour `FRONTIER_GUARDS_OFF=1` / `CLAUDE_GUARDS_OFF=1`.
  Not a sandbox: host can kill-switch, disarm, or run outside hooks.
- `skills/frontierfuse-config/SKILL.md` — interactive mid-flight config; must only ever call
  `frontier-dispatch config` (never invent a second config storage path).

## Packaging (primary path — keep in sync with the manual fallback)

- `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` are the primary install surface
  (self-referential marketplace — verified against the real `Renn-Labs/loopprint` pattern, not
  assumed). `hooks/hooks.json` auto-registers the two hooks via `$CLAUDE_PLUGIN_ROOT`-relative
  commands; skills auto-discover from `skills/`.
- `frontier_dispatch.py install-hooks`/`uninstall-hooks` remain as a documented manual fallback
  (Option B) for environments without marketplace access. If you change what the hooks do or where
  they live, update **both** paths — `hooks/hooks.json` and `settings.hooks.snippet.json` — so they
  don't drift apart.
- Bump `version` in `plugin.json`, `marketplace.json`, `frontier_advisor_mcp.py`, and
  `frontier_update.py` together; keep them equal. Update changelog, install/update/doctor docs, and
  skills in the same release.
- Do not claim separate Codex/Grok/Gemini plugin packages ship unless maintainers have published them.

## Invariants

- **stdlib-only, Python 3.10+.** No third-party imports in the shipped modules.
- **The loop closes only on a fresh snapshot-bound GREEN** (verdict stamped after the last
  dispatch, exit 0, stable complete Git snapshot, arm-time argv/cwd binding, not unsafe/legacy).
  A prose verdict must never satisfy the Stop gate.
- **Workflow guardrail is narrowed** — mutation tools + argv-validated Bash policy, not a blanket
  block. Direct body CLI mutation is denied only while armed on the Claude hook surface. Keep the
  trivial-edit escape and kill-switch. Never describe it as sandbox/security isolation.
- **Body invocation** stays robust for large specs: Codex uses stdin (`codex exec … -`), Grok uses
  `--prompt-file`, and all engines stay overridable via `FRONTIER_*_CMD`. Elevated autonomy
  (`FRONTIER_CODEX_YOLO=1`, `FRONTIER_GROK_YOLO=1`) is **opt-in**; default inherits provider permissions.
- **Market model names must be verified against official provider docs before shipping.** Do not
  infer unreleased family names. Exact verified current IDs:
  - Claude: `claude-fable-5`, `claude-sonnet-5`, `claude-opus-4-8`
  - Grok: `grok-4.5`; account-specific IDs come from `grok models`
  - Gemini: `gemini-3.5-flash` default; exact catalog IDs live in `frontier_models.py`
  - Codex executor: deliberately unpinned (empty default -> CLI account-aware model)
  - GPT-5.6 catalog: `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`
  Provider and model are separate; never promote an unverified requested ID into the static catalog.
- **Update checks stay private and non-blocking.** Ordinary doctor is offline. Passive checks run
  only during explicit FrontierFuse use, use an owner-only seven-day cache, stay silent when current
  or offline, and never mutate installations. Keep Claude, Codex, Grok, and Gemini lifecycle docs aligned.

## Verification

Before claiming a change works: `python3 tests/run_contracts.py` and
`python3 tests/run_contracts.py --self-test` must print PASS. Drive the real CLI for anything with
runtime behaviour (dispatch dry-run, arm freeze + verify without replacement args, gate hook with
synthetic JSON, `verify` snapshot stability). Tests must stay keyless/offline (dummy
`FRONTIER_CODEX_CMD`/`FRONTIER_ADVISOR_CMD`).

## Public launch boundary

- Do **not** create the GitHub remote, push, tag a release, publish packages, or make the repo
  public without explicit maintainer approval.
- When maintainer approval **is** given for a public push/tag/release, follow **Public release scrub
  memory** below with no shortcuts. This applies equally if the work is done in Claude Code, Codex,
  Grok, or any other agent.
- Keep claims precise: FrontierFuse coordinates a body engine and preserves deterministic verification
  artifacts. It is not a proven autonomous workforce; do not claim superiority over prior art it
  builds on (steipete/agent-scripts `codex-first`).
- CI stays keyless and offline unless a maintainer explicitly approves a live-provider gate.
- Never commit secrets, provider logs, generated `runs/`, `verdict.json`, or `.grokprint/` traces.

## Public release scrub memory

- This rule is cross-agent project memory. Keep it aligned with `AGENTS.md` and
  `docs/PUBLIC_RELEASE_CHECKLIST.md` so Claude Code, Codex, Grok, and other agents see the same gate.
- Before any public push to GitHub (`origin`), tag, release, marketplace update, or repo-publication
  work:
  1. `git config core.hooksPath githooks` (enables tracked `githooks/pre-push` → `scripts/pre-push-check.sh`)
  2. `scripts/pre-push-check.sh` must pass
  3. `scripts/public-release-scrub.py --all-history` before first public exposure or after history rewrites
- **Hard bans:** do not use `git push --no-verify` for public origin push/tag/release; do not use
  `FRONTIER_SKIP_PRE_PUSH=1` alone; do not use `--maintainer-escape` for public push/tag/release.
  If the gate fails, fix the failure.
- Public pushes must be from `main` or `master`. CI (`.github/workflows/offline.yml`) is a backstop,
  not a substitute for the local gate.
- Do not print matched secret values. Report only file, line, commit scope, and finding type.
- Test fixtures must not contain complete token-shaped literals; build fake values from pieces.
- If a real secret appears in files or history, stop release work, rotate/revoke it, and scrub local
  history before pushing.
