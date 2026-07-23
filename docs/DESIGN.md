# FrontierFuse Architecture (0.3.8)

This document describes the shipped Claude Code plugin and shared checkout surface. The roadmap is
in `docs/FRONTIERFUSE_EXECUTION_PLAN.md`. Baseline **0.3.8** includes provider-neutral roles, offline
doctor, quiet updates, reliable configuration, Gemini executor support, provider-aware model
discovery, and `--executor-model` as the primary executor pin (with `--model` as legacy alias).

## Product Contract

FrontierFuse separates profile, frontier, and executor as independent decisions:

| Decision | Values | Default |
|-|-|-|
| Profile | `advisor`, `orchestrator` | `advisor` |
| Frontier provider | `codex`, `claude`, `grok`, `gemini`, `openrouter` | `claude` |
| Frontier model | provider model ID | `claude-fable-5` |
| Executor provider | `codex`, `claude`, `grok`, `gemini`, `openrouter` | `codex` |
| Executor model | provider model ID | Codex account default (empty pin) |
| Effort | Codex/Grok: `low`/`medium`/`high` (+ Codex `xhigh`) | provider default / high lane |
| Update mode | `passive`, `manual`, `off` | `passive` |

**Providers are not models.** Sonnet, Opus, and Fable are Claude model IDs, not provider names.

The host harness owns the model already driving its conversation. FrontierFuse does **not** replace
that model and cannot hot-swap it. It configures managed provider calls and the role contract. Until
a managed controller process exists, orchestrator planning remains host-owned; the configured
frontier is managed consult capacity.

### Practical workflows

#### 1. Host / executor-led advisor

```text
user -> executor -> frontier advice (on demand) -> executor -> tests
```

The executor plans, edits, and uses tools. `ask_frontier` calls the configured frontier model only
for decision support. No arm/disarm flow. Lowest frontier-token use and coordination overhead.

#### 2. Host-led verified orchestration (managed executor bodies)

```text
user -> current host controller -> managed executor bodies -> host synthesis -> frozen verifier
```

The host plans, dispatches via `frontier-dispatch`, reviews raw evidence, and synthesizes. The
executor runs bounded work bodies. Claude Code hooks act as a workflow guardrail while armed; they
are not a sandbox. Higher coordination cost; use when snapshot-bound GREEN matters.

#### 3. Premium host lead + deep frontier advisor + cheaper executor bodies (pattern, not a profile)

```text
user -> premium host model (harness-selected)
         -> managed deep frontier consults
         -> cheaper managed executor bodies
      -> host integrates + tests / verify
```

This is not a third profile value: choose `advisor` for occasional managed consults, or choose
`orchestrator` when it also needs guarded body dispatch and frozen verification. Judgment stays on a
strong host model; deep advice is occasional; bulk implementation can use a cheaper executor. Still
host-bound: the plugin cannot swap the harness model.

## Modules

| Path | Responsibility |
|-|-|
| `frontier_common.py` | Config precedence, state, provider command builders, verdict schema, artifacts, owner-only writes |
| `frontier_models.py` | Source-backed current/previous model catalog and local CLI discovery |
| `frontier_advisor.py` / `frontier_advisor_mcp.py` | Provider-neutral `ask_frontier` consult |
| `frontier_dispatch.py` | Models, config, doctor, updates, dispatch, arm, verify, done, hook installation |
| `frontier_update.py` | Privacy-preserving cached release checks |
| `frontier_verify.py` | Snapshot-bound deterministic verifier |
| `frontier_scrub.py` | Optional response redaction helper |
| `hooks/frontier_gate.py` | Armed PreToolUse workflow policy |
| `hooks/frontier_verify_gate.py` | Armed Stop policy |
| `skills/frontierfuse/` | Main operating contract |
| `skills/frontierfuse-config/` | Sequential interactive configuration |

The shipped modules are stdlib-only and support Python 3.10+.

## Provider Adapters

`build_body_command` dispatches on the provider boundary:

| Provider | Prompt transport | Model setting |
|-|-|-|
| Codex | stdin to `codex exec ... -` | optional `--model`; empty preserves account default |
| Claude | stdin to `claude -p` | `--model` |
| Grok | owner-only temporary `--prompt-file` | `--model`; local models discoverable via `grok models` |
| Gemini | stdin appended to `--prompt` | `--model` |

`build_frontier_command` supports the same providers for managed advice. Whole-command overrides
remain available through `FRONTIER_ADVISOR_CMD`, `FRONTIER_BODY_CMD`, and provider-specific
`FRONTIER_*_CMD` variables.

Model IDs in `frontier_models.py` must be verified against official provider documentation. Local
discovery can add account-visible IDs at runtime without claiming them as static public releases.
Catalog status fields (`recommended`, `current`, `previous`, …) are availability-oriented
suggestions only — not authentication or entitlement probes.

## Frozen Verifier

The host freezes a verifier before delegation:

```bash
frontier-dispatch arm --gate "<single argv command>" --cwd PATH
```

The default path parses with `shlex` and executes with `shell=False`. Shell operators,
redirection, substitutions, and newlines are refused. A closable arm requires a Git worktree.

Each schema-v2 verdict records cwd, HEAD, index tree, staged/unstaged hashes, bounded non-ignored
untracked fingerprints, effective config hash, gate argv, and gate identity. GREEN requires:

1. Gate exit code 0.
2. Stable before/after snapshots.
3. Complete supported Git evidence.
4. A non-legacy, non-unsafe gate path.
5. Timestamp after the last dispatch.
6. Live workspace and config still matching at Stop or `done`.
7. Receipt argv and cwd matching the arm record.

Legacy verdicts remain readable but cannot close the loop.

## Workflow Guardrail

While armed on Claude Code's hook surface, PreToolUse denies direct mutation and direct provider
CLIs while allowing bounded dispatcher commands and read-only inspection. Stop refuses completion
without fresh matching GREEN. Trivial edit and kill-switch behavior remains explicit.

The user can disable hooks, disarm, edit state, or use an unhooked shell. This is workflow control,
not process isolation.

## Permissions And Privacy

Provider permission defaults are inherited. `FRONTIER_CODEX_YOLO=1` and
`FRONTIER_GROK_YOLO=1` are explicit opt-ins. `FRONTIER_GROK_PERMISSION_MODE` selects an explicit
Grok mode.

Cross-provider prompts leave the machine. Local config, state, prompt files, cache, artifacts, and
handoff cards are written owner-only by FrontierFuse. Generated runs, verdicts, provider logs,
credentials, and private paths are never release artifacts. Ordinary doctor and passive update
checks send no machine or project telemetry.

## Configuration Reliability

Global config, session state, and handoff cards carry explicit schema versions. Configuration and
state updates use owner-only atomic replacement plus advisory file locks on the supported Linux and
macOS platforms. Persisted provider, profile, effort, model, fast-mode, and update-mode values are
validated before use; invalid values fail closed.

Malformed global config is never silently replaced. `frontier-dispatch doctor [--json]` reports a
typed `config_invalid` state and an actionable recovery command. Explicit
`frontier-dispatch config --repair --global` preserves the exact original in a timestamped `0600`
backup before writing a minimal current-schema document. The same command without `--global`
preserves and repairs malformed current-session state. Armed hooks deny safely until invalid state
is explicitly repaired.

