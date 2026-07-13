# Changelog

All notable changes to FrontierFuse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); this project adheres to semantic
versioning once it reaches 1.0.

## [Unreleased]

No unreleased changes.

## [0.3.3] - 2026-07-12

### Fixed
- Verification contracts use PATH-resolved `true` and `false` executables instead of Linux-specific
  `/bin` paths, keeping the Python 3.10/3.12 matrix portable across Ubuntu and macOS runners.

## [0.3.2] - 2026-07-12

### Added
- Typed `frontier-dispatch doctor --json` results with an actionable next step for missing CLIs,
  malformed configuration, inactive hooks, unwritable state, and unknown release status. The
  read-only JSON diagnostic remains allowed by the armed command policy.
- Explicit `frontier-dispatch config --repair --global` recovery. FrontierFuse preserves the exact
  malformed file in an owner-only timestamped backup before writing a minimal current-schema config.
- Equivalent backed-up session-state recovery through `frontier-dispatch config --repair`; armed
  hooks deny safely instead of treating malformed state as unarmed.
- Schema versions for global configuration, session state, and handoff cards.
- Linux and macOS CI coverage across Python 3.10 and 3.12.

### Security
- Accepted verification receipts are now bound to the exact host session; every session ID uses a
  collision-resistant digest filename that remains distinct on case-insensitive filesystems. Legacy receipt ownership survives dispatch,
  disarm, and malformed-state repair when its workspace path is not yet known.
- Doctor, configuration, state, and hook-settings JSON reads now reject special files without
  blocking and enforce a bounded regular-file read before decoding.
- Legacy noncanonical session-state paths fail closed until explicit repair, oversized malformed
  JSON is preserved by atomic rename before reset, and authoritative GREEN state stores only the
  snapshot identity fields required for live revalidation.
- Manual hook detection follows Claude Code's matcher contract: omitted PreToolUse matchers cover
  every tool, while Stop matchers are ignored by the host and therefore always cover the event.
- Offline update-cache reads now use the same bounded regular-file probe as doctor/config state, and
  malformed session identifiers are rejected consistently before state-path hashing.
- Global configuration now fails closed when JSON is corrupt, has the wrong top-level type, uses an
  unsupported schema, or contains invalid provider/profile/model/effort/update values. The file is
  never silently overwritten by a later config write.
- Global config and session-state read/modify/write cycles are serialized with owner-only advisory
  lock files, retaining atomic owner-only replacement for the final write. Verification and close
  decisions hold the global lock through their final configuration freshness check.
- Active executor runs are tracked in session state, and `done` now uses a compare-and-set disarm
  transition over a monotonic whole-state revision so any concurrent session mutation prevents a
  stale close. Dispatch identities include a UUID so concurrent calls in one long-lived process
  cannot collapse into one active-work marker.
- A successful Stop-hook check validates GREEN without consuming it, because another host hook may
  still block termination. It atomically fences queued dispatch and verification work; a subsequent
  PreTool event proves that termination was blocked, reopens the session, advances its generation,
  and invalidates GREEN. The explicit `done` command consumes GREEN and closes the generation.
- Persisted JSON rejects non-standard numeric constants and non-finite timestamps, preventing
  `NaN` from bypassing freshness comparisons. Explicit host-side disarm clears orphan run markers.
- Verdicts are bound to a monotonic dispatch generation, and dispatch start clears the prior
  verdict, so wall-clock rollback cannot validate an older GREEN against newer work. Verdict
  persistence also uses the state revision, so a dispatch racing final verification forces RED.
- Hook payloads now fail closed when JSON is malformed, not an object, or invalid UTF-8; invalid
  UTF-8 persisted state is classified as corrupt and routed through explicit backed-up repair.
- Verification now freezes its state-revision baseline before snapshot capture, so any session
  mutation through the final-snapshot/persistence window forces RED.
- Owner-only verdict writes preserve the checkout directory's existing permissions. State reads
  recursively reject non-finite or non-serializable retained values so explicit repair cannot be
  bypassed by a poisoned nested verdict or gate payload.
- Each verification attempt invalidates its predecessor before running, preventing an older GREEN
  from surviving a newer failed verification. Active verification IDs prevent overlapping gates
  from publishing GREEN, and Stop revalidates session revision after its live workspace snapshot so
  a concurrent dispatch cannot close against cached state.
- Verification start clears session authority before removing the shared receipt; an artifact
  cleanup failure therefore cannot leave an older GREEN authoritative.
