# FrontierFuse

Pair a selectable **frontier model** with a separate coding **executor**. FrontierFuse supports
Codex, Claude, Grok, and Gemini **providers** in executor-led `advisor` and host-led
`orchestrator` profiles. Fable 5 is the recommended Claude frontier **model**, not a provider and
not the only brain.

Current version: **0.3.8**

**Providers are not models.** Choose provider and model as separate decisions. Never invent model
IDs; use `frontier-dispatch models` and only accept exact IDs the provider CLI can verify.

A plugin **cannot hot-swap the host harness model** already driving the conversation. The configured
frontier is a **managed consult** (or role contract) until a future managed controller exists. Hooks
are a workflow guardrail, not an OS sandbox.

---

## Copy This Into Your Coding Harness

Paste the following prompt into Claude Code, Codex, Grok Build, Gemini CLI, or another agentic
coding harness. It instructs the harness to **set FrontierFuse up for you**: install or update,
configure decisions in order, and verify readiness — not merely describe steps.

```text
Install or update FrontierFuse from https://github.com/Renn-Labs/FrontierFuse and configure it for
this coding harness. Work autonomously through detection, installation, verification, and setup.
Do not expose credentials, prompts, private paths, provider transcripts, or local state.

Target result:
- FrontierFuse is installed through the best supported surface for this harness.
- `frontier-dispatch doctor` has been run and its exit code explained.
- These decisions are configured separately, in order:
  1) profile (`advisor` or `orchestrator`)
  2) frontier provider
  3) frontier model
  4) executor provider
  5) executor model
  6) effort (Codex/Grok only)
  7) update mode
- Selected provider CLIs exist on PATH. Catalog membership is not proof of auth or entitlement.
- Update reminders are configured. After MCP or hook changes, the host is reloaded/restarted.

1. Detect the host harness and operating system.

2. Install or update using the applicable path (do not invent native marketplaces):

   Claude Code marketplace (primary):
   /plugin marketplace add Renn-Labs/FrontierFuse
   /plugin install frontierfuse@frontierfuse
   If already installed:
   /plugin marketplace update frontierfuse
   /plugin update frontierfuse@frontierfuse
   Use /reload-plugins after skill-only changes. Fully restart Claude Code after installing or
   updating hooks or MCP-related code. The plugin provides /frontierfuse and /frontierfuse-config.

   Claude Code Option B (manual hooks, no marketplace):
   export FRONTIERFUSE_HOME="$HOME/.local/share/FrontierFuse"
   # clone or ff-only pull as below, then:
   python3 "$FRONTIERFUSE_HOME/frontier_dispatch.py" install-hooks
   Fully restart Claude Code so hooks load. Doctor should report "manually installed (Option B)".

   Codex, Grok Build, or Gemini CLI shared checkout (no native FrontierFuse marketplace package):
   export FRONTIERFUSE_HOME="$HOME/.local/share/FrontierFuse"
   if [ -d "$FRONTIERFUSE_HOME/.git" ]; then
     mkdir -p "$HOME/.config/frontier-fuse"
     git -C "$FRONTIERFUSE_HOME" rev-parse HEAD > "$HOME/.config/frontier-fuse/last-known-good"
     (cd "$FRONTIERFUSE_HOME" && git pull --ff-only)
   else
     git clone https://github.com/Renn-Labs/FrontierFuse.git "$FRONTIERFUSE_HOME"
   fi
   export PATH="$FRONTIERFUSE_HOME/bin:$PATH"
   Persist PATH once in the detected shell profile (~/.bashrc or ~/.zshrc only; never invent another):
   export PATH="$HOME/.local/share/FrontierFuse/bin:$PATH"

   Register MCP with the host-native syntax only:
   codex mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
   grok mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
   gemini mcp add frontier-advisor python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"
   Gemini CLI takes its command and arguments positionally, so its verified form intentionally has
   no `--` separator.
   Then restart that harness session so MCP is loaded.

3. Run diagnostics without making live model calls:
   frontier-dispatch doctor
   frontier-dispatch doctor --json
   frontier-dispatch doctor --check-updates
   frontier-dispatch update --check

   Doctor is offline by default and does not create an update cache. Exit codes:
   - 0 = READY (blocking CLIs present; hooks/release status may still be optional). This is local
     CLI/state readiness only, not proof that configuration choices are complete or models authorized.
   - 1 = NOT READY (missing blocking CLI, unusable lock, unwritable state, etc.)
   - 2 = CONFIG_INVALID or invalid session id (repair/fix identity before continuing)

   CLI presence does not prove provider authentication, model entitlement, or live compatibility.
   Report those as unprobed unless an explicit provider-side check has succeeded.

   Availability-based suggestions: `frontier-dispatch models` lists source-backed catalog rows and
   local discoveries (status such as recommended/current/previous). Treat those as non-auth,
   non-mutating recommendations only — not proof the account can call that model.

   If doctor reports config_invalid, preserve the original and follow its next_step:
   frontier-dispatch config --repair --global
   frontier-dispatch config --repair
   Reapply valid selections from the owner-only timestamped backup. Session repair clears the
   workflow guardrail and prior verdict; re-arm with the approved gate before treating the session
   as complete.

4. Show current config: frontier-dispatch config

5. Ask profile alone (do not combine it with provider/model). The only profile values are `advisor`
   and `orchestrator`:

   advisor (default) — host/executor-led:
   user -> executor -> frontier advice (when needed) -> executor -> tests
   Lower frontier-token use and coordination cost. Best for most coding tasks.

   orchestrator — host-led verified orchestration with managed executor bodies:
   user -> host controller -> frontier-dispatch bodies -> host review -> frozen verifier
   Higher coordination cost; use for multi-step work that needs arm/verify/done.

   Optional premium-host pattern (not a third profile):
   user -> premium host model (harness-selected) -> managed frontier consults -> cheaper executor bodies
   Choose `advisor` first for occasional consults, or `orchestrator` when the pattern also needs
   guarded body dispatch and a frozen verifier. The host model is still harness-owned; FrontierFuse
   only manages consults and bodies. Use when judgment stays on a premium host, deep advice is
   occasional, and bulk implementation can be cheaper.

   A plugin cannot hot-swap the host harness model. Until a managed controller exists, orchestrator
   planning remains host-owned; the configured frontier is managed consult capacity.

6. Ask frontier provider alone: codex | claude | grok | gemini. Then:
   frontier-dispatch models --provider <provider>
   Ask frontier model in a separate question. Never invent IDs. Fable/Sonnet/Opus are Claude models,
   not providers. Custom exact IDs only after the provider CLI verifies them.

7. Ask executor provider alone: codex | claude | grok | gemini. Then models --provider <executor>.
   Ask executor model separately. For Codex, empty / account default is recommended. This checkout
   verifies grok-4.5 in the static catalog; do not invent other Grok IDs unless `grok models` or
   official docs expose them.

8. Ask remaining controls separately:
   - Effort: Codex/Grok only (high/medium/low; Codex also xhigh). Omit --effort for Claude/Gemini.
   - Fast mode: off/on.
   - Update mode: passive | manual | off.
   - Scope: session (default) or global (--global).

   Apply only through frontier-dispatch config (never hand-edit config files):
   frontier-dispatch config \
     --profile <advisor|orchestrator> \
     --frontier-provider <codex|claude|grok|gemini> \
     --frontier-model <exact-model-id> \
     --executor <codex|claude|grok|gemini> \
     --executor-model <exact-model-id-or-empty> \
     [--effort <low|medium|high|xhigh>] \
     --fast <on|off> \
     --update-mode <passive|manual|off> \
     [--global]
   `--model` is a legacy alias for `--executor-model` (do not pass both).

9. Re-run config + doctor. Report effective profile, frontier provider/model, executor
   provider/model, doctor exit code, readiness, and any exact missing CLI or auth step. Do not make
   a live inference call unless I ask.

10. Orchestrator loops need a host-approved verifier:
    frontier-dispatch arm --gate "<single test/build/lint argv command>" --cwd "$PWD"
    Implement via frontier-dispatch, review the raw diff, then:
    frontier-dispatch verify
    frontier-dispatch done
    `done` only after fresh snapshot-bound GREEN. Never enable YOLO/bypass without explicit user
    direction. xhigh effort is Codex-only; Grok accepts low/medium/high.

11. Preserve rollback and uninstall:

    Claude marketplace rollback: /plugin marketplace update frontierfuse, select a previously
    published compatible version when supported, restart Claude Code. If version pick is unavailable,
    uninstall and reinstall the last known-good release source — do not edit plugin cache files.
    Uninstall:
    /plugin uninstall frontierfuse@frontierfuse
    /plugin marketplace remove frontierfuse

    Claude Option B uninstall:
    python3 "$FRONTIERFUSE_HOME/frontier_dispatch.py" uninstall-hooks
    Restart Claude Code. Leave or remove the checkout only after no harness still needs it.

    Checkout rollback when last-known-good exists:
    git -C "$FRONTIERFUSE_HOME" switch --detach "$(cat "$HOME/.config/frontier-fuse/last-known-good")"
    Return to updates later:
    git -C "$FRONTIERFUSE_HOME" switch master
    git -C "$FRONTIERFUSE_HOME" pull --ff-only

    Checkout MCP uninstall (host-native remove, then PATH line, then checkout):
    codex mcp remove frontier-advisor
    grok mcp remove frontier-advisor
    gemini mcp remove frontier-advisor
    Restart the harness after MCP removal. Do not delete ~/.config/frontier-fuse/ unless the user
    explicitly wants local config, update cache, backups, and state removed too.
```

