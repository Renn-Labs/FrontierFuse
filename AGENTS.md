# FableFuse Agent Memory

This file is shared guidance for Codex, Grok, and any other agent that reads repo-local agent
instructions. Claude Code also has `CLAUDE.md`; keep the two aligned.

## Public Release Scrub

Before any public push, release, tag, plugin marketplace update, or repo-publication step:

1. Run `scripts/pre-push-check.sh`.
2. Run `scripts/public-release-scrub.py --all-history` before first public exposure or after any
   history rewrite.
3. Confirm `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `CHANGELOG.md`, and
   README install/upgrade docs all reflect the same release.
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

Exact verified current IDs for this project (as of 0.2.6 guidance):

| Role | ID / policy |
|-|-|
| Fable (advisor/brain) | `claude-fable-5` |
| Sonnet body | `claude-sonnet-5` |
| Opus body | `claude-opus-4-8` — do not write an Opus major-version model ID unless official Anthropic docs list it |
| Grok body | `grok-4.5` via Grok Build CLI — verify xAI IDs before changing defaults or claims |
| Codex body | deliberately **unpinned** (empty default; Codex CLI account-aware default) |

**0.2.6 note — GPT-5.6:** `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` are
**limited-preview** IDs for entitled organizations only. Never make them a product default, never
imply general availability, and never imply availability in ChatGPT or for all accounts. Optional
pinning is for entitled orgs only.

Do not claim separate Codex/Grok plugin packages ship unless maintainers have published them.

Pushing, tagging, publishing packages, making the repo public, or adding live-provider CI remains a
maintainer-gated action.
