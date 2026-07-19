# FrontierFuse Execution Plan

This is the durable implementation backlog after the provider-neutral FrontierFuse line. The shipped
baseline documented here is **0.3.7**. Releases convert independent frontier-model review into
sequenced, testable work. A release is complete only when every acceptance gate is backed by fresh
evidence.

## Product Contract

FrontierFuse configures a managed frontier consult and a managed executor while the host harness
retains the session lead, then produces a local, verifiable receipt of what happened.

The product has exactly two roles:

- `frontier`: supplies configured managed consultation; it does not replace the host lead.
- `executor`: performs bounded implementation or research work.

The roles support exactly two profiles:

- `executor-led` (`advisor`): the host executor owns the session and consults the frontier on demand.
- `controller-led` (`orchestrator`): the host controller owns delegation and requires deterministic
  verification. Managed executor bodies run through `frontier-dispatch`.

Role binding must always be explicit:

- `host`: the harness selects and runs the model. A plugin cannot replace or hot-swap it.
- `managed`: FrontierFuse invokes the selected provider adapter itself (consults and bodies).

Until a managed controller process ships, orchestrator planning remains host-owned; the configured
frontier is managed consult capacity, not an automatic host-model replacement.

Deterministic verification is infrastructure, not a third model role. Model advice never makes a
verdict GREEN.

## Compatibility Contract

The following remain supported throughout the `1.x` line:

- Claude plugin ID `frontierfuse`.
- Existing `frontier-dispatch`, `ask-frontier`, `/frontierfuse`, and `/frontierfuse-config` entry points.
- `~/.config/frontier-fuse/` configuration and state paths.
- Existing `FRONTIER_*` environment variables.

Startup must never silently migrate an armed session or destructively rewrite configuration.

## Global Release Gates

Every release must satisfy all applicable gates:

- [ ] New behavior has offline regression coverage.
- [ ] Python 3.10 and 3.12 contract tests pass.
- [ ] `python3 -m compileall` passes for shipped modules, hooks, and tests.
- [ ] Claude plugin validation passes.
- [ ] README install, upgrade, rollback, restart, and compatibility guidance matches the release.
- [ ] Plugin and marketplace versions match the changelog release (and the MCP + update version
      carriers).
- [ ] `scripts/pre-push-check.sh` passes from a clean worktree.
- [ ] `scripts/public-release-scrub.py --all-history` passes before public exposure and after any
      history rewrite.
- [ ] An independent reviewer checks security, migration, and product claims.
- [ ] No generated runs, verdicts, provider transcripts, private state, secrets, or private
      absolute paths are tracked.

## Delivered Through 0.3.6

### Release 0.2.6 - Trust Boundary Correction (Delivered)

Goal: make the current control loop materially safer and describe its boundary truthfully.

#### Gate and command policy

- [x] Replace the broad Bash prefix allowlist with parsed command policy.
- [x] Deny direct `codex`, `grok`, `claude`, and `gemini` body execution while the controller gate is armed.
- [x] Deny `frontier-dispatch disarm` from the armed model tool path.
- [x] Allow only the exact non-mutating `frontier-dispatch` subcommands required by the loop.
- [x] Restrict verifier entry points to the expected project script and safe options.
- [x] Remove broad `find` allowance or validate that its actions are read-only.
- [x] Preserve the explicit user kill switches and trivial-edit escape with accurate warnings.
- [x] Add hostile-command tests for shell separators, command substitution, wrappers, aliases,
      quoted paths, path traversal, and misleading prefixes.

#### Snapshot-bound verdicts

- [x] Define a versioned workspace snapshot containing HEAD, index tree, unstaged diff, bounded
      untracked-file hashes, workspace root, effective configuration hash, and gate argv.
- [x] Capture the pre-gate snapshot and final snapshot.
- [x] Make GREEN valid only when the gate exits zero and the final snapshot still matches the
      recorded verified snapshot.
- [x] Recompute the workspace snapshot in the Stop hook before accepting GREEN.
- [x] Invalidate verdicts after staged, committed, unstaged, untracked, configuration, or gate
      changes.
- [x] Keep legacy verdicts readable but never let them satisfy the stronger gate.

#### Safer execution and artifacts