---

## Three Practical Working Patterns

FrontierFuse does **not** replace the model already driving your host session. It configures managed
provider calls and the role contract.

### 1. Host / executor-led advisor (default)

```text
user
  -> host executor (plans, edits, tools)
       -> managed frontier advice (only when needed)
  -> host executor continues
  -> tests / review
```

Use for ordinary implementation, refactors, and debugging. Lowest frontier-token use and least
coordination overhead. No arm/disarm loop.

### 2. Host-led verified orchestration (managed executor bodies)

```text
user
  -> host controller (plans, reviews, synthesizes)
       -> frontier-dispatch arm --gate "…"
       -> frontier-dispatch (managed executor bodies)
       -> host reviews raw handoff / diff
       -> frontier-dispatch verify
       -> frontier-dispatch done   # only after fresh GREEN
```

Use for multi-step work that needs a frozen, snapshot-bound verifier. Higher latency and
coordination cost. Claude Code hooks (marketplace or Option B) are an armed workflow guardrail only
on that host surface — not a sandbox.

### 3. Premium host lead + deep frontier advisor + cheaper executor bodies (pattern, not a profile)

```text
user
  -> premium host model (selected in the harness UI / settings; plugin cannot swap it)
       -> managed deep frontier consults (ask_frontier / ask-frontier)
       -> cheaper managed executor bodies (frontier-dispatch)
  -> host integrates evidence
  -> real tests / frozen verify when orchestrating
```

