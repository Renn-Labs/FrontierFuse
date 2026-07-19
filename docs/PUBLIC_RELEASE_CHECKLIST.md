# Public Release Checklist

Use this checklist before any public push, tag, release, marketplace update, or repo-publication step.
It is intentionally model-neutral so Claude Code, Codex, Grok, and other agents all follow the same
release memory.

## Required Gates

```bash
git config core.hooksPath githooks   # once per clone; enables tracked pre-push hook
scripts/pre-push-check.sh
scripts/public-release-scrub.py --all-history
```

`githooks/pre-push` always execs `scripts/pre-push-check.sh`. If `core.hooksPath` is unset, a
normal `git push` will not run the gate — set it before public work.

`scripts/pre-push-check.sh` includes the normal push-range scrub scan. The explicit `--all-history`
scan is required before first public exposure or after rewriting history.

## Agent rules (Claude Code, Codex, Grok, …)

When an agent is asked to push, tag, or publish publicly:

1. Set `git config core.hooksPath githooks` if not already set.
2. Run `scripts/pre-push-check.sh` and fix failures until it prints success.
3. Run `scripts/public-release-scrub.py --all-history` when required (first exposure / history rewrite / release).
4. **Never** use `git push --no-verify` (or equivalent) to skip hooks for public origin work.
5. **Never** treat `--maintainer-escape` or `FRONTIER_SKIP_PRE_PUSH=1` as valid for public
   push/tag/release. The escape is loud, still runs denylist + version metadata + scrub, and is
   only for non-release debugging.
6. Public pushes must be from `main` or `master`.
7. Do not print secret values from scrub output.

These rules are also in `AGENTS.md` and `CLAUDE.md` so Claude Code and Codex both see them.

## Current Public-Exposure Status

The repository is public. Keep `scripts/public-release-scrub.py --all-history` green before further
public exposure of rewritten history. Local history was previously rewritten during `0.2.2`
live-prep to remove token-shaped scrubber test fixtures.

## Public Scrub Rules

- Never print matched secret values. Report only file, line, commit scope, and finding type.
- Keep `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`, `.grokprint/`,
  `.buildlog/`, local config/state, credentials, and private absolute paths out of git.
- If scanner output points at a real credential, rotate or revoke it before doing anything else.
- If the credential is in local history that has not been pushed publicly, scrub/rewrite history
  before pushing. Do not rewrite published history without maintainer approval.
- Test fixtures must not contain complete token-shaped literals. Build fake values from pieces so
  public scanners do not flag the repository.

## Release Hygiene

- Bump `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
  `frontier_advisor_mcp.py`, and `frontier_update.py` together.
- Move changelog notes out of `Unreleased` into a dated version section.
- Update README install/upgrade instructions for user-facing behavior or configuration changes.
- Verify any current-market model IDs or availability claims against official provider docs. Do not
  infer unreleased family names. Keep provider and model separate: Codex, Claude, Grok, Gemini, and
  OpenRouter are provider values; Fable, Sonnet, Opus, GPT-5.6, Grok releases, Gemini releases, and
  OpenRouter model IDs are models. The source-backed catalog lives in `frontier_models.py`;
  local/account-specific Grok IDs must come from `grok models` or official documentation.
- Validate plugin metadata with `claude plugin validate .`.
- Run keyless/offline tests only unless a maintainer explicitly approves live-provider validation.

## What the pre-push gate runs

Always (including offline CI subset):

- tracked artifact denylist
- four-file version metadata sync
- public-release scrub
- market model-name policy
- whitespace check
- byte-compile of shipped modules (including `frontier_topology.py`, `frontier_openrouter.py`)
- offline contracts + runner self-test

Full local gate also runs plugin validate, foundation smoke, provider dry-runs, and doctor.

CI backstop: `.github/workflows/offline.yml` runs the offline pre-push equivalent on push/PR to
`main`/`master`. That does not replace running the local gate before a public push.

## Human-Gated Actions

Creating or changing remotes, pushing, tagging releases, publishing packages, and adding
live-provider CI remain maintainer-authorized. Once authorized, agents must still run the Required
Gates above and honor the agent hard bans (no `--no-verify`, no silent skip, no maintainer-escape
for public release).