- The shared `verdict.json` receipt is cleared when verification or dispatch starts and rewritten
  only while the same verification ID remains authoritative in session state. Final publication
  rechecks dispatch generation and active work under lock; artifact I/O failure clears session
  authority. The current receipt path remains tracked independently of armed-gate metadata, so an
  unarmed verification receipt is also invalidated by later executor work; cleanup deletes only a
  structurally recognized FrontierFuse receipt (including a state-matched legacy v2 receipt) and
  never revisits historical workspaces. Verification refuses to replace a pre-existing unrelated
  file, symlink, directory, gate-created file, or another session's receipt at the receipt path.
  Receipts carry session ownership. Persisted receipt and approved-workspace paths must be normalized
  absolute filesystem paths before cleanup can use them. Failed cleanup retains compact
  non-authoritative receipt identity for a later retry without restoring GREEN authority. Receipt
  publication uses atomic create-if-absent semantics, and matching unarmed 0.3.1 receipts upgrade
  without manual cleanup. Re-arm retains receipt ownership, disarm retains retry metadata until
  cleanup succeeds, and cleanup atomically quarantines the pathname before validating and deleting
  the owned inode. Deferred cleanup keeps its non-authoritative retry identity. Emergency guard kill
  switches are honored before hook payload parsing.
- Receipt inspection is nonblocking and size-bounded, so FIFOs and oversized path occupants cannot
  wedge the session lock. Verification persists pending cleanup identity before publication, and
  session repair preserves safely recoverable receipt ownership metadata. FrontierFuse compacts
  diagnostic-heavy generated receipts using the exact publication serializer while preserving result
  and cleanup identity. Repair also recovers safe 0.3.1 approved-workspace receipt paths and discards
  non-finite recovery metadata. Cleanup rejects non-regular receipt occupants before quarantine, so
  a pre-existing `verdict.json/` directory is never displaced.

### Changed
- The copy/paste setup prompt now tells coding harnesses how to diagnose and repair malformed
  configuration without discarding the original.
- Doctor JSON identifies blocking versus optional checks, and dry-run handoff cards now carry the
  same schema version as live cards.
- Offline doctor now reports CLI presence without claiming authentication or model entitlement.
  Non-finite update timeout values fall back to a bounded finite default, and state readiness probes
  the actual per-session and global-configuration advisory-lock paths.
- Doctor distinguishes malformed command overrides (`command_invalid`) from absent executables
  (`cli_missing`) and provides the corresponding recovery action. Doctor redacts command arguments
  from text and JSON output and validates that installed hook matchers cover mutation and Stop events.
- Armed `doctor --json` remains filesystem-read-only: readiness checks inspect existing paths and
  nearest writable parents without creating lock files or changing directory permissions. Probes
  honor `CLAUDE_CONFIG_DIR`, follow valid writable symlinked ancestors, and reject dangling symlink
  components.
- Effort validation is provider-specific: Codex and fast lanes preserve valid `xhigh` settings,
  while Grok remains constrained to low, medium, or high, including when fast mode is enabled.
  Dispatch/config `--effort` now targets the active fast preset when fast mode is selected. Codex
  fast mode uses `fast_model`, where null inherits the regular pin and an explicit empty value uses
  Codex's account-aware default; `config --inherit-fast-model` restores inheritance after an
  explicit fast-model selection. Other executors retain their provider-specific model field. Claude
  and Gemini reject unsupported `--effort` flags instead of silently ignoring them, and setup
  guidance omits that flag for those providers.
- Setup guidance now covers persistent PATH configuration, last-known-good checkout rollback,
  uninstall, and the requirement to re-arm/reverify after session-state repair.
- Manual Claude hook install/uninstall refuses malformed or unreadable settings instead of
  replacing them through a soft JSON fallback; install preserves a backup for explicit recovery.
  Doctor parses both expected hook event structures and reports malformed settings as a probe
  failure rather than trusting a filename substring.
- Config writes lock both global and session layers in a consistent order, validate the prospective
  effective configuration, and persist within the same transaction. Doctor distinguishes permission
  recovery for unreadable files from backed-up JSON repair, and gives a concrete healing command for
  cross-layer Grok fast-effort conflicts whose individual files remain structurally valid.
- Monotonic dispatch generations, rather than wall-clock ordering, determine verdict freshness; a
  backward system-clock adjustment cannot strand a valid same-generation GREEN.
- PreTool hooks remain inert for valid unarmed sessions and acquire the session mutation lock only
  when armed or when reopening a blocked Stop attempt; corrupt persisted state still fails closed.
