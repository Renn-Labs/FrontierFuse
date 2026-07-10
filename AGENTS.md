# FrontierFuse Agent Memory

This file is shared guidance for Codex, Grok, and any other agent that reads repo-local agent
instructions. Claude Code also has `CLAUDE.md`; keep the two aligned.

## Public Release Scrub

Before any public push, release, tag, plugin marketplace update, or repo-publication step:

1. Run `scripts/pre-push-check.sh`.
2. Run `scripts/public-release-scrub.py --all-history` before first public exposure or after any
   history rewrite.
3. Bump `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
   `frontier_advisor_mcp.py`, and `frontier_update.py` together. Confirm `CHANGELOG.md`, README
   install/upgrade/doctor docs, and skills all reflect the same release.
4. Do not print matched secret values in chat, logs, issues, or commits. Report only file, line, and
   finding type.
5. Never commit `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`,
   `.grokprint/`, `.buildlog/`, local config/state, credentials, or private absolute paths.
6. If a real secret is found, stop release work, rotate/revoke the secret, scrub local history before
   pushing, and document only the remediation status.

## Market Model Accuracy

Before changing default model IDs, README claims, skill text, marketplace metadata, or examples for
current provider models, verify the exact model IDs against official provider documentation. Do not
infer unreleased family names.

Exact verified IDs and policies for this project (as of 0.3.0 guidance):

| Role | ID / policy |
|-|-|
| Claude frontier default | `claude-fable-5` |
| Claude executor default | `claude-sonnet-5`; selectable `claude-opus-4-8` |
| Grok | `grok-4.5` via Grok Build CLI; merge account-visible IDs from `grok models` |
| Gemini executor default | `gemini-3.5-flash`; catalog includes official current/previous IDs |
| Codex executor default | deliberately **unpinned** (empty; Codex CLI account-aware default) |
| GPT-5.6 catalog | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |

Use `frontier_models.py` as the curated catalog and keep its source URLs current. Provider and model
are separate concepts: `codex|claude|grok|gemini` are providers; Fable, Sonnet, Opus, GPT-5.6,
Grok releases, and Gemini releases are models. Never add an account-specific ID such as a requested
Grok version to the static catalog until official documentation or the installed CLI verifies it.

Do not claim separate Codex/Grok/Gemini plugin packages ship unless maintainers have published them.

Update reminders remain privacy-preserving and non-blocking: ordinary doctor is offline; passive
checks run only during explicit FrontierFuse use, use the owner-only seven-day cache, and never install
automatically. Keep Claude, Codex, Grok, and Gemini install/update/restart/uninstall docs synchronized.

Pushing, tagging, publishing packages, making the repo public, or adding live-provider CI remains a
maintainer-gated action.
