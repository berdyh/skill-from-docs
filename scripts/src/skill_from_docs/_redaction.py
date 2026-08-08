"""Centralized redaction policy.

Used by `probe`, `auth`, `--dry-run` previews, and anywhere request/response
data could touch disk. Default behavior: redact everything that could carry
a credential.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, unquote, urlparse, urlunparse


REDACTED = "<redacted>"

# Header names (case-insensitive match) that always get redacted.
SENSITIVE_HEADER_RE = re.compile(
    r"^(authorization|proxy-authorization|x-api-key|x-auth-token|api-key|token|cookie|x-csrf-token|set-cookie|location)$",
    re.IGNORECASE,
)

# Default body keys to redact recursively in JSON bodies.
DEFAULT_BODY_KEYS = (
    "token",
    "client_secret",
    "client_assertion",
    "api_key",
    "apiKey",
    "secret",
    "password",
    "private_key",
    "access_token",
    "refresh_token",
    "session",
)

# URL query parameter names treated as credentials. Kept in sync with
# DEFAULT_BODY_KEYS — a credential is no less sensitive for arriving in a query
# string than in a body, and query strings additionally reach logs, proxies, and
# CDN caches. Lowercased for comparison; `apikey` covers the `apiKey` spelling.
SENSITIVE_QUERY_KEYS = {k.lower() for k in DEFAULT_BODY_KEYS} | {
    "key",
    "auth",
    "sig",
    "signature",
}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with sensitive values replaced."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if SENSITIVE_HEADER_RE.match(k):
            out[k] = REDACTED
        else:
            out[k] = v
    return out


# Characters left literal when re-encoding a non-sensitive query value. These
# are all legal in a query per RFC 3986. `&`, `=`, `+` and `#` are deliberately
# NOT here: leaving them literal is what would let a single value split back
# into two parameters, which is the corruption the re-encoding exists to stop.
_QUERY_VALUE_SAFE = "/:@,!$'()*~"


# --- A7: fallback for URLs `urlparse` cannot parse -------------------------
#
# A malformed IPv6 literal (`https://[::1/p?token=1`, missing the closing
# `]`) makes `urlparse` raise. The old behaviour was `except Exception: return
# url` — fail-open, in the one function whose job is stripping credentials.
# Failing closed (a placeholder) was rejected: it would destroy the audit
# trail for the common case of a genuinely malformed URL with no credential
# in it, to protect the rare one that has both a syntax error and a secret.
# This is the middle option: a best-effort regex pass over the raw string.
#
# Userinfo first, unconditionally at the front of the string, so a stray `=`
# inside a password cannot later be mistaken for a query pair by the
# key/value pass. Anchored on `scheme://...@` with no `/`, `?`, `#`, `@` or
# whitespace in between — that is exactly how far userinfo can legally
# extend, and it holds regardless of how mangled the *rest* of the URL is
# (the example above is malformed entirely inside the host, well after where
# this pattern stops looking).
_FALLBACK_USERINFO_RE = re.compile(r"^(https?://)[^/?#@\s]+@")

# `key=value` pairs anywhere in the (still raw, not query-isolated) string —
# once urlparse has failed, there is no reliable way to know where the query
# component starts, so this scans the whole thing. The value alternative
# tries the literal `<redacted>` sentinel first so that a previously-redacted
# value round-trips unchanged instead of being re-matched a character short
# (see the idempotence note below).
_FALLBACK_KV_RE = re.compile(
    rf"(?P<key>[A-Za-z0-9_%+.\-]+)=(?P<value>{re.escape(REDACTED)}|[^&\s\"'<>\\]*)"
)


def _redact_url_fallback(url: str) -> str:
    """Best-effort redaction for a URL `urlparse` could not parse.

    Two passes, in this order:

    1. Userinfo (`user:pass@host`), replaced with a constant `<redacted>@` —
       the malformed part of the URL is, by construction, past this point
       (see `_FALLBACK_USERINFO_RE`), so nothing here depends on being able
       to parse the host. Idempotent because the replacement is a constant:
       running it again matches `<redacted>@` and replaces it with the same
       `<redacted>@`.
    2. `key=value` pairs anywhere in the remaining string, redacting the
       value when the (percent-decoded, lowercased) key is in
       `SENSITIVE_QUERY_KEYS`. Idempotent because the value pattern matches
       the literal `<redacted>` sentinel whole — without that alternative,
       `[^&\\s"'<>\\\\]*` would stop at the leading `<` and leave a
       one-character-short match, and a second pass would append a second
       sentinel (the exact bug `_URL_IN_TEXT_RE` already guards against for
       `redact_text`).

    Not attempted: reconstructing a well-formed URL. The output is a
    best-effort redaction of the input's *text*, not a parse-and-rebuild —
    there is nothing reliable to rebuild from.
    """

    def _redact_userinfo(m: re.Match[str]) -> str:
        return f"{m.group(1)}{REDACTED}@"

    out = _FALLBACK_USERINFO_RE.sub(_redact_userinfo, url, count=1)

    def _redact_kv(m: re.Match[str]) -> str:
        # Working from raw text here (no `parse_qsl` to decode for us), so
        # decode before the sensitivity check — same reasoning as the
        # parseable path's `?%74oken=x`.
        if unquote(m.group("key")).lower() in SENSITIVE_QUERY_KEYS:
            return f"{m.group('key')}={REDACTED}"
        return m.group(0)

    return _FALLBACK_KV_RE.sub(_redact_kv, out)


def redact_url(url: str) -> str:
    """Replace credentials in a URL with `<redacted>`.

    Covers both places a credential rides in a URL: the query string, and the
    userinfo component (`https://user:pass@host/...`). Userinfo matters even
    though `HostAllowlist` ignores it — `urlparse().hostname` strips userinfo,
    so an allowlisted host still admits `https://user:pass@host/spec.json`, and
    that string is what reaches `raw/source-map.json`, every `<!-- source: -->`
    comment in docs.md, handoff.json, and every probe fixture.

    Keys and values are re-encoded on the way out. `parse_qsl` hands back
    percent-DECODED text, so writing it back raw corrupts the URL: `?q=one%26two`
    would become `?q=one&two`, turning one parameter into two in a fixture that
    claims to record the request actually sent. Only the characters that could
    cause that split are escaped — a URL is recorded as an audit artifact, so
    gratuitously percent-encoding `?filter=a/b,c` is its own kind of damage.

    The `<redacted>` sentinel is the one deliberate exception — it is left
    unencoded so the result stays readable in fixtures and logs, and so
    re-redacting an already-redacted URL is a no-op.

    Decoding before the sensitivity check is intentional: it means `?%74oken=x`
    is still recognised as `token` and redacted.

    A7: `urlparse` can raise (a malformed IPv6 literal is the verified case).
    Falling back to `url` unchanged there was fail-open — the one function
    whose job is stripping credentials would hand a credential straight
    through if the URL happened to also be syntactically broken.
    `_redact_url_fallback` is the middle option: a regex pass over the raw
    text, redacting recognised `key=value` pairs and userinfo without
    requiring the URL to parse. See its docstring for what it does and does
    not attempt.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return _redact_url_fallback(url)

    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=f"{REDACTED}@{host}")

    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        rebuilt: list[str] = []
        for k, v in pairs:
            safe_k = quote(k, safe="")
            if k.lower() in SENSITIVE_QUERY_KEYS:
                rebuilt.append(f"{safe_k}={REDACTED}")
            else:
                rebuilt.append(f"{safe_k}={quote(v, safe=_QUERY_VALUE_SAFE)}")
        parsed = parsed._replace(query="&".join(rebuilt))

    return urlunparse(parsed)