- The pre-push doctor smoke uses isolated temporary configuration and state, so a maintainer's local
  sessions cannot make an otherwise clean public-release gate fail or pass.
## [0.3.1] - 2026-07-09

### Changed
- Updated GitHub Actions to the current Node 24-based `actions/checkout@v7` and
  `actions/setup-python@v6` releases so public CI runs without the Node 20 deprecation annotation.
- Corrected the public GitHub About description, homepage, and discovery topics to describe
  selectable frontier and executor providers instead of Fable as the only brain.

## [0.3.0] - 2026-07-09

### Added
- Rebranded the product, plugin ID, slash commands, CLIs, MCP tool, hooks, config/state paths, and
  documentation as FrontierFuse. The public slash commands are now `/frontierfuse` and
  `/frontierfuse-config`.
- Independent `profile`, `frontier_provider`/`frontier_model`, and executor provider/model settings.
  The guided walkthrough now asks these decisions separately and explains advisor versus
  orchestrator flow and token impact.
- Codex, Claude, Grok, and Gemini adapters for both managed frontier calls and executor bodies.
  Sonnet and Opus are now selected as Claude models rather than treated as executor types.
- Source-backed `frontier-dispatch models` catalog with current and previous OpenAI, Anthropic, and
  Gemini IDs, local Grok CLI discovery, JSON output, and account-specific custom model support.
- Gemini executor support with `gemini-3.5-flash` as the default Gemini model.
- `frontier-dispatch update --check` with owner-only seven-day caching, explicit `--force`, JSON output,
  and `passive`, `manual`, or `off` reminder modes.
- Offline-first doctor release reporting. `doctor` reads cache only; `doctor --check-updates` is the
  explicit network path and update availability never changes readiness exit status.
- Complete Claude Code, Codex, and Grok Build install, upgrade, rollback, uninstall, MCP
  registration, restart, and zero-key diagnostic guidance.
- Offline contracts for version comparison, cache freshness and permissions, opt-out behavior,
  network failure, passive silence, and doctor's no-network default.

### Privacy
- Passive reminders run only during explicit FrontierFuse use, at most weekly, and stay silent when
  current or offline. They send no machine identifier, repository data, prompts, or usage telemetry
  and never install updates automatically.

### Changed
- Fable 5 remains the recommended Claude frontier model and marketing anchor, but is no longer a
  fixed advisor implementation. Exact GPT-5.6, Claude, Grok, and Gemini models can fill either role
  when supported by the selected provider CLI.
- Doctor now validates both the selected executor CLI and selected frontier provider CLI.

## [0.2.6] - 2026-07-09

### Security
- **Host-frozen verification.** The host freezes the acceptance command at arm time with
  `frontier-dispatch arm --gate "<single argv command>" [--cwd PATH]`. While armed, `verify` runs that
  frozen command only — the model cannot substitute a different gate. `done` requires a
  **snapshot-bound GREEN** that still matches the workspace and the arm-time argv/cwd.
- **Argv-only gate execution.** The gate runs as argv with `shell=False`. Shell pipelines, chaining,
  and redirection are not accepted as the closing path. An explicit `--legacy-shell` /
  `FRONTIER_VERIFY_LEGACY_SHELL` compatibility path remains for tooling that needs it, but it is
  marked **unsafe** and **cannot** close the hardened Stop hook.
- **Complete Git evidence for closure.** A closable arm now requires a Git worktree. Non-Git and
  truncated untracked snapshots cannot produce GREEN, preventing a vacuous workspace receipt.
- **Armed Bash policy hardened.** The PreToolUse allowlist uses parsed argv (not prefix matching
  alone), blocks direct provider CLIs and `disarm`/`arm`/hook-install paths from the model, and
  blocks `git -c` / external-diff style gate bypasses. Snapshot fingerprints for untracked files
  are hardened beyond a content-hash cap.
- **Safer default body permissions.** Codex and Grok now inherit **provider defaults**. Autonomous
  elevation is opt-in: `FRONTIER_CODEX_YOLO=1` (Codex `--yolo`) and `FRONTIER_GROK_YOLO=1` (Grok
  `--permission-mode bypassPermissions`). `FRONTIER_GROK_PERMISSION_MODE` still sets an explicit mode
  when needed.
- **Owner-only local artifacts.** Config, state, prompt files, run dirs, response artifacts, and
  handoff cards are written owner-only (`0600` files / `0700` directories). Cross-provider prompts
  still leave the machine for the selected providers; local artifacts/state remain owner-only.

