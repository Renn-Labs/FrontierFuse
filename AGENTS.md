# FrontierFuse Agent Memory

This file is shared guidance for Codex, Claude Code, Grok, and any other agent that reads
repo-local agent instructions. Keep `AGENTS.md` and `CLAUDE.md` aligned on release gates.

## Public Push Gate (MANDATORY for every agent)

Before any **public** `git push` to GitHub (`origin`), tag, GitHub release, plugin marketplace
update, or repo-publication step, every agent (Claude Code, Codex, Grok, etc.) MUST:

1. Ensure the tracked hook is active in this clone:
   ```bash
   git config core.hooksPath githooks
   ```
   `githooks/pre-push` always runs `scripts/pre-push-check.sh`. Without `hooksPath`, a normal
   `git push` will skip the gate.
2. Run the gate explicitly before pushing (do not rely only on the hook):
   ```bash
   scripts/pre-push-check.sh
   ```
3. Before first public exposure of a branch tip, after any history rewrite, or when releasing:
   ```bash
   scripts/public-release-scrub.py --all-history
   ```
4. Bump these **four** version carriers together and keep them equal:
   - `.claude-plugin/plugin.json`
   - `.claude-plugin/marketplace.json`
   - `frontier_advisor_mcp.py` (`SERVER_VERSION`)
   - `frontier_update.py` (`CURRENT_VERSION`)
   Confirm `CHANGELOG.md`, README install/upgrade/doctor docs, and skills match the same release.
5. Never print matched secret values. Report only file, line, commit scope, and finding type.
6. Never commit `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`,
   `.grokprint/`, `.buildlog/`, local config/state, credentials, or private absolute paths.
7. If a real secret is found, **stop**: rotate/revoke it, scrub local history if needed, and only
   then continue. Document remediation status only — not the secret.

### Hard bans for public push/tag/release

- **Do not** use `git push --no-verify` (or any equivalent hook skip) for public `origin` pushes,
  tags, or releases. If the hook blocks, fix the failure; do not bypass it.
- **Do not** use `FRONTIER_SKIP_PRE_PUSH=1` alone — it fails closed.
- **Do not** use `--maintainer-escape` for public push/tag/release. That escape only skips optional
  local smokes and still runs denylist, version metadata, and scrub; it is **not** a public-release
  bypass.
- Public pushes must be from **`main` or `master`** (the pre-push script enforces this).
- CI (`.github/workflows/offline.yml`) is a **backstop**, not a substitute for running the local
  gate before you push.

### What the gate runs

`githooks/pre-push` → `scripts/pre-push-check.sh`, which always includes:

- tracked artifact denylist (`scripts/release_denylist.py`)
- four-file version metadata sync
- public-release scrub (push-range; use `--all-history` for full release)
- market model-name policy
- whitespace check
- byte-compile of shipped modules (including `frontier_topology.py`, `frontier_openrouter.py`)
- offline contract aggregate + runner self-test

Optional local smokes (skipped only with `--offline`/`--ci` or loud `--maintainer-escape`):
plugin validate, foundation smoke, provider dry-runs, doctor.

## Market Model Accuracy

Before changing default model IDs, README claims, skill text, marketplace metadata, or examples for
current provider models, verify the exact model IDs against official provider documentation. Do not
infer unreleased family names.

Exact verified IDs and policies for this project (as of 0.3.7 guidance):

| Role | ID / policy |
|-|-|
| Claude frontier default | `claude-fable-5` |
| Claude executor default | `claude-sonnet-5`; selectable `claude-opus-4-8` |
| Grok | `grok-4.5` via Grok Build CLI; merge account-visible IDs from `grok models` |
| Gemini executor default | `gemini-3.5-flash`; catalog includes official current/previous IDs |
| Codex executor default | deliberately **unpinned** (empty; Codex CLI account-aware default) |
| GPT-5.6 catalog | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| OpenRouter | provider `openrouter`; use exact catalog or account-visible IDs only; live calls need `OPENROUTER_API_KEY` |

Use `frontier_models.py` as the curated catalog and keep its source URLs current. Provider and model
are separate concepts: `codex|claude|grok|gemini|openrouter` are providers; Fable, Sonnet, Opus,
GPT-5.6, Grok releases, Gemini releases, and OpenRouter model IDs are models. Never add an
account-specific ID to the static catalog until official documentation or the installed CLI verifies it.

Do not claim separate Codex/Grok/Gemini plugin packages ship unless maintainers have published them.

Update reminders remain privacy-preserving and non-blocking: ordinary doctor is offline; passive
checks run only during explicit FrontierFuse use, use the owner-only seven-day cache, and never install
automatically. Keep Claude, Codex, Grok, and Gemini install/update/restart/uninstall docs synchronized.

Pushing, tagging, and publishing remain maintainer-authorized actions — but when authorized, the
**Public Push Gate** section above is mandatory for every agent.