This is not a third `profile` value. Select `advisor` first for occasional managed consults, or
select `orchestrator` when the same pattern also needs guarded body dispatch and a frozen verifier.
Use it when judgment stays on a strong host model, deep advice is occasional, and bulk coding can use
a cheaper executor. The configured frontier remains a **managed consult** until a managed controller
ships.

### Comparison

| Working pattern | Frontier-token use | Latency / coordination | Choose when |
|-|-|-|-|
| Advisor | Low (on-demand consults) | Lowest | Default coding loop |
| Host orchestrator | Medium–high (host planning + body rounds + verify) | Higher (arm/dispatch/verify) | Multi-step work needing GREEN receipts |
| Premium host + deep frontier + cheap bodies | Higher if consults are frequent; body cost can stay low | Highest setup care | Hard judgment + cost control on implementation |

---

## Install, Update, Restart, Rollback, Uninstall

No separate Codex / Grok / Gemini **native marketplace packages** are claimed. Claude Code has the
marketplace plugin; other harnesses use a shared checkout plus optional MCP.

### Claude Code — marketplace (primary)

| Action | Commands |
|-|-|
| Install | `/plugin marketplace add Renn-Labs/FrontierFuse` then `/plugin install frontierfuse@frontierfuse` |
| Update | `/plugin marketplace update frontierfuse` then `/plugin update frontierfuse@frontierfuse` |
| Reload / restart | `/reload-plugins` after skill-only changes; **fully restart Claude Code** after hook or MCP-related install/update |
| Rollback | Prefer marketplace version selection when available; otherwise uninstall and reinstall last known-good source (do not hand-edit plugin caches) |
| Uninstall | `/plugin uninstall frontierfuse@frontierfuse` then `/plugin marketplace remove frontierfuse` |

### Claude Code — Option B (manual hooks)

Use when marketplace install is unavailable. From a stable checkout:

```bash
export FRONTIERFUSE_HOME="${FRONTIERFUSE_HOME:-$HOME/.local/share/FrontierFuse}"
python3 "$FRONTIERFUSE_HOME/frontier_dispatch.py" install-hooks
# Fully restart Claude Code so PreToolUse/Stop hooks load.
python3 "$FRONTIERFUSE_HOME/frontier_dispatch.py" uninstall-hooks   # reverse
```

`install-hooks` merges into `~/.claude/settings.json` (backup `.json.bak`). See
`settings.hooks.snippet.json` for the inert shape. Hooks do nothing until
`frontier-dispatch arm --gate "…"`.

### Codex / Grok Build / Gemini — shared checkout + MCP

Shared install/update/rollback shell:

```bash
export FRONTIERFUSE_HOME="$HOME/.local/share/FrontierFuse"
if [ -d "$FRONTIERFUSE_HOME/.git" ]; then
  mkdir -p "$HOME/.config/frontier-fuse"
  git -C "$FRONTIERFUSE_HOME" rev-parse HEAD > "$HOME/.config/frontier-fuse/last-known-good"
  (cd "$FRONTIERFUSE_HOME" && git pull --ff-only)
else
  git clone https://github.com/Renn-Labs/FrontierFuse.git "$FRONTIERFUSE_HOME"
fi
export PATH="$FRONTIERFUSE_HOME/bin:$PATH"
# Persist PATH once in ~/.bashrc or ~/.zshrc as appropriate.
```

| Harness | Add MCP (verified) | Remove MCP (verified) | After change |
|-|-|-|-|
| Codex | `codex mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"` | `codex mcp remove frontier-advisor` | Restart Codex session |
| Grok Build | `grok mcp add frontier-advisor -- python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"` | `grok mcp remove frontier-advisor` | Restart Grok Build session |
| Gemini CLI | `gemini mcp add frontier-advisor python3 "$FRONTIERFUSE_HOME/frontier_advisor_mcp.py"` | `gemini mcp remove frontier-advisor` | Restart Gemini CLI session |

Gemini CLI takes `<name> <commandOrUrl> [args...]` positionally, so its verified stdio form
intentionally has no `--` separator.

Checkout rollback:

```bash
git -C "$FRONTIERFUSE_HOME" switch --detach "$(cat "$HOME/.config/frontier-fuse/last-known-good")"
# later:
git -C "$FRONTIERFUSE_HOME" switch master
git -C "$FRONTIERFUSE_HOME" pull --ff-only
```

Uninstall checkout path: remove MCP → remove PATH line → remove checkout only if no harness still
uses it. Delete `~/.config/frontier-fuse/` only on explicit request (config, cache, backups, state).

---

## Doctor, Updates, And Exit Codes

| Command | Network? | Purpose |
|-|-|-|
| `frontier-dispatch doctor` | No (offline) | Typed readiness: config, locks, body CLI, frontier CLI, hooks/plugin presence, release status row |
| `frontier-dispatch doctor --json` | No | Same report as JSON (`ready`, `blocking`, `next_step`) |
| `frontier-dispatch doctor --check-updates` | Yes (explicit) | Full doctor **plus** public release-metadata check |
| `frontier-dispatch update --check` | Yes (unless mode/cache blocks) | Cached privacy-preserving release check only |
| `frontier-dispatch update --check --force` | Yes | Bypass cache / disabled-mode restrictions for an explicit check |
| `frontier-dispatch update --check --passive` | Conditional | Honors `passive` mode; silent when current/offline |

**Doctor exit codes**

| Code | Meaning |
|-|-|
| `0` | READY — blocking prerequisites present (body + frontier CLIs, usable global lock). Optional hooks/release rows can still show gaps. |
| `1` | NOT READY — missing blocking CLI, unusable lock path, unwritable state, etc. |
| `2` | CONFIG_INVALID or invalid `FRONTIER_SESSION_ID` — repair or fix identity before continuing. |

Ordinary doctor validates local structure and **CLI presence only**. It does **not** prove login,
billing, model entitlement, or live provider compatibility. Offline tests and `--dry-run` work
regardless of auth.

An exit code of `0` means only local CLI/state readiness. It does not mean every configuration choice
has been made or that a selected model is authorized.

**Availability suggestions** from `frontier-dispatch models` (catalog + local discovery) are
non-mutating recommendations. They are **not** entitlement probes.

Passive update reminders run at most weekly during **explicit** FrontierFuse use, use an owner-only
seven-day cache, send no machine or project data, stay silent when current or offline, and never
install automatically. Modes: `passive` | `manual` | `off`.

