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
    disc = payload["gemini"]["discovery"]
    assert disc["supported"] is False
    assert disc["attempted"] is False
    assert disc["succeeded"] is False
    assert disc["discovered_ids"] == []


def test_discovery_supported_only_for_codex_and_grok() -> None:
    assert models.DISCOVERY_SUPPORTED == frozenset({"codex", "grok"})
    for provider in ("claude", "gemini"):
        result = models.discover_models(provider, attempt=True)
        assert result == {
            "supported": False,
            "attempted": False,
            "succeeded": False,
            "discovered_ids": [],
            "error_class": None,
        }
        assert models.discover_local_models(provider) == []

    for provider in ("codex", "grok"):
        skipped = models.discover_models(provider, attempt=False)
        assert skipped["supported"] is True
        assert skipped["attempted"] is False
        assert skipped["succeeded"] is False
        assert skipped["discovered_ids"] == []
        assert skipped["error_class"] is None


def test_parse_local_model_listings_without_inventing_ids() -> None:
    grok_ids = models._parse_grok_models(
        "Available models:\n"
        "* grok-4.5 (default)\n"
        "* grok-4\n"
        "please ignore prose without a bullet\n"
    )
    assert grok_ids == ["grok-4.5", "grok-4"]

    codex_ids = models._parse_codex_models(
        "Models:\n"
        "  gpt-5.6-sol\n"
        "  gpt-5.4-mini\n"
        "Reasoning effort:\n"
        "  high\n"
        "  xhigh\n"
    )
    assert codex_ids == ["gpt-5.6-sol", "gpt-5.4-mini"]
    assert "high" not in codex_ids

    assert models._parse_grok_models("please log in first") is None
    # Header/listing with no parseable IDs is malformed (None), not an invented empty success.
    assert models._parse_codex_models("Models:\n") is None
    assert models._parse_grok_models("Available models:\n") is None


def test_provider_models_payload_includes_discovery_metadata() -> None:
    payload = models.provider_models_payload("claude", discover=True)
    assert payload["custom_model_allowed"] is True
    assert payload["source"] == models.SOURCES["claude"]
    assert any(row["id"] == "claude-fable-5" for row in payload["models"])
    assert payload["discovery"]["supported"] is False
    assert payload["discovery"]["discovered_ids"] == []

    skipped = models.provider_models_payload("codex", discover=False)
    assert skipped["discovery"]["supported"] is True
    assert skipped["discovery"]["attempted"] is False
    assert all(row["status"] != "local" for row in skipped["models"])


def test_discovery_language_does_not_overstate_cli_behavior() -> None:
    discover_docs = models.discover_models.__doc__ or ""
    local_docs = models.discover_local_models.__doc__ or ""
    assert "may use its own authentication or network behavior" in discover_docs
    assert "not an entitlement guarantee" in discover_docs
    assert "entitlement-aware" not in local_docs


def main() -> int:
    tests = [
        test_catalog_has_all_supported_providers,
        test_catalog_contains_verified_current_and_previous_models,
        test_models_cli_json_is_machine_readable,
        test_discovery_supported_only_for_codex_and_grok,
        test_parse_local_model_listings_without_inventing_ids,
        test_provider_models_payload_includes_discovery_metadata,
        test_discovery_language_does_not_overstate_cli_behavior,
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
