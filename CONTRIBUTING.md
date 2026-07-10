# Contributing to FrontierFuse

Thanks for your interest. FrontierFuse is small, stdlib-only, and offline-testable on purpose — keep it
that way.

## Ground rules

- **Python 3.10+, standard library only** in shipped modules. No third-party runtime deps.
- **Keyless & offline.** Tests, CI, and ordinary doctor runs must use no API keys and no live model
  calls. `doctor --check-updates` is the only explicit release-metadata network path. Use the
  `FRONTIER_CODEX_CMD` / `FRONTIER_ADVISOR_CMD` overrides (e.g. `echo`) to stub engines.
- **Don't fork the contract.** Shared config/state/verdict/command-builder logic lives in
  `frontier_common.py`. Import it; don't duplicate it.
- **Preserve the invariants** in `CLAUDE.md` — especially: the loop closes only on a fresh
  deterministic GREEN, and the workflow guardrail stays narrowed + kill-switchable.

## Dev loop

```bash
git config core.hooksPath githooks       # enable the tracked pre-push release gate
python3 tests/run_contracts.py       # every offline contract suite must pass
python3 frontier_common.py              # sanity: effective config + built commands
python3 frontier_dispatch.py doctor     # readiness
```

For anything with runtime behaviour, drive the real CLI (dispatch `--dry-run`, feed the hooks
synthetic JSON on stdin, run `verify --gate "true"` / `"false"`) — don't rely on unit tests alone.

Before any public branch push, `scripts/pre-push-check.sh` must pass. It enforces version/changelog/
README install hygiene, public scrub candidates, plugin validation, compile checks, offline
contracts, CI branch coverage, and the Opus-lead dry-run smoke. Before first public exposure or after
history rewrites, also run `scripts/public-release-scrub.py --all-history`.

## Pull requests

- Keep PRs focused. Update `tests/frontier_contracts.py` when you change a contract.
- Bump `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` together, then update
  `README.md` / `CHANGELOG.md` when behaviour or config changes.
- Be precise in claims. FrontierFuse coordinates a body engine and preserves verification artifacts;
  it does not guarantee correctness.

## What maintainers gate

Creating the GitHub remote, pushing, tagging releases, publishing packages, making the repo public,
or adding a live-provider CI gate are explicit maintainer actions — please don't include them in PRs.

By contributing you agree your contributions are licensed under the MIT License (`LICENSE`).
