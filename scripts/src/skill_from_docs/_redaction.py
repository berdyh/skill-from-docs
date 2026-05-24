"""Centralized redaction policy.

Used by `probe`, `auth`, `--dry-run` previews, and anywhere request/response
data could touch disk. Default behavior: redact everything that could carry
a credential.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


REDACTED = "<redacted>"

# Header names (case-insensitive match) that always get redacted.
SENSITIVE_HEADER_RE = re.compile(
    r"^(authorization|x-api-key|x-auth-token|api-key|token|cookie|x-csrf-token|set-cookie|location)$",
    re.IGNORECASE,
)

# Default body keys to redact recursively in JSON bodies.
DEFAULT_BODY_KEYS = (
    "token",
    "api_key",
    "apiKey",
    "secret",
    "password",
    "private_key",
    "access_token",
    "refresh_token",
    "session",
)

# URL query parameter names treated as credentials.
SENSITIVE_QUERY_KEYS = {
    "token",
    "api_key",
    "apikey",
    "access_token",
    "key",
    "secret",
    "password",
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

    The query string is rebuilt without URL-encoding the literal `<redacted>`
    sentinel so the result remains human-readable in fixtures and logs.
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
        if k.lower() in SENSITIVE_QUERY_KEYS:
            rebuilt.append(f"{k}={REDACTED}")
        else:
            rebuilt.append(f"{k}={v}")
    return urlunparse(parsed._replace(query="&".join(rebuilt)))


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
                else:
                    out[k] = _walk(v)
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