### Added
- Snapshot-bound verdict schema (workspace HEAD/index/diff/untracked + gate identity) so GREEN is
  stale when the tree moves after verify.
- Aggregate offline runner `tests/run_contracts.py` — discovers and runs every
  `tests/*_contracts.py` suite (including gate security, safe-execution, and verification-snapshot
  contracts). CI and pre-push use this aggregate entry point.
- Standalone contract suites for the armed Bash policy, safer execution defaults, and
  snapshot-bound verification.

### Changed
- Orchestrator enforcement is documented and messaged as a **workflow guardrail** (not a hard
  sandbox). Kill-switch remains `FRONTIER_GUARDS_OFF=1` / `CLAUDE_GUARDS_OFF=1`.
- Codex body default is `codex exec -c model_reasoning_effort=<e> -` (no `--yolo` unless opted in).
- Grok body default omits `--permission-mode` unless YOLO or `FRONTIER_GROK_PERMISSION_MODE` is set.
- Unknown executors fail closed; whole-command overrides (`FRONTIER_*_CMD`) remain trusted
  compatibility inputs for tests and custom harnesses.

### Migration notes
- **Re-arm with a frozen gate.** Prefer
  `frontier-dispatch arm --gate "pytest -q"` (or your single argv test/build/lint command). After
  upgrade, sessions armed without a frozen gate cannot verify/finish until re-armed with `--gate`.
- **Gate command shape.** Use a single argv command string (`"pytest -q"`, `"python3 -m unittest"`).
  Do not rely on shell pipelines (`cmd1 | cmd2`) or chaining (`&&` / `;`) for the closing gate.
- **YOLO is no longer the default.** If your workflow needs unattended body autonomy, set
  `export FRONTIER_CODEX_YOLO=1` and/or `export FRONTIER_GROK_YOLO=1` explicitly (and only on repos you
  trust).
- **Plugin update.** After
  `/plugin marketplace update frontierfuse` and `/plugin update frontierfuse@frontierfuse`, **restart**
  Claude Code so hooks and skills reload.
- The 0.3.0 release replaces the pre-0.3 plugin identity and command namespace; reinstall the plugin
  under `frontierfuse@frontierfuse` and restart Claude Code.

## [0.2.5] - 2026-07-09

### Added
- Grok Build CLI can now be selected as the lead/body executor with
  `frontier-dispatch config --executor grok`, defaulting to the official `grok-4.5` model ID and
  managed prompt-file delivery for large dispatch specs.
- Added `FRONTIER_GROK_MODEL`, `--grok-model`, `FRONTIER_GROK_CMD`, `FRONTIER_GROK_EFFORT`, and
  `FRONTIER_GROK_PERMISSION_MODE` support for Grok executor customization, plus `FRONTIER_GROK_YOLO=0`
  to disable the default Grok `bypassPermissions` body mode.
- Added Grok executor contract coverage and a pre-push Grok dry-run smoke check.

## [0.2.4] - 2026-07-09

### Fixed
- Corrected the Opus executor default from the non-existent `claude-opus-5` to the current official
  `claude-opus-4-8` model ID.
- Added a release-gate check that blocks unverified Opus 5 references from re-entering shipped code,
  docs, tests, or plugin metadata.

## [0.2.3] - 2026-07-08

### Fixed
- The tracked pre-push release gate now allows clean, up-to-date public clones to pass while still
  requiring a version bump when a local branch is ahead of upstream.

## [0.2.2] - 2026-07-08

### Added
- Claude executor support gained an explicit Opus model option while preserving an independent
  advisor model.

## [0.2.1] - 2026-07-08

### Added
- **Real Claude Code plugin packaging**: `.claude-plugin/plugin.json` + a self-referential
  `.claude-plugin/marketplace.json` (matching the established `Renn-Labs/loopprint` pattern —
  verified directly against the real installed plugin, not assumed). Hooks now auto-register from
  `hooks/hooks.json` and skills auto-discover from `skills/` — no `settings.json` editing required.
  Install: `/plugin marketplace add Renn-Labs/FrontierFuse` then `/plugin install frontierfuse@frontierfuse`.
  Local dev: `claude plugin validate .` / `claude --plugin-dir .`.
