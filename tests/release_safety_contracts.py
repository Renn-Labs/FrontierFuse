#!/usr/bin/env python3
"""Stdlib contracts for public-release scrub, denylist, ignore sync, and pre-push gates.

Probes are assembled from harmless fragments so tracked test sources never contain a
complete token-shaped literal. Scanner output is asserted never to echo probe values.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRUB_PATH = SCRIPTS / "public-release-scrub.py"
DENY_PATH = SCRIPTS / "release_denylist.py"
PRE_PUSH = SCRIPTS / "pre-push-check.sh"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # Ensure sibling imports (release_denylist) resolve for the scrub module.
    sys.path.insert(0, str(path.parent))
    # dataclasses (and similar) resolve cls.__module__ via sys.modules.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scrub = _load("public_release_scrub_under_test", SCRUB_PATH)
denylist = _load("release_denylist_under_test", DENY_PATH)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _frag_join(*parts: str) -> str:
    return "".join(parts)


def _openai_shaped() -> str:
    # sk-ant- + 24 body chars — assembled so this file never holds a full literal.
    return _frag_join("sk", "-", "ant", "-", "a" * 24)


def _dotenv_line() -> str:
    name = _frag_join("OPENAI", "_", "API", "_", "KEY")
    val = _frag_join("x", "y", "z", "0", "1", "2", "3", "4", "5", "6", "7", "8")
    return f"{name}={val}"


def _github_shaped() -> str:
    return _frag_join("ghp", "_", "A" * 36)


def test_dotenv_secret_assignment_detected() -> None:
    line = _dotenv_line()
    findings = scrub.scan_text("t", "local.env", line + "\n")
    kinds = {f.kind for f in findings}
    _assert(
        "DOTENV_SECRET_ASSIGNMENT" in kinds or "SECRET_ASSIGNMENT" in kinds,
        f"dotenv assignment not detected: {kinds}",
    )
    indented = scrub.scan_text("t", "local.env", "  export " + line + "\n")
    _assert(
        any(f.kind == "DOTENV_SECRET_ASSIGNMENT" for f in indented),
        "indented dotenv assignment not detected",
    )
    code = "\n".join((
        "_NON_MODEL_TOKENS = frozenset({",
        "token = _strip_model_token(value)",
    ))
    code_findings = scrub.scan_text("t", "probe.py", code)
    code_kinds = {f.kind for f in code_findings}
    _assert("DOTENV_SECRET_ASSIGNMENT" not in code_kinds, code_kinds)
    _assert("SECRET_ASSIGNMENT" not in code_kinds, code_kinds)


def test_provider_token_prefixes_detected() -> None:
    probes = {
        "OPENAI_ANTHROPIC_OR_OPENROUTER_KEY": _openai_shaped(),
        "GITHUB_TOKEN": _github_shaped(),
        "HF_TOKEN": _frag_join("hf", "_", "B" * 24),
        "NPM_TOKEN": _frag_join("npm", "_", "C" * 24),
        "PYPI_TOKEN": _frag_join("pypi", "-", "D" * 24),
        "XAI_TOKEN": _frag_join("xai", "-", "E" * 24),
    }
    for kind, probe in probes.items():
        findings = scrub.scan_text("t", "probe.txt", f"value {probe}\n")
        kinds = {f.kind for f in findings}
        _assert(kind in kinds, f"expected {kind} for probe family, got {kinds}")


def test_high_entropy_secret_context_skips_code_identifiers() -> None:
    opaque = _frag_join(
        "aB9/", "cD+e", "F-gH", "2iJ3", "kL4m", "N5oP", "6qR7", "sT8u", "V9wX"
    )
    findings = scrub.scan_text("t", "probe.txt", f"secret value {opaque}\n")
    _assert(
        any(f.kind == "HIGH_ENTROPY_SECRET_CONTEXT" for f in findings),
        "opaque secret-context value not detected",
    )
    code_name = "test_denylist_hits_runtime_and_secret_artifacts"
    findings = scrub.scan_text("t", "probe.py", f"def {code_name}():\n")
    _assert(
        not any(f.kind == "HIGH_ENTROPY_SECRET_CONTEXT" for f in findings),
        "code identifier must not be treated as an opaque secret",
    )


def test_non_leak_output() -> None:
    probe = _openai_shaped()
    text = f"token_line = {probe}\n"
    findings = scrub.scan_text("worktree", "secret.env", text)
    _assert(findings, "expected at least one finding")
    for finding in findings:
        rendered = scrub.format_finding(finding)
        _assert(probe not in rendered, "finding output leaked probe value")
        _assert(probe not in finding.kind, "kind must not contain probe")
        _assert(probe not in finding.path, "path must not contain probe")
        # Only metadata fields: scope/path/line/kind
        _assert(finding.scope, "scope required")
        _assert(finding.path, "path required")
        _assert(isinstance(finding.line, int), "line must be int")
        _assert(finding.kind, "kind required")
        _assert(not re.search(r"sk-ant-a{10,}", rendered), "output must not echo token shape")


def test_commit_message_scope() -> None:
    probe = _openai_shaped()
    findings = scrub.scan_text("push:deadbeef", "COMMIT_MESSAGE", f"WIP drop {probe}\n")
    _assert(findings, "commit message with token shape must yield findings")
    _assert(all(f.path == "COMMIT_MESSAGE" for f in findings), "path must be COMMIT_MESSAGE")
    rendered = "\n".join(scrub.format_finding(f) for f in findings)
    _assert(probe not in rendered, "commit-message findings must not leak probe")


def test_scan_commits_includes_commit_messages() -> None:
    """Integration: temp repo with clean tree + secret only in commit message."""
    probe = _openai_shaped()
    with tempfile.TemporaryDirectory(prefix="ff-scrub-msg-") as td:
        repo = Path(td)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Release Safety"
        env["GIT_AUTHOR_EMAIL"] = "release-safety@example.com"
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "release-safety@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Release Safety"], cwd=repo, check=True)
        (repo / "README").write_text("clean\n")
        subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
        msg = f"chore: temporary probe {probe}"
        subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True, env=env)

        # Run scrub against the temp repo cwd (script path absolute).
        proc = subprocess.run(
            [sys.executable, str(SCRUB_PATH), "--all-history"],
            cwd=repo,
            capture_output=True,
            text=True,
            errors="ignore",
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        _assert(proc.returncode != 0, "expected FAIL when commit message holds token shape")
        _assert("COMMIT_MESSAGE" in combined, f"expected COMMIT_MESSAGE scope in output, got:\n{combined}")
        _assert(probe not in combined, "scrub CLI must not print probe value")
        _assert("Matched values are intentionally not printed" in combined, "non-leak banner missing")


def test_denylist_hits_runtime_and_secret_artifacts() -> None:
    hits = [
        "runs/out/verdict-copy.json",
        "verdict.json",
        "pkg/__pycache__/x.pyc",
        "mod.pyc",
        ".omc/state.json",
        ".omx/cache",
        "nested/.omc/state.json",
        "nested/.omx/cache",
        "nested/.grokprint/trace",
        "nested/.buildlog/day1.md",
        ".grokprint/trace",
        ".buildlog/day1.md",
        ".env",
        ".env.local",
        ".envrc",
        "env.local",
        "config/credentials.json",
        "service-secrets.yaml",
        "id_rsa",
        "id_ed25519",
        "id_rsa_work",
        "tls.pem",
        "dump.quarantine",
        "quarantine/item.bin",
        "provider-codex.log",
        "body_provider.log",
        "transcripts/session.jsonl",
        "agent.transcript",
        "agent.transcript.jsonl",
        "session_transcript.log",
    ]
    for path in hits:
        _assert(denylist.is_forbidden_path(path), f"expected forbidden: {path}")


def test_denylist_false_positives_docs_and_prose_names() -> None:
    safe = [
        "docs/PUBLIC_RELEASE_CHECKLIST.md",
        "AGENTS.md",
        "README.md",
        "skills/frontierfuse/SKILL.md",
        "notes-about-transcript-handling.md",
        "docs/using-transcripts-in-tests.md",
        "tests/release_safety_contracts.py",
        ".env.example",
        ".env.sample",
        "nested/.env.example",
        "src/env_helpers.py",
        "hooks/frontier_gate.py",
    ]
    for path in safe:
        _assert(not denylist.is_forbidden_path(path), f"false positive denylist hit: {path}")


def test_scan_commits_fails_closed_on_history_read_errors() -> None:
    commit = "a" * 40
    old_message = scrub.commit_message
    old_paths = scrub.tree_paths
    old_show = scrub.show_file
    try:
        scrub.commit_message = lambda _commit: "clean message"  # type: ignore[assignment]
        scrub.tree_paths = lambda _commit: ["clean.txt"]  # type: ignore[assignment]
        scrub.show_file = lambda _commit, _path: None  # type: ignore[assignment]
        findings = scrub.scan_commits([commit], "history")
        _assert(
            any(f.kind == "HISTORY_FILE_READ_ERROR" for f in findings),
            "unreadable history blob must block the scrub",
        )

        scrub.commit_message = lambda _commit: None  # type: ignore[assignment]
        scrub.tree_paths = lambda _commit: None  # type: ignore[assignment]
        findings = scrub.scan_commits([commit], "history")
        kinds = {f.kind for f in findings}
        _assert("HISTORY_COMMIT_MESSAGE_READ_ERROR" in kinds, "unreadable message must block")
        _assert("HISTORY_TREE_READ_ERROR" in kinds, "unreadable tree must block")
    finally:
        scrub.commit_message = old_message
        scrub.tree_paths = old_paths
        scrub.show_file = old_show


def test_gitignore_aligned_with_denylist() -> None:
    gi = (ROOT / ".gitignore").read_text()
    for needle in denylist.GITIGNORE_NEEDLES:
        _assert(needle in gi, f".gitignore missing denylist family needle {needle!r}")
    # Portable repo rules present (not only a global ignore comment)
    _assert("runs/" in gi and ".env" in gi and "transcripts/" in gi, "core ignore families missing")


def test_pre_push_silent_skip_rejected() -> None:
    env = os.environ.copy()
    env["FRONTIER_SKIP_PRE_PUSH"] = "1"
    # Pair without --maintainer-escape must fail closed before heavy work.
    proc = subprocess.run(
        ["bash", str(PRE_PUSH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="ignore",
        env=env,
        timeout=30,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    _assert(proc.returncode != 0, "silent FRONTIER_SKIP_PRE_PUSH=1 must not succeed")
    _assert("not a silent bypass" in combined.lower() or "NOT a silent bypass" in combined, combined)


def test_pre_push_help_documents_escape_limitation() -> None:
    proc = subprocess.run(
        ["bash", str(PRE_PUSH), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=15,
    )
    _assert(proc.returncode == 0, proc.stderr)
    out = proc.stdout + proc.stderr
    _assert("--maintainer-escape" in out, "help must document maintainer escape")
    _assert("--all-history" in out or "--release-check" in out, "help must document full-history option")
    _assert("--offline" in out or "--ci" in out, "help must document offline mode")
    _assert("NOT" in out and "public-release" in out.lower(), "help must state escape is not a public-release bypass")


def test_pre_push_offline_runs_public_gates() -> None:
    """Offline mode should complete public-release gates without needing claude CLI."""
    env = os.environ.copy()
    env.pop("FRONTIER_SKIP_PRE_PUSH", None)
    # Avoid PATH requiring claude; offline must not need it.
    proc = subprocess.run(
        ["bash", str(PRE_PUSH), "--offline", "--allow-dirty"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="ignore",
        env=env,
        timeout=600,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    _assert(proc.returncode == 0, f"offline pre-push failed:\n{combined}")
    _assert("public release scrub" in combined.lower() or "scrub scope" in combined.lower(), combined)
    _assert("denylist" in combined.lower(), combined)
    _assert("all checks passed" in combined.lower(), combined)
    # Must not claim maintainer escape path.
    _assert("MAINTAINER ESCAPE ACTIVE" not in combined, combined)


def test_pre_push_source_has_all_history_and_loud_escape() -> None:
    src = PRE_PUSH.read_text()
    _assert("--all-history" in src, "pre-push must support --all-history")
    _assert("MAINTAINER ESCAPE" in src, "escape must be loud in script output")
    _assert("Does NOT skip public-release gates" in src or "does NOT skip public-release" in src, src)
    _assert("FRONTIER_SKIP_PRE_PUSH" in src, "must mention env skip hardening")
    # Silent skip alone must not exit 0 early at top.
    _assert("not a silent bypass" in src.lower() or "NOT a silent bypass" in src or "not a silent bypass" in src, src)


def test_offline_workflow_wires_scrub_and_prepush() -> None:
    yml = (ROOT / ".github" / "workflows" / "offline.yml").read_text()
    _assert("public-release-scrub.py" in yml, "offline workflow must run scrub")
    _assert("--all-history" in yml, "offline workflow must include full-history scrub")
    _assert("pre-push-check.sh" in yml, "offline workflow must run pre-push equivalent")
    _assert("--offline" in yml, "pre-push invocation must be offline subset")
    _assert("release_denylist.py" in yml, "offline workflow must check shared denylist")
    # New modules must stay in CI compile list.
    _assert("frontier_topology.py" in yml, "offline workflow must compile frontier_topology.py")
    _assert("frontier_openrouter.py" in yml, "offline workflow must compile frontier_openrouter.py")
    # No live provider secret env wiring
    _assert("OPENAI_API_KEY" not in yml, "must not wire live provider keys")
    _assert("ANTHROPIC_API_KEY" not in yml, "must not wire live provider keys")


def test_pre_push_compiles_new_modules() -> None:
    src = PRE_PUSH.read_text()
    _assert("frontier_topology.py" in src, "pre-push must byte-compile frontier_topology.py")
    _assert("frontier_openrouter.py" in src, "pre-push must byte-compile frontier_openrouter.py")


def test_agent_gate_memory_files_present() -> None:
    """Agent-facing memory must exist and ban hook skips for public pushes."""
    agents = (ROOT / "AGENTS.md").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()
    checklist = (ROOT / "docs" / "PUBLIC_RELEASE_CHECKLIST.md").read_text()
    for label, text in (("AGENTS.md", agents), ("CLAUDE.md", claude), ("checklist", checklist)):
        _assert("hooksPath" in text, f"{label} must mention hooksPath")
        _assert("pre-push-check.sh" in text, f"{label} must mention pre-push-check.sh")
        _assert("--no-verify" in text, f"{label} must ban --no-verify for public pushes")
    _assert("Claude Code" in agents and "Codex" in agents, "AGENTS.md must name Claude Code and Codex")
    _assert("Public Push Gate" in agents, "AGENTS.md must have Public Push Gate section")


def main() -> int:
    tests = [
        test_dotenv_secret_assignment_detected,
        test_provider_token_prefixes_detected,
        test_high_entropy_secret_context_skips_code_identifiers,
        test_non_leak_output,
        test_commit_message_scope,
        test_scan_commits_includes_commit_messages,
        test_scan_commits_fails_closed_on_history_read_errors,
        test_denylist_hits_runtime_and_secret_artifacts,
        test_denylist_false_positives_docs_and_prose_names,
        test_gitignore_aligned_with_denylist,
        test_pre_push_silent_skip_rejected,
        test_pre_push_help_documents_escape_limitation,
        test_pre_push_source_has_all_history_and_loud_escape,
        test_offline_workflow_wires_scrub_and_prepush,
        test_pre_push_compiles_new_modules,
        test_agent_gate_memory_files_present,
        # Heavier: runs full offline gate including this suite via discovery once nested.
        # Keep last so unit checks fail fast first.
        test_pre_push_offline_runs_public_gates,
    ]
    # Avoid recursive explosion: when already inside pre-push offline, skip the nested
    # full pre-push invocation (env marker set by that test only for child? we set none).
    # Instead: if FRONTIER_RELEASE_SAFETY_INNER=1, skip the heavy nested pre-push test.
    if os.environ.get("FRONTIER_RELEASE_SAFETY_INNER") == "1":
        tests = [t for t in tests if t is not test_pre_push_offline_runs_public_gates]

    failed = 0
    for test in tests:
        try:
            if test is test_pre_push_offline_runs_public_gates:
                os.environ["FRONTIER_RELEASE_SAFETY_INNER"] = "1"
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
        finally:
            if test is test_pre_push_offline_runs_public_gates:
                os.environ.pop("FRONTIER_RELEASE_SAFETY_INNER", None)
    if failed:
        print(f"release_safety_contracts: FAIL ({failed}/{len(tests)})", file=sys.stderr)
        return 1
    print("release_safety_contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