---

## Model Sources

`frontier-dispatch models` combines a maintained catalog with local CLI discovery where supported.
Catalog membership is not proof that the current account is authenticated or entitled.

Verified examples (not exhaustive): GPT-5.6 Sol/Terra/Luna; Claude Fable/Sonnet/Opus family IDs;
`grok-4.5`; Gemini 3.5/3.1/2.5 options. Codex executor default is deliberately **unpinned** (empty →
CLI account-aware model).

- [OpenAI models](https://developers.openai.com/api/docs/models/all)
- [Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- Grok account availability: `grok models`

---

## Operational Contract

| Piece | Purpose |
|-|-|
| `/frontierfuse` | Main Claude Code skill |
| `/frontierfuse-config` | Sequential guided configuration |
| `ask_frontier` / `ask-frontier` | On-demand managed frontier advice |
| `frontier-dispatch models` | Catalog + local discovery |
| `frontier-dispatch doctor [--json]` | Offline typed readiness + recovery actions |
| `frontier-dispatch config --repair [--global]` | Backed-up malformed config/state recovery |
| `frontier-dispatch config --inherit-fast-model` | Codex fast mode inherits regular model pin |
| `frontier-dispatch arm --gate` | Freeze orchestrator verifier argv/cwd |
| `frontier-dispatch verify` | Run frozen verifier (snapshot-bound) |
| `frontier-dispatch update --check` | Privacy-preserving cached release check |

Precedence: per-call > session > `~/.config/frontier-fuse/config.json` > environment > defaults.
Config/session writes are schema-versioned, atomic, owner-only, and advisory-locked on supported
Linux/macOS. Invalid persisted values fail closed; explicit repair preserves a timestamped backup.

Doctor JSON marks each check with `blocking`. Missing optional Claude hooks or offline update status
does not by itself make another harness's provider execution unready.

Provider permissions are inherited by default. Elevated autonomy is explicit only:

```bash
export FRONTIER_CODEX_YOLO=1
export FRONTIER_GROK_YOLO=1
# or FRONTIER_GROK_PERMISSION_MODE=<mode>
```

Cross-provider prompts leave the machine and are subject to provider terms. Never commit `runs/`,
`verdict.json`, provider transcripts, `.omx/`, `.omc/`, credentials, or local state. No telemetry is
sent by ordinary doctor or passive update checks.

---

## Maintainer Verification

```bash
git config core.hooksPath githooks   # once per clone; required for pre-push hook
python3 tests/run_contracts.py
claude plugin validate .
scripts/pre-push-check.sh
python3 scripts/public-release-scrub.py --all-history
```

The release gate checks synchronized versions (plugin, marketplace, MCP server, update module),
public-data scrub rules, model-name policy, Python 3.10/3.12 contracts, plugin validation, portable
shims, provider dry-runs, and doctor output. Agents (Claude Code, Codex, Grok) must run the same
gate before public origin push/tag/release and must not use `git push --no-verify`. See `AGENTS.md`
and `docs/PUBLIC_RELEASE_CHECKLIST.md`.
MIT licensed. Scrub and handoff helpers are adapted from
[FleetFuse](https://github.com/Renn-Labs/FleetFuse); see `NOTICE`.

## Multi-role topology

Native durable slots remain **frontier** (managed consult) and **executor** (managed body). The host
harness model is never swapped by the plugin.

Add named roles when you hold several models:

```bash
# Premium host (Opus in Claude Code) + Fable frontier + Grok bodies + Sol orchestration consult
frontier-dispatch config --global \
  --profile advisor \
  --frontier-provider claude --frontier-model claude-fable-5 \
  --executor grok --grok-model grok-4.5

frontier-dispatch role set --global --name orchestration_consult --kind consult \
  --role-provider codex --role-model gpt-5.6-sol --role-effort xhigh

frontier-dispatch topology --json   # no token spend
frontier-dispatch consult --role orchestration_consult --dry-run --question "outline the plan"
```

### OpenRouter menagerie

```bash
export OPENROUTER_API_KEY=...   # required for live OpenRouter calls only
frontier-dispatch config --global --executor openrouter --openrouter-model openrouter/auto
frontier-dispatch models --provider openrouter --no-discover --json
```

Context sent to OpenRouter leaves your machine and may be routed to third-party models.