# A URL embedded in free text — an exception message, a log line. Stops at
# whitespace, quotes, and the punctuation that usually ends a sentence rather
# than a URL. The `<redacted>` alternative keeps the sentinel inside the match
# so redacting twice is a no-op: without it the match stopped at `token=` and
# the second pass appended a second sentinel.
_URL_IN_TEXT_RE = re.compile(rf"https?://(?:{re.escape(REDACTED)}|[^\s\"'<>\\])+")


def redact_text(text: str) -> str:
    """Run `redact_url` over every http(s) URL found inside free text.

    `redact_url` only helps when you are holding a URL. Credentials also travel
    inside strings that merely *contain* one — an httpx exception message for a
    `--include-query-auth` attempt carries the token in the URL it quotes, and
    that string is written to a probe fixture. Redact where the value enters the
    workspace, not at each place it leaves.
    """
    return _URL_IN_TEXT_RE.sub(lambda m: redact_url(m.group(0)), text)


def redact_body(
    body: Any,
    *,
    extra_keys: Iterable[str] = (),
    patterns: Iterable[re.Pattern[str]] = (),
) -> Any:
    """Recursively redact sensitive values in a JSON-like body.

    - If body is a dict, redacts values for default + extra keys.
    - If body is a list, recurses into elements.
    - If body is a string, applies regex patterns.
    """
    keys = {k.lower() for k in DEFAULT_BODY_KEYS}
    keys.update(k.lower() for k in extra_keys)

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if isinstance(k, str) and k.lower() in keys:
                    out[k] = REDACTED
                    continue
                # Patterns apply to keys too. A body misread as form-encoded
                # would otherwise move a secret into a key, where a value-only
                # pass would never see it.
                new_k = k
                if isinstance(k, str):
                    for pat in patterns:
                        new_k = pat.sub(REDACTED, new_k)
                out[new_k] = _walk(v)
            return out
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            text = value
            for pat in patterns:
                text = pat.sub(REDACTED, text)
            return text
        return value

    return _walk(body)


def compile_patterns(raw: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in raw]