- **`/frontierfuse-config`**: a new interactive skill for changing executor/model/effort/fast
  mid-session (`disable-model-invocation: true` — only runs on explicit request). Wraps the
  existing, tested `frontier-dispatch config` CLI rather than introducing new config storage; applies
  to the next dispatch, no restart needed.
- `frontier-dispatch doctor` now reports plugin-manifest presence and which install path (native
  plugin vs. manual `install-hooks`) is actually active.

### Changed
- The original `frontier_dispatch.py install-hooks`/`uninstall-hooks` path (merges hooks into
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
  immediately got blocked trying `python3 frontier_dispatch.py --help` (only the bare `frontier_dispatch.py`
  prefix matched, not the `python3 <script>` form everyone actually types) and blocked trying an
  inline env-var prefix like `FRONTIER_GUARDS_OFF=1 python3 ...`. The gate now strips a leading
  interpreter (`python3`/`python`) and leading `VAR=value` assignments before the allowlist
  comparison — the dangerous-metacharacter/chaining check still runs on the untouched original
  string, so this closes a real usability gap without reopening the security fix from `0.1.0`.

## [0.1.0] - 2026-07-07

### Added
- Initial FrontierFuse: Fable 5 (brain/advisor) + a swappable body/executor (Codex, no model version
  pinned by default, or Sonnet 5).
- **Advisor mode** (default): `frontier_advisor.ask_frontier`, the `ask-frontier` CLI, and `frontier_advisor_mcp.py`
  (stdio MCP server exposing `ask_frontier`) so an executor main loop consults Fable on-demand.
- **Orchestrator mode**: `frontier_dispatch.py` (single + parallel body dispatch, bounded handoff cards,
  raw artifacts, `arm`/`disarm`/`done`/`verify`/`config`/`doctor`/`install-hooks`).
- **Deterministic verify**: `frontier_verify.py` runs an external gate and writes `verdict.json`
  (GREEN iff the gate exits 0), with a diff sha and freshness check.
- **Workflow guardrail**: `hooks/frontier_gate.py` (PreToolUse) blocks the brain's direct
  mutation/execution while armed; `hooks/frontier_verify_gate.py` (Stop) blocks finishing until a fresh
  GREEN verdict. Tunable allowlist, trivial-edit escape, and `FRONTIER_GUARDS_OFF` kill-switch.
- Runtime config toggles for executor, model, effort, fast mode, and frontier model
  with per-call > session > global > env > default precedence; persist per-session or `--global`.
- `frontier_common.py` shared foundation; `frontier_scrub.py` (copied from FleetFuse); `/frontierfuse` skill;
  keyless offline CI; offline contract suite.

### Fixed
Found during live smoke + code review (native + `peer trio`) before the initial commit:
- **Workflow guardrail never engaged in a real Claude Code session** — `frontier-dispatch` defaulted its
  session key to the literal string `"default"`, but the real PreToolUse/Stop hook payload carries
  Claude Code's actual session id. `SESSION_ID` now auto-derives from `$CLAUDE_CODE_SESSION_ID`.
- **Bash allowlist bypass** — prefix matching alone let a chained command through an allowlisted
  prefix (e.g. `git status && rm -rf ...`). The gate now rejects any command containing shell
  metacharacters (`&& ; | \` $( > <`), not just prefix-matching the base command.
- **`frontier-dispatch done` unconditionally disarmed** the guardrail regardless of verdict state,
  letting the brain kill the gate on demand (it's itself Bash-allowlisted). `done` now refuses to
  disarm without a fresh GREEN verdict.
- `verdict.json` now chmod 0600 (can contain gate stdout/stderr).
- `FRONTIER_GATE_ALLOW_TRIVIAL` now uses proper boolean parsing (`"0"`/`"false"` no longer bypass).
- `install-hooks` now warns instead of silently replacing a malformed `settings.json`.
- Parallel dispatch cards now sort by label (previously returned in nondeterministic completion order).

### Changed
- **No Codex model is pinned by default.** The initial draft shipped `gpt-5.5-codex` as the
  default — that model never existed; it was invented from the "Codex 5.5 high" framing rather
  than verified. Live smoke confirmed the failure. Since OpenAI ships new Codex-capable models
  every few weeks, FrontierFuse now omits `--model` unless explicitly pinned via
  `FRONTIER_CODEX_MODEL`/`--model`, so Codex's own current account-aware default is used. See README
  "Staying current on model names".

### Notes
- Body invocation follows steipete/agent-scripts `codex-first` (`codex exec` with opt-in `--yolo`,
  `-c model_reasoning_effort=high`, prompt on stdin).