- [x] Default Codex and Grok permission behavior to inherited/provider defaults.
- [x] Keep bypass/autonomous profiles explicit and opt-in.
- [x] Create state, run, prompt, response, and verdict files with owner-only permissions.
- [x] Replace new shell-string verification paths with argv execution.
- [x] Keep the legacy shell gate only as an explicitly unsafe compatibility path with a warning.
- [x] Ensure provider-execution timeout handling terminates the whole provider process group.

#### Truthful product surface

- [x] Replace "hard gate" with "workflow guardrail" for host-hook behavior.
- [x] Replace "cost-optimal" with "lower coordination overhead."
- [x] Explain that cross-provider prompts leave the local machine.
- [x] Explain that a host-bound plugin cannot independently replace the host model.
- [x] Synchronize README, DESIGN, SECURITY, skills, manifests, and agent guidance.

#### Acceptance evidence

- [x] Every bypass identified in the Sol review has a passing regression test.
- [x] Any post-GREEN workspace mutation causes the Stop hook to reject completion.
- [x] Missing hook imports fail the test suite instead of silently passing.
- [x] Default dry-runs omit `--yolo` and `bypassPermissions`.
- [x] Existing commands and configuration continue to work.

Non-goals (this release): FrontierFuse rebrand, new provider support, managed controller process,
new plugin ID.

### Release 0.3.0 - Installation, Doctor, Quiet Updates, Roles (Delivered)

Goal: make supported harnesses installable, diagnosable, and update-aware without telemetry, and
expose provider-neutral roles/profiles.

- [x] Document native Claude marketplace install, update, restart, rollback, and uninstall.
- [x] Document stable-checkout Codex and Grok MCP registration, update, restart, rollback, and
      uninstall (Gemini uses the same checkout pattern; no native marketplace claimed).
- [x] Keep `doctor` offline by default and add cached release status to its readiness report.
- [x] Add an explicit `doctor --check-updates` network path.
- [x] Add `update --check` with `passive`, `manual`, and `off` modes.
- [x] Cache passive checks for seven days in an owner-only file.
- [x] Run passive checks only during explicit FrontierFuse skill use and stay silent when current or
      offline.
- [x] Provide exact manual marketplace and checkout update commands; never update automatically.
- [x] Keep update requests free of machine identifiers, repository data, prompts, and telemetry.
- [x] Change public display branding, plugin ID, commands, and repository references to FrontierFuse.
- [x] Add explicit `profile`, `frontier_provider`, `frontier_model`, executor, and provider model fields.
- [x] Separate host-model limitations from managed provider calls in docs and skills.
- [x] Add a source-backed catalog for verified Fable, GPT-5.6, Claude, Grok, and Gemini IDs.
- [x] Add local Grok model discovery and custom exact model IDs without inventing static releases.
- [x] Synchronize core update, MCP server, plugin, marketplace, changelog, README, and design
      versions.

Non-goals: daemon, startup ping, machine identifier, automatic update, custom marketplace service,
Codex/Grok/Gemini plugin packages, managed controller process.

### Release 0.3.2 - Reliable Configuration and Diagnostics (Delivered)

Goal: make configuration failures recoverable and readiness reports trustworthy.

- [x] Add `schema_version` to global config, session state, handoff cards, and verdicts.
- [x] Validate every persisted executor, model, effort, profile, fast-mode, and update-mode value.
- [x] Fail closed on unknown executors instead of falling through to Codex.
- [x] Write JSON atomically through owner-only temporary files plus `os.replace`.
- [x] Add advisory file locks for concurrent configuration and state writes.
- [x] Preserve a timestamped owner-only backup before explicit repair.
- [x] Add typed offline doctor states and machine-readable recovery actions for configuration,
      CLI, installation, state-directory, and update readiness.
- [x] Keep `doctor` offline unless the existing explicit `--check-updates` path is selected.
- [x] Synchronize core, MCP server, plugin, marketplace, and documentation versions.
- [x] Add Linux and macOS CI coverage for config recovery and command construction.

Live auth, model entitlement, CLI compatibility, and probe-failure classification require the
provider capability contract and remain in `0.4.0`; no default doctor path makes provider calls.

### Releases 0.3.3 – 0.3.6 (Delivered maintenance)

- [x] **0.3.3** — portable verification contracts (PATH-resolved gate executables across Ubuntu/macOS).
- [x] **0.3.4** — verification fixture path canonicalization for macOS `/var` aliases and symlinked
      temp roots.
