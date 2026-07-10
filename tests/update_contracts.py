#!/usr/bin/env python3
"""Offline contracts for FrontierFuse release checks and update reminders."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import frontier_update as update


def _env(cache: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "FRONTIER_UPDATE_CACHE": str(cache),
            "FRONTIER_UPDATE_URL": "http://127.0.0.1:9/should-not-be-called",
            "FRONTIER_UPDATE_TIMEOUT": "0.05",
            "FRONTIER_CONFIG_DIR": str(cache.parent / "config"),
            "FRONTIER_STATE_DIR": str(cache.parent / "state"),
        }
    )
    return env


def main() -> int:
    assert update.semver_tuple("0.3.0") == (0, 3, 0)
    for invalid in ("0.2", "v0.3.0", "0.3.0-beta", "0.two.7"):
        try:
            update.semver_tuple(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid semver {invalid!r}")

    with tempfile.TemporaryDirectory(prefix="frontier-update-contracts-") as tmp:
        cache = Path(tmp) / "update-check.json"
        os.environ["FRONTIER_UPDATE_CACHE"] = str(cache)

        calls = 0

        def newer(_url: str, _timeout: float) -> dict:
            nonlocal calls
            calls += 1
            return {"version": "0.3.2"}

        result = update.check_for_updates(
            allow_network=True, fetcher=newer, now=1000, mode="passive"
        )
        assert result["status"] == "update_available", result
        assert calls == 1
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600

        def forbidden(_url: str, _timeout: float) -> bytes:
            raise AssertionError("fresh cache must prevent a network request")

        cached = update.check_for_updates(
            allow_network=True, fetcher=forbidden, now=1001, mode="passive"
        )
        assert cached["status"] == "update_available", cached
        assert cached["source"] == "cache", cached

        disabled = update.check_for_updates(
            allow_network=True, fetcher=forbidden, now=1002, mode="off"
        )
        assert disabled["status"] == "disabled", disabled

        cache.unlink()

        def broken(_url: str, _timeout: float) -> bytes:
            raise OSError("offline fixture")

        unknown = update.check_for_updates(
            allow_network=True, fetcher=broken, now=1003, mode="manual"
        )
        assert unknown["status"] == "unknown", unknown

        current = update.check_for_updates(
            allow_network=True,
            fetcher=lambda _url, _timeout: json.dumps({"version": update.CURRENT_VERSION}),
            now=2000,
            mode="manual",
            force=True,
        )
        assert current["status"] == "current", current

        cache.write_text('{"checked_at": "broken", "latest_version": "broken"}\n')
        malformed = update.check_for_updates(
            allow_network=False, now=2001, mode="passive"
        )
        assert malformed["status"] == "unknown", malformed

        cache.unlink()
        doctor = subprocess.run(
            [sys.executable, str(ROOT / "frontier_dispatch.py"), "doctor"],
            cwd=str(ROOT),
            env=_env(cache),
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert doctor.returncode in (0, 1), doctor
        assert "release status" in doctor.stdout, doctor.stdout
        assert "UNKNOWN" in doctor.stdout, doctor.stdout
        assert not cache.exists(), "offline doctor must not create an update cache"

        passive = subprocess.run(
            [
                sys.executable,
                str(ROOT / "frontier_dispatch.py"),
                "update",
                "--check",
                "--passive",
            ],
            cwd=str(ROOT),
            env={**_env(cache), "FRONTIER_UPDATE_MODE": "manual"},
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert passive.returncode == 0, passive
        assert passive.stdout == "", passive.stdout
        assert not cache.exists(), "manual mode passive reminder must make no request"

    print("update_contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
