# Contributing to FrontierFuse

Thanks for your interest. FrontierFuse is small, stdlib-only, and offline-testable on purpose — keep it
that way.

## Ground rules

- **Python 3.10+, standard library only** in shipped modules. No third-party runtime deps.
- **Keyless & offline.** Tests, CI, and ordinary doctor runs must use no API keys and no live model
  calls. `doctor --check-updates` is the only explicit doctor network path; `update --check` is the
  standalone release-metadata path. Use the `FRONTIER_CODEX_CMD` / `FRONTIER_ADVISOR_CMD` overrides
  (e.g. `echo`) to stub engines.
- **Don't fork the contract.** Shared config/state/verdict/command-builder logic lives in
  `frontier_common.py`. Import it; don't duplicate it.
- **Preserve the invariants** in `CLAUDE.md` / `AGENTS.md` — especially: the loop closes only on a
  fresh deterministic GREEN, and the workflow guardrail stays narrowed + kill-switchable.
- **Model accuracy.** Never invent model IDs or call a model a provider. Verify IDs against official
  provider docs and `frontier_models.py` before documenting them.

## Four-file version contract

When shipping a version bump, keep these **four** carriers equal, then update docs:

1. `.claude-plugin/plugin.json` (`version`)
2. `.claude-plugin/marketplace.json` (top-level and plugin-entry `version`)
3. `frontier_advisor_mcp.py` (`SERVER_VERSION`)
4. `frontier_update.py` (`CURRENT_VERSION`)

Also update `CHANGELOG.md`, README install/upgrade/doctor text, and skills in the same release.
Do not bump version fields in a docs-only lane unless the release intentionally ships a new version.

## Mandatory public scrub

Before any public branch push, tag, marketplace update, or first public exposure:

1. `scripts/pre-push-check.sh` must pass (version sync, install hygiene, scrub candidates, plugin
   validation, compile, offline contracts, CI branch coverage, dry-run smokes).
2. Before first public exposure or after any history rewrite, also run
   `scripts/public-release-scrub.py --all-history`.
3. Do not print matched secret values. Report only file, line, commit scope, and finding type.
4. Never commit `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`, `.grokprint/`,
   credentials, or private absolute paths.

Creating the GitHub remote, pushing, tagging releases, publishing packages, making the repo public,
or adding live-provider CI remains a maintainer-gated action.

## Dev loop

```bash
git config core.hooksPath githooks       # enable the tracked pre-push release gate
python3 tests/run_contracts.py       # every offline contract suite must pass
python3 frontier_common.py              # sanity: effective config + built commands
python3 frontier_dispatch.py doctor     # readiness (exit 0 ready, 1 not ready, 2 config/session invalid)
```

For anything with runtime behaviour, drive the real CLI (dispatch `--dry-run`, feed the hooks
synthetic JSON on stdin, run `verify --gate "true"` / `"false"`) — don't rely on unit tests alone.

## Pull requests

- Keep PRs focused. Update the relevant `tests/*_contracts.py` suite when you change a contract.
- For behaviour or config changes: bump the four version carriers together, then update
  `README.md` / `CHANGELOG.md` / skills.
- Be precise in claims. FrontierFuse coordinates a body engine and preserves verification artifacts;
  it does not guarantee correctness. Do not claim Codex/Grok/Gemini native marketplace packages
  unless maintainers have published them.
- Docs-only lanes may adjust README, design, execution plan, skills, hook snippets, and lightweight
  docs contracts without touching Python sources or version fields.

By contributing you agree your contributions are licensed under the MIT License (`LICENSE`).