- [x] **0.3.5** — `--executor-model` as primary executor pin; `--model` retained as legacy alias;
      conflict handling when both are passed; docs/contracts keep profile, frontier, and executor
      decisions separate.
- [x] **0.3.6** — provider-aware model discovery and doctor suggestions; truthful host-bound
      workflow/install UX; public-release scrub, denylist, and offline pre-push hardening.

## Planned Releases

The milestone sections below define the completion envelope. Work is delivered through the
following smaller operating releases so each increment can be independently rejected. Planned
scope is not shipped capability.

| # | Release | Operating outcome |
|-:|-|-|
| 1 | `0.3.7` | Guardrail and execution-boundary hardening |
| 2 | `0.4.0` | Provider Adapter v1 contract and migration |
| 3 | `0.4.1` | Offline provider capability evidence |
| 4 | `0.4.2` | Explicit auth, entitlement, and model probes |
| 5 | `0.4.3` | Effective topology, context crossing, and consent explanation |
| 6 | `0.5.0` | Receipt v3, leased run identity, and crash journal |
| 7 | `0.5.1` | Status, receipt inspection, and redacted export |
| 8 | `0.5.2` | Idempotent resume and artifact retention controls |
| 9 | `0.6.0` | Core Protocol v1 and wrapper negotiation |
| 10 | `0.6.1` | Official-surface Codex package |
| 11 | `0.6.2` | Cross-harness install, update, rollback, and uninstall proof |
| 12 | `0.7.0` | Compatibility-aware release metadata |
| 13 | `0.7.1` | Migration-aware stable and preview update guidance |
| 14 | `0.7.2` | Artifact provenance, checksums, and rollback metadata |
| 15 | `0.8.0` | Capped managed-controller preview |
| 16 | `0.8.1` | Bounded depth-one worker pools |
| 17 | `0.8.2` | Matched-cost evidence and explainable local presets |
| 18 | `0.9.0` | Release candidate and contract freeze |
| 19 | `0.9.1` | Observation-window fixes only |
| 20 | `1.0.0` | Stable compatibility and evidence contract |

These rows are an operating decomposition, not a promise to publish twenty tags. A hardening row
with no independently valuable accepted delta folds into its milestone instead of shipping only to
preserve the count. After the adapter boundary, the product spine (`0.4.x` -> `0.5.x` -> `0.8.x`)
and distribution spine (`0.4.x` -> `0.6.x` -> `0.7.x`) may develop in parallel, but both must
converge at `0.9.0`; no lane may bypass its own predecessor.

### Release 0.3.7 - Guardrail and Execution Boundary Hardening (Delivered 2026-07-19)

Also ships multi-role topology foundation + OpenRouter provider (see DESIGN).

Goal: close confirmed safety gaps before introducing the provider adapter boundary.

#### Tasks

- [ ] Cover mutating host-tool classes fail-closed while preserving an explicit narrow read-only
      allow policy; continue to describe hooks as workflow guardrails, not a sandbox.
- [ ] Terminate the complete verification-gate process group on timeout and interruption, matching
      the already-delivered provider-execution cleanup behavior.
- [ ] Enforce validated hard task-count and provider/gate output limits during execution rather
      than clipping only after unbounded buffering.
- [ ] Normalize timeout, crash, and truncation evidence without exposing prompts or secrets.
- [ ] Keep interrupted active markers fail-closed and provide an explicit recovery diagnosis;
      durable leased recovery remains in `0.5.0`.

#### Acceptance evidence

- [ ] Hostile tool fixtures cannot mutate through uncovered armed tool classes.
- [ ] Timed-out gates and providers leave no live descendant process.
- [ ] Output and task-count limits cannot be exceeded in fake-process stress tests.
- [ ] All existing offline contract suites remain GREEN.

Non-goals: provider adapter migration, automatic stale-run recovery, managed controller, new
provider, versioned role graph.

### Release 0.4.0 - Provider Adapter Contract (Next tranche)

Goal: make Claude, Codex, Grok, and Gemini execution behavior predictable and capability-aware.

#### Tasks

- [ ] Define one adapter interface for detection, argv construction, prompt transport, permission
      mapping, timeout, exit normalization, usage reporting, and capability declaration.
