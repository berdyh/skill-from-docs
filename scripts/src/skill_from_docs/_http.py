"""Shared HTTP client.

- HTTP/1.1 only (`http2=False`).
- Custom User-Agent.
- Host allowlist bound to the client: an off-allowlist request is structurally
  impossible, not conventionally avoided (D1).
- Redirects disabled by default.
- Retry helper that honors Retry-After (429) and exponential backoff (5xx).
"""

from __future__ import annotations

import contextlib
import sys
import time
from typing import Iterable, Iterator
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
    """Case-insensitive host-membership check.

    The two query methods answer different questions and treat an **empty**
    allowlist oppositely, on purpose:

    - `check()` gates an outbound request the user asked for. Empty means the
      user named no restriction, so it permits everything. Subcommands that
      must not run unrestricted call `require_allowlist` instead of relying on
      this.
    - `lists_host()` asks whether the user *named* a host. Empty means they
      named none, so it is False for every host. Callers vetting a target the
      user never typed — an `$ref` inside a downloaded spec — need that
      fail-closed answer.

    Do not collapse them, and do not spell `lists_host` as `in`: `host in
    allowlist` reads like `check` and silently means the opposite.
    """

    def __init__(self, hosts: Iterable[str] | None):
        self._hosts: set[str] = {h.lower() for h in (hosts or []) if h}

    def __bool__(self) -> bool:
        return bool(self._hosts)

    def check(self, url: str) -> None:
        """Raise AllowlistViolation unless `url`'s host is allowed. An empty
        allowlist permits every host — see the class docstring."""
        if not self._hosts:
            return
        host = urlparse(url).hostname
        if host is None or not self.permits_host(host):
            raise AllowlistViolation(
                f"host '{host}' not in allowlist (have: {sorted(self._hosts)})"
            )

    def permits_host(self, host: str) -> bool:
        """`check`'s question asked about a bare host instead of a URL, with
        `check`'s reading of empty: no restriction named, so everything passes.

        This is the *same* side of the asymmetry as `check`, not a third
        policy — `lists_host` is still the other side. `narrowed` needs the
        question in this shape because it compares two allowlists, not a URL.
        """
        if not self._hosts:
            return True
        return host.lower() in self._hosts

    def lists_host(self, host: str) -> bool:
        """True only if the user explicitly named `host`. An empty allowlist
        lists nothing — see the class docstring."""
        return host.lower() in self._hosts

    def hosts(self) -> frozenset[str]:
        """The hosts the user named, lowercased. Empty means they named none,
        which `check` reads as "no restriction" and `lists_host` as "nothing"."""
        return frozenset(self._hosts)


def require_httpx() -> None:
    if httpx is None:
        raise RuntimeError(
            f"httpx is not installed: {_IMPORT_ERROR}.\n"
            "Fix: pip install -e ~/.claude/skills/skill-from-docs/scripts"
        )


# The exception types that mean "the request reached the network stack and the
# server did not answer". Anything *outside* this tuple means the request could
# not be issued at all — a degenerate timeout, a malformed URL — which is a
# config error a probe loop must not swallow as "that candidate 404'd". (A10/B5)
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    () if httpx is None else (httpx.RequestError,)
)


class _ClientPolicy:
    """The allowlist a `GuardedClient` enforces, plus its narrowing stack.

    A stack rather than a single value so `narrowed` restores the outer policy
    on the way out, including when the block raises.
    """

    def __init__(self, allowlist: "HostAllowlist | None"):
        self._stack: list[HostAllowlist] = [allowlist or HostAllowlist([])]

    @property
    def active(self) -> "HostAllowlist":
        return self._stack[-1]

    def push(self, allowlist: "HostAllowlist") -> None:
        self._stack.append(allowlist)

    def pop(self) -> None:
        self._stack.pop()


