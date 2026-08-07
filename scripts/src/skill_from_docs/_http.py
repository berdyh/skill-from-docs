"""Shared HTTP client.

- HTTP/1.1 only (`http2=False`).
- Custom User-Agent.
- Host allowlist check before every request.
- Redirects disabled by default.
- Retry helper that honors Retry-After (429) and exponential backoff (5xx).
"""

from __future__ import annotations

import time
from typing import Iterable
from urllib.parse import urlparse

try:  # pragma: no cover - import-time
    import httpx
except ImportError as e:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

from . import __version__


DEFAULT_USER_AGENT = f"skill-from-docs/{__version__} (https://github.com/berdyh/skill-from-docs)"


class AllowlistViolation(Exception):
    """Raised when a request target is not on the host allowlist."""


class HostAllowlist:
    """Case-insensitive host-membership check."""

    def __init__(self, hosts: Iterable[str] | None):
        self._hosts: set[str] = {h.lower() for h in (hosts or []) if h}

    def __bool__(self) -> bool:
        return bool(self._hosts)

    def check(self, url: str) -> None:
        if not self._hosts:
            return
        host = urlparse(url).hostname
        if host is None or host.lower() not in self._hosts:
            raise AllowlistViolation(
                f"host '{host}' not in allowlist (have: {sorted(self._hosts)})"
            )

    def __contains__(self, host: str) -> bool:
        return host.lower() in self._hosts


def require_httpx() -> None:
    if httpx is None:
        raise RuntimeError(
            f"httpx is not installed: {_IMPORT_ERROR}.\n"
            "Fix: pip install -e ~/.claude/skills/skill-from-docs/scripts"
        )


def build_client(
    *,
    timeout: float = 30.0,
    user_agent: str | None = None,
    follow_redirects: bool = False,
    transport=None,
):
    """Construct an `httpx.Client`. Caller is responsible for `.close()` or
    using as a context manager.
    """
    require_httpx()
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    kwargs = {
        "headers": headers,
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "http2": False,
        # Ignore HTTP_PROXY / HTTPS_PROXY / NO_PROXY env vars so token-bearing
        # requests never get routed through an attacker-controlled proxy. The
        # allowlist defends against poisoned target hosts; trust_env=False
        # closes the environment-poisoning side channel. (B4)
        "trust_env": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def request_with_retry(
    client,
    method: str,
    url: str,
    *,
    allowlist: HostAllowlist | None = None,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    sleeper=time.sleep,
):
    """Run a request with 429-Retry-After + 5xx exponential-backoff retries.

    Returns the final response (success OR last failing). Raises
    AllowlistViolation if the URL host isn't allowed.

    Backoff schedule: 1s, 2s, 4s, ...
    """
    if allowlist is not None:
        allowlist.check(url)

    attempts = 0
    while True:
        try:
            response = client.request(method, url, headers=headers, content=content)
        except Exception:  # network errors retried up to max_retries
            if attempts >= max_retries:
                raise
            sleeper(2 ** attempts)
            attempts += 1
            continue

        if response.status_code == 429 and attempts < max_retries:
            ra = response.headers.get("Retry-After")
            delay = _parse_retry_after(ra) if ra else 2 ** attempts
            sleeper(delay)
            attempts += 1
            continue
        if 500 <= response.status_code < 600 and attempts < max_retries:
            sleeper(2 ** attempts)
            attempts += 1
            continue
        return response


def _parse_retry_after(value: str) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        # HTTP-date form is rare for retry guidance from APIs; default small.
        return 1.0
