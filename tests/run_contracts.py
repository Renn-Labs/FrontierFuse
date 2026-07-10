#!/usr/bin/env python3
"""Aggregate offline contract runner for FableFuse.

Discovers every tests/*_contracts.py suite (excluding this file) in deterministic
order, runs each in a subprocess, streams stdout/stderr, and exits nonzero if
no suites are found or any suite fails.

Stdlib-only. No live providers, no third-party imports.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SELF_NAME = Path(__file__).name


def discover_suites(tests_dir: Path) -> list[Path]:
    """Return *_contracts.py paths under tests_dir, excluding this runner, sorted."""
    if not tests_dir.is_dir():
        return []
    suites = [
        p
        for p in tests_dir.iterdir()
        if p.is_file()
        and p.name.endswith("_contracts.py")
        and p.name != SELF_NAME
    ]
    return sorted(suites, key=lambda p: p.name)


def run_suite(suite: Path, *, cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run one contract suite as a subprocess; stream output; return exit code."""
    print(f"=== {suite.name} ===", flush=True)
    proc = subprocess.run(
        [sys.executable, "-u", str(suite)],
        cwd=str(cwd),
        env=env,
        # No capture — stream suite output to the parent tty/CI log.
    )
    status = "PASS" if proc.returncode == 0 else f"FAIL (exit {proc.returncode})"
    print(f"--- {suite.name}: {status} ---", flush=True)
    return proc.returncode


def run_all(tests_dir: Path | None = None, *, cwd: Path | None = None) -> int:
    """Discover and run all contract suites. Returns process exit code."""
    tests_dir = tests_dir if tests_dir is not None else HERE
    cwd = cwd if cwd is not None else ROOT
    suites = discover_suites(tests_dir)
    if not suites:
        print(
            f"run_contracts: FAIL: no *_contracts.py suites discovered under {tests_dir}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(f"run_contracts: {len(suites)} suite(s): {', '.join(s.name for s in suites)}", flush=True)
    failed: list[str] = []
    for suite in suites:
        rc = run_suite(suite, cwd=cwd)
        if rc != 0:
            failed.append(f"{suite.name} (exit {rc})")

    if failed:
        print(
            f"run_contracts: FAIL ({len(failed)}/{len(suites)}): {', '.join(failed)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"run_contracts: PASS ({len(suites)} suite(s))", flush=True)
    return 0


def _self_test() -> None:
    """Focused unit checks for discovery / empty-dir failure (no full suite run)."""
    with tempfile.TemporaryDirectory(prefix="run-contracts-self-") as tmp:
        tdir = Path(tmp)

        # Empty dir → no suites; run_all must fail closed
        assert discover_suites(tdir) == [], "empty tests dir should yield no suites"
        rc = run_all(tests_dir=tdir, cwd=tdir)
        assert rc == 1, f"empty discovery should exit 1, got {rc}"

        # Non-matching names ignored; runner self-name ignored; order deterministic
        (tdir / "zzz_contracts.py").write_text(
            "import sys; print('zzz: PASS'); sys.exit(0)\n"
        )
        (tdir / "aaa_contracts.py").write_text(
            "import sys; print('aaa: PASS'); sys.exit(0)\n"
        )
        (tdir / "not_a_suite.py").write_text("# skip\n")
        (tdir / "readme.md").write_text("x\n")
        (tdir / SELF_NAME).write_text("# self\n")
        names = [p.name for p in discover_suites(tdir)]
        assert names == ["aaa_contracts.py", "zzz_contracts.py"], names
        assert SELF_NAME not in names

        # Two passing dummy suites → exit 0
        rc = run_all(tests_dir=tdir, cwd=tdir)
        assert rc == 0, f"passing suites should exit 0, got {rc}"

        # Any failing suite → nonzero aggregate
        (tdir / "bad_contracts.py").write_text(
            "import sys; print('bad'); sys.exit(7)\n"
        )
        rc = run_all(tests_dir=tdir, cwd=tdir)
        assert rc == 1, f"failing suite should exit 1, got {rc}"

    # Live discovery in this repo must include fable_contracts.py and exclude self
    live = discover_suites(HERE)
    live_names = [p.name for p in live]
    assert "fable_contracts.py" in live_names, live_names
    assert SELF_NAME not in live_names
    assert live_names == sorted(live_names)

    print("run_contracts: self-test PASS", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--self-test"], ["-t"]):
        try:
            _self_test()
        except AssertionError as exc:
            print(f"run_contracts: self-test FAIL: {exc}", file=sys.stderr, flush=True)
            return 1
        return 0
    if args and args[0] in ("-h", "--help"):
        print(
            "Usage: python tests/run_contracts.py [--self-test]\n"
            "  Discover and run tests/*_contracts.py (excluding this file).\n"
            "  --self-test  run focused runner self-tests only.",
            flush=True,
        )
        return 0
    if args:
        print(f"run_contracts: unknown args: {args}", file=sys.stderr, flush=True)
        return 2
    return run_all()


if __name__ == "__main__":
    # Ensure unbuffered progress when CI/pre-push sets PYTHONUNBUFFERED or not.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