if httpx is not None:

    class GuardedClient(httpx.Client):  # type: ignore[misc]
        """An `httpx.Client` that cannot issue a request to an off-allowlist host.

        The check runs in a request event hook, so it sits under every route
        into the client — `get`, `request`, `send`, and each hop a redirect
        follower would issue — instead of at whichever call sites happened to
        remember it. That was D1's whole point: one invariant, one enforcement.

        The hook is re-installed by the `event_hooks` setter, so assigning
        `client.event_hooks = {...}` cannot unhook the guard.
        """

        def __init__(self, *args, allowlist: "HostAllowlist | None" = None, **kwargs):
            self._policy = _ClientPolicy(allowlist)
            super().__init__(*args, **kwargs)
            # httpx's own __init__ assigns `self._event_hooks` directly rather
            # than through the property, so the setter below does not run for
            # it. Install explicitly; the setter covers every later assignment.
            self.event_hooks = self._event_hooks

        # httpx's own `event_hooks` property is re-declared here only so the
        # setter can keep the guard installed; the getter is unchanged.
        @property
        def event_hooks(self) -> dict:
            return self._event_hooks

        @event_hooks.setter
        def event_hooks(self, event_hooks: dict) -> None:
            hooks = {
                "request": list(event_hooks.get("request", [])),
                "response": list(event_hooks.get("response", [])),
            }
            guard = self._enforce_allowlist
            if guard not in hooks["request"]:
                hooks["request"].insert(0, guard)
            self._event_hooks = hooks

        def _enforce_allowlist(self, request) -> None:
            # `str(request.url)` keeps any userinfo httpx preserved; the
            # allowlist parses the host out of it rather than string-matching,
            # so `https://api.allowed.example@evil.example/x` is judged on
            # `evil.example`.
            self._policy.active.check(str(request.url))

        @contextlib.contextmanager
        def narrowed(self, allowlist: "HostAllowlist") -> Iterator["GuardedClient"]:
            """Restrict this client to `allowlist` for the duration of the block.

            Narrowing only ever restricts. A host the enclosing scope would
            reject is rejected here too — this is not a way to reach one, and
            asking for it raises rather than silently permitting it.

            An empty `HostAllowlist` is refused outright: `check` reads empty as
            "no restriction named", so installing one would widen the policy to
            every host. That is failure mode 4 (`--allow-host ""`) wearing a
            different hat, and it is the reason this takes a constructed
            `HostAllowlist` and not a list of strings.
            """
            if not isinstance(allowlist, HostAllowlist):
                raise TypeError(
                    "narrowed() takes a constructed HostAllowlist, not a raw host list"
                )
            if not allowlist:
                raise ValueError(
                    "narrowed() requires at least one host: an empty HostAllowlist "
                    "permits every host, so installing one would widen the policy"
                )
            outer = self._policy.active
            rejected = sorted(h for h in allowlist.hosts() if not outer.permits_host(h))
            if rejected:
                raise AllowlistViolation(
                    f"cannot narrow to {rejected}: not permitted by the enclosing "
                    "allowlist (narrowing restricts, it never widens)"
                )
            self._policy.push(allowlist)
            try:
                yield self
            finally:
                self._policy.pop()

else:  # pragma: no cover - httpx missing; require_httpx() fires first
    GuardedClient = None  # type: ignore[assignment]


def build_client(
    *,
    allowlist: HostAllowlist | None = None,
    timeout: float = 30.0,
    user_agent: str | None = None,
    follow_redirects: bool = False,
    transport=None,
):
    """Construct a `GuardedClient`. Caller is responsible for `.close()` or
    using as a context manager.

    `allowlist` is bound to the client: every request it issues is checked
    against it. `None` means unrestricted, matching `HostAllowlist([]).check`
    — every subcommand that touches the network passes a real one, gated by
    `require_allowlist`.
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
    return GuardedClient(allowlist=allowlist, **kwargs)


def request_with_retry(
    client,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float | None = None,
    sleeper=time.sleep,
):
    """Run a request with 429-Retry-After + 5xx exponential-backoff retries.

    Returns the final response (success OR last failing). Raises
    AllowlistViolation if the URL host isn't allowed by the client's bound
    allowlist — there is no `allowlist` parameter here any more, because the
    client enforces it and a second gate is a second thing to remember (D1).

    `timeout` overrides the client's timeout for this request only — used for
    speculative probes that should not inherit a long download budget.

    Backoff schedule: 1s, 2s, 4s, ...
    """
    extra = {} if timeout is None else {"timeout": timeout}
    attempts = 0
    while True:
        try:
            response = client.request(method, url, headers=headers, content=content, **extra)
        except AllowlistViolation:
            # Raised by the client's request hook, i.e. before anything left the
            # process. Retrying a policy decision would just re-decide it, and
            # the generic handler below would burn the whole retry budget doing
            # so before surfacing the same error.
            raise
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


def require_allowlist(hosts, *, subcommand: str, context: str | None = None) -> "HostAllowlist | None":
    """Build a HostAllowlist and return it, or None if it would permit everything.

    Callers must test the *constructed* allowlist rather than the raw argparse
    list: `--allow-host ""` (an unset shell var) yields `[""]`, which is truthy,
    but HostAllowlist drops empty strings and an empty allowlist permits every
    host — so the flag that exists to restrict outbound calls would silently
    allow them all. On None, the caller prints nothing extra and exits 1; this
    function has already explained the problem.
    """
    allowlist = HostAllowlist(hosts or [])
    if allowlist:
        return allowlist
    where = f" {context}" if context else ""
    print(
        f"ERROR: --allow-host HOST is required for {subcommand}{where} "
        "and must name at least one non-empty host.",
        file=sys.stderr,
    )
    return None


def _parse_retry_after(value: str) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        # HTTP-date form is rare for retry guidance from APIs; default small.
        return 1.0