- [ ] Extract the existing Claude, Codex, Grok Build, and Gemini command builders behind the adapter interface.
- [ ] Execute argv without a shell by default.
- [ ] Scope every run to an explicit workspace root.
- [ ] Transport prompts through stdin or owner-only temporary files.
- [ ] Bound captured output and artifact size.
- [ ] Kill provider process groups on timeout and interruption.
- [ ] Filter inherited environment variables through a documented allow policy.
- [ ] Require explicit confirmation before sending context to another provider.
- [ ] Normalize errors without exposing prompt or secret content.

#### Acceptance evidence

- [ ] Golden fake-CLI tests cover success, timeout, crash, auth failure, invalid model, oversized
      output, malformed structured output, and interruption.
- [ ] Adapter capability output matches actual harness behavior.
- [ ] Legacy whole-command overrides remain available with a trusted-input warning.

Non-goals: provider SDKs, extra providers, automatic routing, model leaderboard.

### Release 0.5.0 - Verification Receipts and Recovery

Goal: let users prove exactly what was verified and resume safely after failures.

#### Tasks

- [ ] Expand the snapshot contract to include component versions, adapters, models, permissions,
      retries, changed paths, artifact hashes, redaction mode, and retention policy.
- [ ] Freeze named verification argv before execution.
- [ ] Add `status`, `resume`, `receipt show`, and redacted `receipt export` commands.
- [ ] Store runtime state outside the repository by default.
- [ ] Add configurable artifact retention with safe deletion.
- [ ] Keep prompt storage disabled by default.
- [ ] Mark usage as provider-reported, estimated, or unavailable.
- [ ] Add crash-recovery journaling for dispatch and verification transitions.

#### Acceptance evidence

- [ ] Any post-GREEN mutation invalidates the receipt.
- [ ] Injected process interruption never leaves corrupt state.
- [ ] Redacted exports contain no configured secret fixtures.
- [ ] Legacy verdicts remain inspectable but cannot satisfy current verification.

Non-goals: cloud storage, telemetry, transcript dashboard, model-based judge.

### Release 0.6.0 - Claude and Codex Packaging

Goal: provide first-class installation for the two harnesses with stable packaging surfaces.

#### Tasks

- [ ] Extract one versioned core protocol shared by all wrappers.
- [ ] Keep the Claude Code marketplace package thin and backward compatible.
- [ ] Add a schema-validated Codex plugin/skill package using only officially supported surfaces.
- [ ] Expose a generic `ask_frontier` MCP tool.
- [ ] Add wrapper/core protocol negotiation and actionable skew errors.
- [ ] Document host-model ownership and harness-specific enforcement levels.
- [ ] Test clean install, upgrade, rollback, and uninstall independently.
- [ ] Abort manual installers when harness configuration is malformed.

#### Acceptance evidence

- [ ] Every supported OS/harness install, upgrade, and uninstall passes three consecutive times in
      fresh environments.
- [ ] Existing Claude marketplace installations upgrade in place.
- [ ] No documentation claims identical enforcement across harnesses.

Non-goals: unsupported Codex hooks, Grok pseudo-marketplace, plugin-ID change.

### Release 0.7.0 - Compatibility Metadata and Native Distribution

Goal: build on the quiet `0.3.0` reminder with protocol-aware compatibility and native harness
distribution where official packaging surfaces support it.

#### Tasks

- [ ] Add core protocol handshake and supported-version ranges.
- [ ] Extend `update --check` to distinguish compatible, migration-required, and rollback-required
      releases.
- [ ] Preserve the existing passive/manual/off privacy and cache contract.
- [ ] Notify only for compatible stable releases unless the user selects a preview channel.
- [ ] Publish checksummed release metadata with schema, protocol, channel, and migration notes.
- [ ] Prefer native harness update metadata where it exists.
- [ ] Add a Grok package only if current official Grok packaging supports the required contract.
- [ ] Build `0.7.2` artifact-provenance and rollback checks against offline fixtures before H2;
      require fresh-environment verification against public artifacts only after H2 authority.

#### Acceptance evidence

- [ ] Manual and off modes continue to make zero passive update-network requests.
- [ ] Passive checks do not measurably delay normal startup.
- [ ] Every incompatible pairing reports the exact safe update or rollback action.
- [ ] Old source URLs continue to install and update after any future repository rename.

Non-goals: daemon, startup ping, machine identifier, automatic update, custom marketplace service.

### Release 0.8.0 - Managed Controller Preview

Goal: let users independently select the frontier and executor when measurable value justifies the
additional cost and latency.

#### Tasks

