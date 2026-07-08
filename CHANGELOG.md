# Changelog

All notable changes to FableFuse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); this project adheres to semantic
versioning once it reaches 1.0.

## [Unreleased]

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
