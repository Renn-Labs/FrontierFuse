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
5. Never commit `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`, `.buildlog/`,
   local config/state, credentials, or private absolute paths.
6. If a real secret is found, stop release work, rotate/revoke the secret, scrub local history before
   pushing, and document only the remediation status.

Pushing, tagging, publishing packages, making the repo public, or adding live-provider CI remains a
maintainer-gated action.
