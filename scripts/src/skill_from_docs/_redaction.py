"""Centralized redaction policy.

Used by `probe`, `auth`, `--dry-run` previews, and anywhere request/response
data could touch disk. Default behavior: redact everything that could carry
a credential.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlparse, urlunparse


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


def redact_url(url: str) -> str:
    """Replace sensitive query-string values with `<redacted>`.

    Keys and values are re-encoded on the way out. `parse_qsl` hands back
    percent-DECODED text, so writing it back raw corrupts the URL: `?q=one%26two`
    would become `?q=one&two`, turning one parameter into two in a fixture that
    claims to record the request actually sent.

    The `<redacted>` sentinel is the one deliberate exception — it is left
    unencoded so the result stays readable in fixtures and logs.

    Decoding before the sensitivity check is intentional: it means `?%74oken=x`
    is still recognised as `token` and redacted.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if not parsed.query:
        return url
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    rebuilt: list[str] = []
    for k, v in pairs:
        safe_k = quote(k, safe="")
        if k.lower() in SENSITIVE_QUERY_KEYS:
            rebuilt.append(f"{safe_k}={REDACTED}")
        else:
            rebuilt.append(f"{safe_k}={quote(v, safe='')}")
    return urlunparse(parsed._replace(query="&".join(rebuilt)))


# A URL embedded in free text — an exception message, a log line. Stops at
# whitespace, quotes, and the punctuation that usually ends a sentence rather
# than a URL.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+")


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