Executor dispatches are recorded as active before provider work begins and removed on completion.
`done` uses a compare-and-set state transition after snapshot verification, so an overlapping
dispatch or state change cannot disarm the workflow guardrail with a stale verdict.
Each dispatch also increments a monotonic generation and clears the prior verdict. Verification
receipts bind to that generation, so clock rollback cannot make an older GREEN valid for newer work.
Every session mutation increments a separate state revision. Final verdict persistence and `done`
use compare-and-set transitions against that revision, so concurrent config, dispatch, or gate-state
changes cannot commit a stale GREEN or disarm decision. The global configuration lock is held from
the final gate snapshot through verdict publication, and across close freshness checks, preventing a
concurrent global update from escaping those decisions without blocking the gate subprocess itself.
Starting verification clears the previous
receipt and records an active verification ID. Receipt publication atomically removes that ID and
succeeds only when the verifier remained the sole unchanged attempt; overlapping verifiers therefore
return RED. Stop refuses while verification is active and revalidates session revision after its live
workspace snapshot before accepting GREEN. Accepting GREEN atomically marks that session generation
closed under the state lock, so a queued dispatch is refused until re-arm or deliberate disarm clears
the marker. The shared `verdict.json` artifact is cleared at verifier
or dispatch start, after session authority is invalidated, and written only if that verification ID still owns the authoritative session
receipt with no active or newer dispatch. Artifact persistence failure clears session authority.
Non-standard JSON constants and non-finite timestamps fail closed. If a process is killed before
its run marker can be removed, explicit host-side `disarm` clears orphan markers.

Doctor JSON distinguishes blocking execution prerequisites from optional ecosystem checks with a
`blocking` field. Optional hook or release-status checks can be unavailable while the selected
provider CLIs remain ready. Manual Claude hook readiness is determined from parsed `PreToolUse` and
`Stop` command entries; corrupt JSON or malformed hook structures are reported as `probe_failed`.
Doctor probes both the session and global-configuration advisory-lock paths used by verification and
closure before reporting readiness.
Configuration commands validate the prospective effective defaults/environment/global/session
composition before writing any layer, preventing individually valid files from creating an unusable
cross-layer combination.

### Doctor exit codes

| Code | Meaning |
|-|-|
| `0` | READY — blocking body + frontier CLIs present and global lock usable |
| `1` | NOT READY — missing blocking prerequisite, unusable lock, unwritable state, etc. |
| `2` | CONFIG_INVALID or invalid session identifier |

CLI presence never claims authentication or model entitlement. Use `doctor --check-updates` or
`update --check` only when an explicit release-metadata network path is desired.

## Packaging And Lifecycle

Claude Code uses `.claude-plugin/plugin.json`, the self-referential marketplace manifest,
conventional hook loading, and skill discovery. Manual `install-hooks` / `uninstall-hooks` (Option B)
remains the fallback for environments without marketplace access.

Codex, Grok Build, and Gemini CLI use a stable shared checkout plus stdio MCP registration. Separate
native marketplace packages for those harnesses are **not** claimed.

Verified MCP registration / removal (after setting `FRONTIERFUSE_HOME`):

```bash
codex mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
codex mcp remove frontier-advisor

grok mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
grok mcp remove frontier-advisor

gemini mcp add frontier-advisor python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
gemini mcp remove frontier-advisor
```

Restart the host session after MCP or hook changes so the new registration loads.

Doctor is offline by default. Passive update reminders use an owner-only seven-day cache, send no
machine or project data, and never mutate an installation.

Version changes must stay synchronized across the four version carriers — plugin/marketplace
manifests, `frontier_advisor_mcp.py`, and `frontier_update.py` — plus changelog, README, and skills.

## Verification

```bash
python3 tests/run_contracts.py
python3 tests/run_contracts.py --self-test
claude plugin validate .
scripts/pre-push-check.sh
python3 scripts/public-release-scrub.py --all-history
```

Tests are keyless and offline. Runtime behavior is covered with dry-runs, synthetic hook payloads,
temporary Git repositories, and dummy command overrides.

## Multi-role topology (0.3.7+)

FrontierFuse still has two native durable slots (`frontier` consult + `executor` body). Optional
named `roles` bindings add extra consult/body labels (for example `orchestration_consult` =
Codex `gpt-5.6-sol` @ `xhigh`) without claiming a host-model swap. Inspect with:

```bash
frontier-dispatch topology --json
frontier-dispatch role set --name orchestration_consult --kind consult \
  --role-provider codex --role-model gpt-5.6-sol --role-effort xhigh
frontier-dispatch consult --role orchestration_consult --dry-run --question "plan this"
```

OpenRouter is a fifth provider backend. Live calls require `OPENROUTER_API_KEY`. Default doctor
remains offline; key presence is not entitlement proof.
