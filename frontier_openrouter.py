#!/usr/bin/env python3
"""Stdlib OpenRouter transport for FrontierFuse managed consults/bodies.

Usage (argv, no shell):
  python3 frontier_openrouter.py --model <id> --prompt-file <path>
  python3 frontier_openrouter.py --model <id> --prompt-file <path> --dry-run

Requires OPENROUTER_API_KEY for live calls. Never prints the key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://openrouter.ai/api/v1"
MAX_PROMPT_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _read_prompt(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_PROMPT_BYTES:
        raise SystemExit(f"openrouter refused: prompt exceeds {MAX_PROMPT_BYTES} bytes")
    return data.decode("utf-8")


def _api_key() -> str:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "openrouter refused: OPENROUTER_API_KEY is not set. "
            "Export a key from openrouter.ai, or use another provider."
        )
    return key


def call_chat(*, model: str, prompt: str, timeout: float) -> str:
    key = _api_key()
    base = (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE).rstrip("/")
    url = f"{base}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Renn-Labs/FrontierFuse",
            "X-Title": "FrontierFuse",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        # Never echo Authorization header material.
        raise SystemExit(
            f"openrouter HTTP {exc.code}: {detail[:200]}"
        ) from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"openrouter network error: {exc.reason}") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise SystemExit("openrouter refused: response exceeds capture bound")
    try:
        data = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise SystemExit("openrouter refused: non-JSON response") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise SystemExit("openrouter refused: empty choices")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str):
        raise SystemExit("openrouter refused: missing message content")
    return content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FrontierFuse OpenRouter transport")
    ap.add_argument("--model", required=True, help="exact OpenRouter model ID")
    ap.add_argument("--prompt-file", required=True, help="owner-managed prompt file")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print argv shape without network",
    )
    args = ap.parse_args(argv)
    path = Path(args.prompt_file)
    if not path.is_file():
        print(f"openrouter refused: prompt file not found: {path}", file=sys.stderr)
        return 2
    prompt = _read_prompt(path)
    if args.dry_run or os.environ.get("FRONTIER_OPENROUTER_DRY_RUN") == "1":
        print(
            json.dumps({
                "ok": True,
                "dry_run": True,
                "model": args.model,
                "prompt_bytes": len(prompt.encode("utf-8")),
                "base": (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE),
                "key_present": bool((os.environ.get("OPENROUTER_API_KEY") or "").strip()),
            })
        )
        return 0
    text = call_chat(model=args.model, prompt=prompt, timeout=float(args.timeout))
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
