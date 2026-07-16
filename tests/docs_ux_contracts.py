#!/usr/bin/env python3
"""Lightweight offline contracts for documentation / install UX surfaces.

Docs-only suite: no provider calls, no third-party imports. Guards the public
README/skills/design install narrative, host-bound wording, doctor boundaries,
and markdown fence hygiene for the files this lane is allowed to edit.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontier_dispatch as dispatch  # noqa: E402

DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "docs" / "DESIGN.md",
    ROOT / "docs" / "FRONTIERFUSE_EXECUTION_PLAN.md",
    ROOT / "skills" / "frontierfuse" / "SKILL.md",
    ROOT / "skills" / "frontierfuse-config" / "SKILL.md",
    ROOT / "settings.hooks.snippet.json",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fences_balanced(text: str) -> bool:
    """Count markdown fence openers/closers ignoring indentation."""
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            count += 1
    return count % 2 == 0


def _no_prose_trapped_in_bash_fence(text: str) -> bool:
    """Reject the known skill bug: closing prose about --model still inside ```bash."""
    # Match bash fences that still contain a markdown-inline `--model` explanation line.
    pattern = re.compile(
        r"```bash\n(?:(?!```).)*?`(?:--model)` remains available(?:(?!```).)*?```",
        re.DOTALL,
    )
    return pattern.search(text) is None


def test_settings_hooks_snippet_is_valid_json() -> None:
    path = ROOT / "settings.hooks.snippet.json"
    data = json.loads(_read(path))
    assert "hooks" in data
    assert "PreToolUse" in data["hooks"]
    assert "Stop" in data["hooks"]
    comment = data.get("//", "")
    assert "Option B" in comment or "install-hooks" in comment
    assert "not a sandbox" in comment.lower() or "workflow guardrail" in comment.lower()
    pre_matchers = {e.get("matcher") for e in data["hooks"]["PreToolUse"]}
    assert pre_matchers & {"", "*"}, f"snippet PreToolUse must cover all tools: {pre_matchers!r}"
    stop_matchers = {e.get("matcher") for e in data["hooks"]["Stop"]}
    assert "*" in stop_matchers
    # Official exec form: command=python3, args=[one <REPO> script path], aligned timeout.
    for event in ("PreToolUse", "Stop"):
        for entry in data["hooks"][event]:
            for hook in entry.get("hooks", []):
                if hook.get("type") != "command":
                    continue
                assert hook.get("timeout") == dispatch.HOOK_COMMAND_TIMEOUT, (
                    f"snippet {event} timeout mismatch: {hook!r}"
                )
                assert hook.get("command") == "python3", (
                    f"snippet {event} must use exec-form command=python3: {hook!r}"
                )
                args = hook.get("args")
                assert isinstance(args, list) and len(args) == 1, (
                    f"snippet {event} must have exactly one args path: {hook!r}"
                )
                assert args[0].startswith("<REPO>/hooks/"), (
                    f"snippet {event} args must use <REPO> placeholder: {args!r}"
                )
                # No shell-form script path in the command field.
                assert "/hooks/" not in str(hook.get("command", ""))


def test_markdown_fences_balanced_in_docs_lane() -> None:
    for path in DOC_PATHS:
        if path.suffix == ".json":
            continue
        text = _read(path)
        assert _fences_balanced(text), f"unbalanced markdown fences in {path.relative_to(ROOT)}"


def test_skill_fence_does_not_trap_model_alias_prose() -> None:
    skill = _read(ROOT / "skills" / "frontierfuse" / "SKILL.md")
    assert _no_prose_trapped_in_bash_fence(skill), (
        "skills/frontierfuse/SKILL.md still traps `--model` prose inside a bash fence"
    )
    # The alias note must still exist outside a fence.
    assert "`--model` remains available as a legacy alias" in skill


def test_readme_install_lifecycle_paths() -> None:
    readme = _read(ROOT / "README.md")
    for needle in (
        "/plugin marketplace add Renn-Labs/FrontierFuse",
        "/plugin install frontierfuse@frontierfuse",
        "/plugin marketplace update frontierfuse",
        "/plugin update frontierfuse@frontierfuse",
        "/reload-plugins",
        "/plugin uninstall frontierfuse@frontierfuse",
        "install-hooks",
        "uninstall-hooks",
        "Option B",
        "codex mcp add frontier-advisor",
        "codex mcp remove frontier-advisor",
        "grok mcp add frontier-advisor",
        "grok mcp remove frontier-advisor",
        'gemini mcp add frontier-advisor python3',
        "gemini mcp remove frontier-advisor",
        "intentionally has no `--` separator",
        "last-known-good",
        "git pull --ff-only",
        "0.3.6",
    ):
        assert needle in readme, f"README.md missing install/lifecycle needle {needle!r}"


def test_readme_three_workflows_and_host_bound() -> None:
    readme = _read(ROOT / "README.md")
    for needle in (
        "Three Practical Working Patterns",
        "Host / executor-led advisor",
        "Host-led verified orchestration",
        "Premium host lead + deep frontier advisor + cheaper executor bodies (pattern, not a profile)",
        "cannot hot-swap",
        "managed consult",
        "Comparison",
        "not a third `profile` value",
    ):
        assert needle in readme, f"README.md missing workflow needle {needle!r}"


def test_readme_config_decision_separation() -> None:
    readme = _read(ROOT / "README.md")
    for needle in (
        "profile (`advisor` or `orchestrator`)",
        "frontier provider",
        "frontier model",
        "executor provider",
        "executor model",
        "update mode",
        "Providers are not models",
    ):
        assert needle.lower() in readme.lower(), f"README.md missing decision needle {needle!r}"


def test_doctor_and_update_boundaries() -> None:
    readme = _read(ROOT / "README.md")
    skill = _read(ROOT / "skills" / "frontierfuse" / "SKILL.md")
    design = _read(ROOT / "docs" / "DESIGN.md")
    surface = "\n".join((readme, skill, design))
    for needle in (
        "offline",
        "does not prove",
        "entitlement",
        "doctor --check-updates",
        "update --check",
        "CONFIG_INVALID",
    ):
        assert needle in surface, f"doctor/update boundary missing {needle!r}"
    # Exit codes documented in README doctor section
    assert re.search(r"\|\s*`0`\s*\|", readme)
    assert re.search(r"\|\s*`1`\s*\|", readme)
    assert re.search(r"\|\s*`2`\s*\|", readme)


def test_no_stale_active_0_3_2_baseline() -> None:
    plan = _read(ROOT / "docs" / "FRONTIERFUSE_EXECUTION_PLAN.md")
    design = _read(ROOT / "docs" / "DESIGN.md")
    readme = _read(ROOT / "README.md")
    assert "Architecture (0.3.2)" not in design
    assert "Architecture (0.3.6)" in design or "0.3.6" in design
    assert "active build is `0.3.2`" not in plan
    assert "0.3.6" in plan and "Shipped baseline" in plan
    assert "0.3.6" in readme
    # Historical delivered section may still name 0.3.2 as a past release.
    assert "Release 0.3.2" in plan or "0.3.2" in plan


def test_no_stale_product_claims() -> None:
    # Product-facing surfaces only. The execution plan may quote retired phrases in
    # delivered-task checklists (e.g. "Replace hard gate with workflow guardrail").
    product_paths = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "DESIGN.md",
        ROOT / "skills" / "frontierfuse" / "SKILL.md",
        ROOT / "skills" / "frontierfuse-config" / "SKILL.md",
        ROOT / "settings.hooks.snippet.json",
    ]
    surface = "\n".join(_read(p) for p in product_paths).lower()
    for stale in ("hard gate", "hard-gated", "cost-optimal"):
        assert stale not in surface, f"docs lane still contains stale claim {stale!r}"
    # Do not claim native marketplace packages for non-Claude harnesses.
    readme = _read(ROOT / "README.md").lower()
    assert "no separate codex / grok / gemini **native marketplace packages** are claimed" in readme


def test_skills_host_bound_and_decision_order() -> None:
    main = _read(ROOT / "skills" / "frontierfuse" / "SKILL.md")
    cfg = _read(ROOT / "skills" / "frontierfuse-config" / "SKILL.md")
    for text in (main, cfg):
        assert "cannot hot-swap" in text or "cannot hot-swap" in text.replace("‑", "-")
        assert "host" in text.lower()
    assert "frontier-led orchestrator" not in main.lower()
    assert "host-led" in main.lower() or "host controller" in main.lower()
    assert "separate" in cfg.lower()
    assert "profile" in cfg.lower() and "frontier" in cfg.lower() and "executor" in cfg.lower()


def test_contributing_four_file_version_and_scrub() -> None:
    text = _read(ROOT / "CONTRIBUTING.md")
    for needle in (
        "four",
        "plugin.json",
        "marketplace.json",
        "frontier_advisor_mcp.py",
        "frontier_update.py",
        "pre-push-check.sh",
        "public-release-scrub.py",
    ):
        assert needle in text, f"CONTRIBUTING.md missing {needle!r}"


def main() -> int:
    tests = [
        test_settings_hooks_snippet_is_valid_json,
        test_markdown_fences_balanced_in_docs_lane,
        test_skill_fence_does_not_trap_model_alias_prose,
        test_readme_install_lifecycle_paths,
        test_readme_three_workflows_and_host_bound,
        test_readme_config_decision_separation,
        test_doctor_and_update_boundaries,
        test_no_stale_active_0_3_2_baseline,
        test_no_stale_product_claims,
        test_skills_host_bound_and_decision_order,
        test_contributing_four_file_version_and_scrub,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - report every contract failure
            failed += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"docs_ux_contracts: FAIL ({failed}/{len(tests)})", file=sys.stderr)
        return 1
    print(f"docs_ux_contracts: PASS ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
