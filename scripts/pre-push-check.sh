#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

ALLOW_DIRTY=0
if [[ "${1:-}" == "--allow-dirty" ]]; then
  ALLOW_DIRTY=1
fi

fail() {
  echo "pre-push: FAIL: $*" >&2
  exit 1
}

step() {
  echo "pre-push: $*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

if [[ "${FRONTIER_SKIP_PRE_PUSH:-}" == "1" ]]; then
  echo "pre-push: skipped because FRONTIER_SKIP_PRE_PUSH=1"
  exit 0
fi

need_cmd git
need_cmd python3
need_cmd claude

branch="$(git branch --show-current)"
[[ -n "$branch" ]] || fail "detached HEAD; push from an explicit release branch"
case "$branch" in
  main|master) ;;
  *) fail "public pushes must go from main/master; current branch is $branch" ;;
esac

if [[ "$ALLOW_DIRTY" != "1" ]] && [[ -n "$(git status --porcelain)" ]]; then
  git status --short >&2
  fail "worktree has uncommitted changes"
fi

bad_tracked="$(
  git ls-files | grep -E '(^runs/|^verdict\.json$|(^|/)__pycache__/|\.pyc$|^\.omc/|^\.omx/|^\.grokprint/|^\.buildlog/)' || true
)"
[[ -z "$bad_tracked" ]] || fail "generated/private files are tracked: $bad_tracked"

step "release metadata"
BRANCH="$branch" python3 - <<'PY'
import json
import os
import re
import subprocess
import sys
from pathlib import Path

errors = []
plugin = json.loads(Path(".claude-plugin/plugin.json").read_text())
market = json.loads(Path(".claude-plugin/marketplace.json").read_text())
version = plugin.get("version", "")
plugin_entry = (market.get("plugins") or [{}])[0]

if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    errors.append(f"plugin version is not semver: {version!r}")
if market.get("version") != version:
    errors.append("marketplace top-level version does not match plugin.json")
if plugin_entry.get("version") != version:
    errors.append("marketplace plugin entry version does not match plugin.json")

mcp_source = Path("frontier_advisor_mcp.py").read_text()
mcp_version = re.search(r'^SERVER_VERSION\s*=\s*["\x27]([^"\x27]+)["\x27]', mcp_source, re.MULTILINE)
if not mcp_version or mcp_version.group(1) != version:
    errors.append("frontier_advisor_mcp.py SERVER_VERSION does not match plugin.json")

update_source = Path("frontier_update.py").read_text()
update_version = re.search(r'^CURRENT_VERSION\s*=\s*["\x27]([^"\x27]+)["\x27]', update_source, re.MULTILINE)
if not update_version or update_version.group(1) != version:
    errors.append("frontier_update.py CURRENT_VERSION does not match plugin.json")

changelog = Path("CHANGELOG.md").read_text()
if f"## [{version}] - " not in changelog:
    errors.append(f"CHANGELOG.md is missing a dated {version} entry")

readme = Path("README.md").read_text()
for needle in (
    "/plugin marketplace add Renn-Labs/FrontierFuse",
    "/plugin install frontierfuse@frontierfuse",
    "/reload-plugins",
    "frontier-dispatch arm --gate",
    "frontier-dispatch verify",
    "frontier-dispatch doctor --check-updates",
    "frontier-dispatch update --check",
    "codex mcp add frontier-advisor",
    "grok mcp add frontier-advisor",
    "git pull --ff-only",
    version,
):
    if needle not in readme:
        errors.append(f"README.md install/upgrade docs are missing {needle!r}")

truth_surface = "\n".join(
    Path(path).read_text()
    for path in (
        "README.md",
        "SECURITY.md",
        "docs/DESIGN.md",
        "skills/frontierfuse/SKILL.md",
        "skills/frontierfuse-config/SKILL.md",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "hooks/hooks.json",
        "settings.hooks.snippet.json",
        "CONTRIBUTING.md",
        "CLAUDE.md",
        "AGENTS.md",
        "docs/PUBLIC_RELEASE_CHECKLIST.md",
    )
).lower()
for stale_claim in ("hard gate", "hard-gated", "cost-optimal"):
    if stale_claim in truth_surface:
        errors.append(f"public product surface still contains stale claim {stale_claim!r}")

workflow = Path(".github/workflows/offline.yml").read_text()
branch = os.environ["BRANCH"]
match = re.search(r"branches:\s*\[([^\]]+)\]", workflow)
branches = [b.strip() for b in match.group(1).split(",")] if match else []
if branch not in branches:
    errors.append(f"offline CI push branches {branches!r} do not include current branch {branch!r}")

def semver_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]

