#!/usr/bin/env python3
"""fleet_scrub.py — redaction + leak-block for fleet-fuse external calls.

Every prompt that leaves this machine for a non-local model is run through
``Scrubber.scrub()`` first. Two severity classes:

  HIGH  (secrets/keys/private material) -> after scrubbing, if ANY high-severity
        token still remains, ``assert_external_safe`` raises ``LeakBlocked`` and
        the orchestrator MUST drop that sub-task rather than send it. Fail closed.
  MED   (PII: email / IP / phone / SSN / card) -> replaced with reversible
        placeholders and rehydrated locally on the model's response.

Design: stdlib-only by default. If Microsoft Presidio is installed it is used as
an ADDITIONAL detector for names/locations/orgs; absence never breaks the engine
(it degrades to the regex+entropy core). No network, no external process.

Placeholders use the form ``[[RDCT_TAG_n]]`` — bracketed, distinctive, and
generally preserved verbatim by LLMs so rehydration round-trips.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# --- optional Presidio (PII names/locations) ---------------------------------
try:  # pragma: no cover - presence-dependent
    from presidio_analyzer import AnalyzerEngine  # type: ignore

    _PRESIDIO = AnalyzerEngine()
except Exception:  # not installed / failed to init -> regex core only
    _PRESIDIO = None


class LeakBlocked(Exception):
    """Raised when high-severity material survives scrubbing (fail closed)."""


# --- detector patterns -------------------------------------------------------
# HIGH severity: credentials / keys / private material. Order matters (most
# specific first). Each entry: (tag, compiled regex).
_HIGH = [
    ("PRIVKEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----.*?-----END[^-]*-----", re.DOTALL)),
    ("AWS_AKIA", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GCP_KEY", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9\-_]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b")),
    ("KV_SECRET", re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|access[_-]?key)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+._\-]{12,}")),
]

# MEDIUM severity: PII — reversible redaction.
_MED = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IPV4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("IPV6", re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[ \-.]?)?\(?\d{3}\)?[ \-.]\d{3}[ \-.]\d{4}(?!\d)")),
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
]

# Entropy gate: opaque tokens that match no pattern but look like secrets.
# Floor lowered to 24 so shorter API-style secrets don't slip past the residual scan.
_TOKEN = re.compile(r"\b[A-Za-z0-9+/_\-]{24,}={0,2}\b")
_ENTROPY_BITS = 3.6  # shannon bits/char threshold for "looks random"


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _luhn(num: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", num)]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ScrubResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    high_hits: list[str] = field(default_factory=list)     # tags of HIGH matches found
    med_hits: list[str] = field(default_factory=list)      # tags of MED matches found


class Scrubber:
    def __init__(self, presidio: bool = True):
        self.presidio = _PRESIDIO if presidio else None

    def scrub(self, text: str) -> ScrubResult:
        mapping: dict[str, str] = {}
        high_hits: list[str] = []
        med_hits: list[str] = []
        counters: dict[str, int] = {}

        def place(tag: str, original: str) -> str:
            counters[tag] = counters.get(tag, 0) + 1
            ph = f"[[RDCT_{tag}_{counters[tag]}]]"
            mapping[ph] = original
            return ph

        # HIGH first — these are replaced AND recorded (block trigger if residual)
        for tag, rx in _HIGH:
            def _sub_high(m, _tag=tag):
                high_hits.append(_tag)
                return place(_tag, m.group(0))
            text = rx.sub(_sub_high, text)

        # MEDIUM PII
        for tag, rx in _MED:
            def _sub_med(m, _tag=tag):
                val = m.group(0)
                if _tag == "CARD" and not _luhn(val):
                    return val  # not a real card number
                med_hits.append(_tag)
                return place(_tag, val)
            text = rx.sub(_sub_med, text)

        # Presidio pass (names/locations/orgs) if available
        if self.presidio is not None:
            try:
                spans = self.presidio.analyze(text=text, language="en")
                # apply high-to-low offset so indices stay valid
                for r in sorted(spans, key=lambda x: x.start, reverse=True):
                    if r.score < 0.6:
                        continue
                    if r.entity_type in ("PERSON", "LOCATION", "NRP", "ORGANIZATION"):
                        original = text[r.start:r.end]
                        ph = place(r.entity_type, original)
                        med_hits.append(r.entity_type)
                        text = text[:r.start] + ph + text[r.end:]
            except Exception:
                pass

        # Entropy gate for leftover opaque tokens
        def _ent(m):
            tok = m.group(0)
            if tok in mapping:  # already placeheld
                return tok
            if _shannon(tok) >= _ENTROPY_BITS:
                high_hits.append("HIGH_ENTROPY")
                return place("HIGH_ENTROPY", tok)
            return tok
        text = _TOKEN.sub(_ent, text)

        return ScrubResult(text=text, mapping=mapping, high_hits=high_hits, med_hits=med_hits)

    @staticmethod
    def rehydrate(text: str, mapping: dict[str, str]) -> str:
        for ph, original in mapping.items():
            text = text.replace(ph, original)
        return text

    def residual_high(self, text: str) -> list[str]:
        """Re-scan ALREADY-SCRUBBED text for any high-severity leftovers."""
        hits = []
        for tag, rx in _HIGH:
            if rx.search(text):
                hits.append(tag)
        for m in _TOKEN.finditer(text):
            if "[[RDCT_" not in m.group(0) and _shannon(m.group(0)) >= _ENTROPY_BITS:
                hits.append("HIGH_ENTROPY")
        return hits

    def assert_external_safe(self, scrubbed_text: str) -> None:
        """Fail closed: raise if any high-severity material survived scrubbing."""
        residual = self.residual_high(scrubbed_text)
        if residual:
            raise LeakBlocked(f"high-severity material survived scrub: {sorted(set(residual))}")


# quick self-test ------------------------------------------------------------
if __name__ == "__main__":
    s = Scrubber()
    fake_aws = "AKIA" + "1234567890ABCDEF"
    fake_openai = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123"
    samples = {
        "key": "use api_key=" + fake_aws + " and " + fake_openai,
        "pii": "email me at jane.doe@acme.com from 10.0.0.4, ssn 123-45-6789",
        "clean": "refactor the retry loop to use exponential backoff",
    }
    ok = True
    r = s.scrub(samples["key"])
    print("key   ->", r.text, "| high:", sorted(set(r.high_hits)))
    try:
        s.assert_external_safe(r.text); print("  external-safe: OK (scrubbed)")
    except LeakBlocked as e:
        ok = False; print("  BLOCK:", e)
    r2 = s.scrub(samples["pii"])
    print("pii   ->", r2.text, "| med:", sorted(set(r2.med_hits)))
    print("  rehydrate:", Scrubber.rehydrate(r2.text, r2.mapping) == samples["pii"])
    r3 = s.scrub(samples["clean"])
    print("clean ->", r3.text, "| (no hits:", not r3.high_hits and not r3.med_hits, ")")
    # residual block test: a raw key that slips through must be caught
    try:
        s.assert_external_safe("token ghp_" + "a" * 36); print("RESIDUAL TEST FAILED (should have blocked)"); ok = False
    except LeakBlocked:
        print("residual block: OK")
    print("SELFTEST:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
