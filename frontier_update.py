#!/usr/bin/env python3
"""Privacy-preserving, cached release checks for FrontierFuse.

Checks read the public Claude plugin manifest. No machine identifier, repository data,
prompt, or usage information is sent. Callers decide whether network access is allowed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable

import frontier_common as fc

CURRENT_VERSION = "0.3.0"
DEFAULT_UPDATE_URL = (
    "https://raw.githubusercontent.com/Renn-Labs/FrontierFuse/"
    "master/.claude-plugin/plugin.json"
)
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


def _cache_path() -> Path:
    return Path(os.environ.get("FRONTIER_UPDATE_CACHE", fc.CONFIG_HOME / "update-check.json"))


def _ttl_seconds() -> int:
    try:
        return max(0, int(os.environ.get("FRONTIER_UPDATE_TTL_SECONDS", DEFAULT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def semver_tuple(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"not a stable semantic version: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _status(latest: str) -> str:
    current_v = semver_tuple(CURRENT_VERSION)
    latest_v = semver_tuple(latest)
    if latest_v > current_v:
        return "update_available"
    if latest_v < current_v:
        return "ahead"
    return "current"


def _read_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached_result(cache: dict, now: float) -> dict:
    latest = str(cache.get("latest_version") or "")
    try:
        status = _status(latest)
    except ValueError:
        return {
            "status": "unknown",
            "current_version": CURRENT_VERSION,
            "latest_version": "",
            "source": "none",
            "checked_at": 0.0,
        }
    try:
        checked_at = float(cache.get("checked_at") or 0.0)
    except (TypeError, ValueError):
        checked_at = 0.0
    return {
        "status": status,
        "current_version": CURRENT_VERSION,
        "latest_version": latest,
        "source": "cache",
        "checked_at": checked_at,
        "cache_fresh": checked_at > 0 and now - checked_at < _ttl_seconds(),
    }


def _fetch_manifest(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"FrontierFuse/{CURRENT_VERSION}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def check_for_updates(
    *,
    allow_network: bool,
    force: bool = False,
    mode: str = "passive",
    fetcher: Callable[[str, float], bytes | str | dict] | None = None,
    now: float | None = None,
) -> dict:
    """Return release status without raising on network or metadata failures."""
    now = time.time() if now is None else float(now)
    mode = str(mode or "passive").lower()
    if mode not in fc.UPDATE_MODES:
        mode = "passive"
    if mode == "off" and not force:
        return {
            "status": "disabled",
            "current_version": CURRENT_VERSION,
            "latest_version": "",
            "source": "disabled",
            "checked_at": 0.0,
        }

    cache = _read_cache()
    cached = _cached_result(cache, now)
    if not allow_network:
        return cached
    if cached.get("cache_fresh") and not force:
        return cached

    url = os.environ.get("FRONTIER_UPDATE_URL", DEFAULT_UPDATE_URL)
    try:
        timeout = max(0.1, float(os.environ.get("FRONTIER_UPDATE_TIMEOUT", "3")))
        raw = (fetcher or _fetch_manifest)(url, timeout)
        if isinstance(raw, dict):
            manifest = raw
        else:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            manifest = json.loads(raw)
        latest = str(manifest.get("version") or "")
        status = _status(latest)
        fc.write_json_owner_only(
            _cache_path(),
            {"checked_at": now, "latest_version": latest},
        )
        return {
            "status": status,
            "current_version": CURRENT_VERSION,
            "latest_version": latest,
            "source": "network",
            "checked_at": now,
            "cache_fresh": True,
        }
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        if cached["status"] != "unknown":
            cached["stale"] = True
            return cached
        return {
            "status": "unknown",
            "current_version": CURRENT_VERSION,
            "latest_version": "",
            "source": "error",
            "checked_at": 0.0,
        }