upstream = subprocess.run(
    ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
    capture_output=True,
    text=True,
)
if upstream.returncode == 0:
    upstream_ref = upstream.stdout.strip()
    old = subprocess.run(
        ["git", "show", f"{upstream_ref}:.claude-plugin/plugin.json"],
        capture_output=True,
        text=True,
    )
    if old.returncode == 0 and old.stdout.strip():
        old_version = json.loads(old.stdout).get("version", "")
        if re.fullmatch(r"\d+\.\d+\.\d+", old_version):
            ahead = subprocess.run(
                ["git", "rev-list", "--count", f"{upstream_ref}..HEAD"],
                capture_output=True,
                text=True,
            )
            has_local_commits = ahead.returncode == 0 and int(ahead.stdout.strip() or "0") > 0
            if has_local_commits and semver_tuple(version) <= semver_tuple(old_version):
                errors.append(
                    f"version must be bumped above upstream {old_version}; current is {version}"
                )

if errors:
    for error in errors:
        print(f"pre-push: {error}", file=sys.stderr)
    sys.exit(1)
print(f"pre-push: release metadata ok ({version})")
PY

step "public release scrub"
python3 scripts/public-release-scrub.py --push-range

step "market model names"
if git grep -n -E 'claude-opus-5|Opus 5|grok-5|Grok 5|gpt-5\.6-soul|GPT-5\.6 Soul' -- \
  '*.py' '*.md' '*.json' '*.sh' \
  ':!scripts/pre-push-check.sh' \
  ':!CHANGELOG.md' >/tmp/frontier-model-name-grep.$$; then
  cat /tmp/frontier-model-name-grep.$$ >&2
  rm -f /tmp/frontier-model-name-grep.$$
  fail "found an unverified or misspelled model reference"
fi
rm -f /tmp/frontier-model-name-grep.$$

step "whitespace"
git diff --check

step "byte compile"
python3 -m compileall -q \
  frontier_common.py frontier_advisor.py frontier_advisor_mcp.py frontier_dispatch.py frontier_models.py frontier_update.py \
  frontier_verify.py frontier_scrub.py hooks tests

step "offline contracts (aggregate)"
python3 tests/run_contracts.py

step "contract runner self-test"
python3 tests/run_contracts.py --self-test

step "plugin validation"
claude plugin validate .

step "foundation smoke"
python3 frontier_common.py >/dev/null

step "portable command shims"
bin/frontier-dispatch --help >/dev/null
FRONTIER_ADVISOR_CMD=echo bin/ask-frontier "pre-push advisor shim smoke" >/dev/null

step "claude executor with opus model dry-run smoke"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
FRONTIER_CONFIG_DIR="$tmpdir/config" FRONTIER_STATE_DIR="$tmpdir/state" FRONTIER_RUNS_DIR="$tmpdir/runs" \
  python3 frontier_dispatch.py --dry-run --executor claude --model claude-opus-4-8 \
  "pre-push smoke: Claude executor with Opus model" >/dev/null

step "grok lead dry-run smoke"
grok_smoke="$(
  FRONTIER_CONFIG_DIR="$tmpdir/config" FRONTIER_STATE_DIR="$tmpdir/state" FRONTIER_RUNS_DIR="$tmpdir/runs" \
    python3 frontier_dispatch.py --dry-run --executor grok --grok-model grok-4.5 \
    "pre-push smoke: Grok executor with Fable advisor"
)"
printf '%s\n' "$grok_smoke" | grep -q 'grok --model grok-4.5' || fail "grok smoke did not select grok-4.5"
printf '%s\n' "$grok_smoke" | grep -q -- '--prompt-file <prompt-file>' || fail "grok smoke did not use prompt-file"

step "gemini executor dry-run smoke"
gemini_smoke="$(
  FRONTIER_CONFIG_DIR="$tmpdir/config" FRONTIER_STATE_DIR="$tmpdir/state" FRONTIER_RUNS_DIR="$tmpdir/runs" \
    python3 frontier_dispatch.py --dry-run --executor gemini --gemini-model gemini-3.5-flash \
    "pre-push smoke: Gemini executor with Fable advisor"
)"
printf '%s\n' "$gemini_smoke" | grep -q 'gemini --model gemini-3.5-flash' \
  || fail "gemini smoke did not select gemini-3.5-flash"

# Doctor never hits live providers. Exit 1 means the configured body CLI is not
# on PATH (NOT READY) — common on partial installs and not a release-scrub
# failure. Exit 0 (READY) or 1 (NOT READY) are accepted; any other code fails.
# Output must include an explicit readiness line so a silent crash cannot pass.
step "doctor (readiness; exit 1 = body CLI missing, non-blocking)"
set +e
doctor_out="$(python3 frontier_dispatch.py doctor 2>&1)"
doctor_rc=$?
set -e
printf '%s\n' "$doctor_out"
printf '%s\n' "$doctor_out" | grep -qE 'READY|NOT READY' \
  || fail "doctor did not print READY/NOT READY status"
if [[ "$doctor_rc" -ne 0 && "$doctor_rc" -ne 1 ]]; then
  fail "doctor exited with unexpected code $doctor_rc (expected 0=READY or 1=NOT READY)"
fi
if [[ "$doctor_rc" -eq 1 ]]; then
  echo "pre-push: doctor NOT READY (body CLI missing) — continuing; offline contracts already passed"
fi

step "all checks passed"
