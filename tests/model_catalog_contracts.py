#!/usr/bin/env python3
"""Offline contracts for provider/model selection and the source-backed catalog."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontier_models as models  # noqa: E402


def test_catalog_has_all_supported_providers() -> None:
    assert models.PROVIDERS == {"codex", "claude", "grok", "gemini"}
    for provider in models.PROVIDERS:
        assert models.SOURCES[provider]
        assert models.models_for(provider, discover=False)


def test_catalog_contains_verified_current_and_previous_models() -> None:
    ids = {
        provider: {row[0] for row in rows}
        for provider, rows in models.CATALOG.items()
    }
    assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini"} <= ids["codex"]
    assert {"claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6"} <= ids["claude"]
    assert {"gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro"} <= ids["gemini"]
    assert "grok-4.5" in ids["grok"]
    assert "grok-4.3" not in ids["grok"], "unverified IDs must not enter the static catalog"


def test_models_cli_json_is_machine_readable() -> None:
    with tempfile.TemporaryDirectory(prefix="frontier-models-") as td:
        env = os.environ.copy()
        env["FRONTIER_CONFIG_DIR"] = str(Path(td) / "config")
        env["FRONTIER_STATE_DIR"] = str(Path(td) / "state")
        proc = subprocess.run(
            [
                sys.executable, str(ROOT / "frontier_dispatch.py"),
                "models", "--provider", "gemini", "--no-discover", "--json",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert list(payload) == ["gemini"]
    assert payload["gemini"]["custom_model_allowed"] is True
    assert any(row["id"] == "gemini-3.5-flash" for row in payload["gemini"]["models"])


def main() -> int:
    tests = [
        test_catalog_has_all_supported_providers,
        test_catalog_contains_verified_current_and_previous_models,
        test_models_cli_json_is_machine_readable,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
    if failed:
        print(f"model_catalog_contracts: FAIL ({failed}/{len(tests)})", file=sys.stderr)
        return 1
    print("model_catalog_contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
