# Changelog

All notable changes to FableFuse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); this project adheres to semantic
versioning once it reaches 1.0.

## [Unreleased]

## [0.2.1] - 2026-07-08

### Added
- **Real Claude Code plugin packaging**: `.claude-plugin/plugin.json` + a self-referential
  `.claude-plugin/marketplace.json` (matching the established `Renn-Labs/loopprint` pattern —
  verified directly against the real installed plugin, not assumed). Hooks now auto-register from
  `hooks/hooks.json` and skills auto-discover from `skills/` — no `settings.json` editing required.
  Install: `/plugin marketplace add Renn-Labs/FableFuse` then `/plugin install fablefuse@fablefuse`.
  Local dev: `claude plugin validate .` / `claude --plugin-dir .`.
- **`/fablefuse-config`**: a new interactive skill for changing executor/model/effort/fast
  mid-session (`disable-model-invocation: true` — only runs on explicit request). Wraps the
  existing, tested `fable-dispatch config` CLI rather than introducing new config storage; applies
  to the next dispatch, no restart needed.
- `fable-dispatch doctor` now reports plugin-manifest presence and which install path (native
  plugin vs. manual `install-hooks`) is actually active.

### Changed
- The original `fable_dispatch.py install-hooks`/`uninstall-hooks` path (merges hooks into
  `~/.claude/settings.json`) is kept as a documented fallback ("Option B") for environments that
  can't use the marketplace/plugin system, but is no longer the primary install path. Note it never
  registered the skills on its own — only the plugin path does that automatically.

### Fixed
- **`plugin.json` declared `"hooks": "./hooks/hooks.json"` explicitly, which broke a REAL install**
  (`claude plugin marketplace add` + `claude plugin install` — `claude plugin validate .` did NOT
  catch this). `hooks/hooks.json` is auto-loaded by convention; declaring it in the manifest too
  caused "Duplicate hooks file detected" and the plugin failed to load. Removed the `hooks` key —
  `manifest.hooks` is only for *additional*, non-standard hook files. Lesson: `claude plugin
  validate` is not a substitute for an actual `marketplace add` + `install` — do both.

Found by live-loading the plugin (`claude --plugin-dir .`) and actually arming a real nested
session — not a synthetic test:
- **The Bash allowlist fought its own brain on the most natural invocations.** A just-armed session
  immediately got blocked trying `python3 fable_dispatch.py --help` (only the bare `fable_dispatch.py`
  prefix matched, not the `python3 <script>` form everyone actually types) and blocked trying an
  inline env-var prefix like `FABLE_GUARDS_OFF=1 python3 ...`. The gate now strips a leading
  interpreter (`python3`/`python`) and leading `VAR=value` assignments before the allowlist
  comparison — the dangerous-metacharacter/chaining check still runs on the untouched original
  string, so this closes a real usability gap without reopening the security fix from `0.1.0`.

## [0.1.0] - 2026-07-07

### Added
- Initial FableFuse: Fable 5 (brain/advisor) + a swappable body/executor (Codex, no model version
  pinned by default, or Sonnet 5).
- **Advisor mode** (default): `fable_advisor.ask_fable`, the `ask-fable` CLI, and `fable_advisor_mcp.py`
  (stdio MCP server exposing `ask_fable`) so an executor main loop consults Fable on-demand.
- **Orchestrator mode**: `fable_dispatch.py` (single + parallel body dispatch, bounded handoff cards,
  raw artifacts, `arm`/`disarm`/`done`/`verify`/`config`/`doctor`/`install-hooks`).
- **Deterministic verify**: `fable_verify.py` runs an external gate and writes `verdict.json`
  (GREEN iff the gate exits 0), with a diff sha and freshness check.
- **Narrowed hard gate**: `hooks/fable_gate.py` (PreToolUse) blocks the brain's direct
  mutation/execution while armed; `hooks/fable_verify_gate.py` (Stop) blocks finishing until a fresh
  GREEN verdict. Tunable allowlist, trivial-edit escape, and `FABLE_GUARDS_OFF` kill-switch.
- Runtime config toggles (`executor`, `codex_model`, `codex_effort`, `fast`, `sonnet_model`, `fable_model`)
  with per-call > session > global > env > default precedence; persist per-session or `--global`.
- `fable_common.py` shared foundation; `fable_scrub.py` (copied from FleetFuse); `/fablefuse` skill;
  keyless offline CI; offline contract suite.

### Fixed
Found during live smoke + code review (native + `peer trio`) before the initial commit:
- **Hard gate never engaged in a real Claude Code session** — `fable-dispatch` defaulted its
  session key to the literal string `"default"`, but the real PreToolUse/Stop hook payload carries
  Claude Code's actual session id. `SESSION_ID` now auto-derives from `$CLAUDE_CODE_SESSION_ID`.
- **Bash allowlist bypass** — prefix matching alone let a chained command through an allowlisted
  prefix (e.g. `git status && rm -rf ...`). The gate now rejects any command containing shell
  metacharacters (`&& ; | \` $( > <`), not just prefix-matching the base command.
- **`fable-dispatch done` unconditionally disarmed** the hard gate regardless of verdict state,
  letting the brain kill the gate on demand (it's itself Bash-allowlisted). `done` now refuses to
  disarm without a fresh GREEN verdict.
- `verdict.json` now chmod 0600 (can contain gate stdout/stderr).
- `FABLE_GATE_ALLOW_TRIVIAL` now uses proper boolean parsing (`"0"`/`"false"` no longer bypass).
- `install-hooks` now warns instead of silently replacing a malformed `settings.json`.
- Parallel dispatch cards now sort by label (previously returned in nondeterministic completion order).

### Changed
- **No Codex model is pinned by default.** The initial draft shipped `gpt-5.5-codex` as the
  default — that model never existed; it was invented from the "Codex 5.5 high" framing rather
  than verified. Live smoke confirmed the failure. Since OpenAI ships new Codex-capable models
  every few weeks, FableFuse now omits `--model` unless explicitly pinned via
  `FABLE_CODEX_MODEL`/`--model`, so Codex's own current account-aware default is used. See README
  "Staying current on model names".

### Notes
- Body invocation follows steipete/agent-scripts `codex-first` (`codex exec --yolo
  -c model_reasoning_effort=high`, prompt on stdin).
