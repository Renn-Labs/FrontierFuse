# Public Release Checklist

Use this checklist before any public push, tag, release, marketplace update, or repo-publication step.
It is intentionally model-neutral so Claude Code, Codex, Grok, and other agents all follow the same
release memory.

## Required Gates

```bash
git config core.hooksPath githooks
scripts/pre-push-check.sh
scripts/public-release-scrub.py --all-history
```

`scripts/pre-push-check.sh` includes the normal push-range scrub scan. The explicit `--all-history`
scan is required before first public exposure or after rewriting history.

## Current Public-Exposure Status

During the `0.2.2` live-prep work, local history was rewritten to remove token-shaped scrubber test
fixtures from earlier commits. `scripts/public-release-scrub.py --all-history` must stay green before
any public exposure.

## Public Scrub Rules

- Never print matched secret values. Report only file, line, commit scope, and finding type.
- Keep `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`, `.buildlog/`, local
  config/state, credentials, and private absolute paths out of git.
- If scanner output points at a real credential, rotate or revoke it before doing anything else.
- If the credential is in local history that has not been pushed publicly, scrub/rewrite history
  before pushing. Do not rewrite published history without maintainer approval.
- Test fixtures must not contain complete token-shaped literals. Build fake values from pieces so
  public scanners do not flag the repository.

## Release Hygiene

- Bump `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` together.
- Move changelog notes out of `Unreleased` into a dated version section.
- Update README install/upgrade instructions for user-visible behavior or configuration changes.
- Verify any current-market model IDs or availability claims against official provider docs. Do not
  infer unreleased family names; current Opus executor defaults must remain `claude-opus-4-8` unless
  official Anthropic docs say otherwise. Current Grok executor defaults must remain `grok-4.5`
  unless official xAI docs say otherwise.
- Validate plugin metadata with `claude plugin validate .`.
- Run keyless/offline tests only unless a maintainer explicitly approves live-provider validation.

## Human-Gated Actions

Creating or changing remotes, pushing, tagging, publishing packages, making the repo public, and
adding live-provider CI all require explicit maintainer approval.