- [ ] Add experimental `frontier-fuse run` managed loop.
- [ ] Require managed bindings for independently selected models.
- [ ] Add hard call, concurrency, retry, timeout, and artifact caps.
- [ ] Add explicit entitlement probes with preview-aware failure states.
- [ ] Add Fable/Grok and GPT-5.6 Sol/Grok recipes only when locally entitled.
- [ ] Record provider-reported usage when available; never present inferred token or dollar values
      as provider-reported evidence.
- [ ] Deny recursive delegation before `1.0.0`; keep preview worker pools depth-one and capped.
- [ ] Define a representative benchmark set before evaluating quality.

#### Acceptance evidence

- [ ] Run at least 30 representative tasks with frozen inputs; claim matched cost only where
      provider-reported usage supports the comparison.
- [ ] Managed mode improves verified completion by at least 10 percentage points or remains
      experimental.
- [ ] No benchmark run exceeds configured fan-out or retry limits.
- [ ] Latency and usage reports distinguish measured, reported, estimated, and unavailable values.

Non-goals: autonomous workforce, self-routing, hard dollar guarantee, default managed mode.

### Release 0.9.0 - Release Candidate

Goal: freeze the support contract and prove upgrades before declaring stability.

#### Tasks

- [ ] Freeze config schema v2 and core protocol v1.
- [ ] Publish the complete support, compatibility, privacy, and threat-model documentation.
- [ ] Run independent product, security, migration, and accessibility reviews.
- [ ] Exercise rollback from every supported install path.
- [ ] Run the full supported-baseline migration matrix, including reproducible `0.3.0` and `0.3.6`
      fixtures; include `0.2.5` only if its exact artifact or commit is retained and reproducible.
- [ ] Consider renaming the repository only after old URLs and marketplace installs remain green.
- [ ] Keep plugin ID, commands, paths, and legacy environment variables unchanged.
- [ ] Open a 30-day release-candidate observation window.

#### Acceptance evidence

- [ ] No unresolved P0/P1 security, migration, data-loss, or installation issue for 30 days.
- [ ] Upgrade and rollback from every declared reproducible baseline pass on every supported
      harness and OS.
- [ ] Old repository URLs and install commands remain valid after any rename.

Non-goals: plugin-ID rename, new provider, large new feature, legacy removal.

### Release 1.0.0 - Stable Contract

Goal: publish a dependable long-term support and compatibility boundary.

#### Tasks

- [ ] Ship only the reviewed release candidate.
- [ ] Publish checksummed artifacts and release metadata.
- [ ] Publish semver, compatibility, security-response, and deprecation policies.
- [ ] Document every harness capability and enforcement limitation.
- [ ] Confirm the complete install, migration, rollback, and uninstall matrix.
- [ ] Confirm independent reviews have no release blocker.
- [ ] Preserve every promised 1.x compatibility alias.

#### Acceptance evidence

- [ ] Every global release gate is GREEN with fresh evidence.
- [ ] Every 0.9.0 blocker is closed and independently verified.
- [ ] No experimental managed feature is promoted without meeting its quality and cost gate.

Non-goals: last-minute providers, automatic updates, legacy removal, plugin-ID rename.

## Kill Criteria

- Hide controller-led mode behind Advanced if users cannot explain who owns the loop.
- Keep managed controller mode experimental if it misses the verified-completion threshold or
  regularly exceeds twice the latency without a quality gain.
- Do not create a Grok marketplace story without a stable official packaging contract.
- Remove dollar budgets if provider CLIs cannot expose reliable usage.
- Keep update checks manual if passive checks add noticeable delay or privacy concern.
- Keep account-specific model selections custom if entitlement cannot be diagnosed reliably.
- Prefer prompt non-retention and permission controls over redaction that damages reproducibility or
  creates false confidence.

## Current Execution Tranche

**Shipped baseline: `0.3.7`.** Delivered work through 0.3.6 is complete (trust boundary, install/
doctor/updates, roles, reliable configuration, provider-aware model selection, release safety,
portability fixes, executor-model alias).

**Next planned tranche: `0.4.0` (Guardrail and Execution Boundary Hardening), followed by `0.4.0`
(Provider Adapter Contract).** All later operating releases remain pending until their predecessor
dependencies and evidence gates are satisfied. Isolated candidate branches may be built in
parallel, but they do not integrate out of order and do not change version carriers. Docs and
install UX may improve on the 0.3.7 baseline without claiming unfinished capabilities.
