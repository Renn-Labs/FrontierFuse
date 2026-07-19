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

1. Enable the tracked hook in this clone (once per clone):
   ```bash
   git config core.hooksPath githooks
   ```
   This makes `git push` run `githooks/pre-push` → `scripts/pre-push-check.sh`.
2. `scripts/pre-push-check.sh` must pass (denylist, version sync, scrub, model-name policy,
   whitespace, compile, offline contracts, plugin validate, dry-run smokes, doctor).
3. Before first public exposure or after any history rewrite, also run
   `scripts/public-release-scrub.py --all-history`.
4. Do not print matched secret values. Report only file, line, commit scope, and finding type.
5. Never commit `runs/`, `verdict.json`, provider transcripts/logs, `.omc/`, `.omx/`, `.grokprint/`,
   credentials, or private absolute paths.
6. **Do not** use `git push --no-verify` for public origin push/tag/release. Fix gate failures.
7. **Do not** use `--maintainer-escape` or silent `FRONTIER_SKIP_PRE_PUSH` for public release work.

Creating the GitHub remote, pushing, tagging releases, publishing packages, or adding live-provider
CI remains a maintainer-authorized action. Once authorized, the gates above are mandatory for
humans and for every agent (Claude Code, Codex, Grok, etc.). See `AGENTS.md` and
`docs/PUBLIC_RELEASE_CHECKLIST.md`.

## Dev loop

```bash
git config core.hooksPath githooks       # enable the tracked pre-push release gate (once per clone)
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
