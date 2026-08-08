"""Unit tests for the shared HTTP substrate."""

from __future__ import annotations

import httpx
import pytest

from skill_from_docs._http import (
    AllowlistViolation,
    HostAllowlist,
    build_client,
    request_with_retry,
    require_allowlist,
)


ALLOWED = "https://api.example.com/x"
OFF = "https://evil.example.net/x"


def _ok_transport(seen: list[str] | None = None) -> httpx.MockTransport:
    """A transport that answers 200 to anything. Anything it records is a
    request that got past the allowlist — which is the whole assertion."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def test_check_permits_everything_when_empty():
    """`check` gates a request the user asked for; no allowlist means no
    restriction was named."""
    HostAllowlist([]).check("https://anything.example.com/x")
    HostAllowlist(None).check("https://anything.example.com/x")


def test_lists_host_lists_nothing_when_empty():
    """`lists_host` asks whether the user *named* a host. The opposite reading
    of "empty" from `check`, deliberately: callers vetting a target the user
    never typed — an `$ref` inside a downloaded spec — need fail-closed."""
    assert HostAllowlist([]).lists_host("anything.example.com") is False
    assert HostAllowlist(["api.example.com"]).lists_host("api.example.com") is True
    assert HostAllowlist(["API.Example.com"]).lists_host("api.example.COM") is True
    assert HostAllowlist(["api.example.com"]).lists_host("evil.example.net") is False


def test_check_rejects_off_allowlist_host():
    with pytest.raises(AllowlistViolation):
        HostAllowlist(["api.example.com"]).check("https://evil.example.net/x")


def test_require_allowlist_rejects_the_empty_string(capsys):
    """argparse `append` turns an unset shell variable into `[""]`, which is
    truthy but builds an allowlist that permits everything. Gates must test the
    constructed object, which is what this helper exists to force."""
    assert require_allowlist([""], subcommand="probe") is None
    assert "allow-host" in capsys.readouterr().err
    assert require_allowlist([], subcommand="probe") is None

    built = require_allowlist(["api.example.com"], subcommand="probe")
    assert built is not None and built.lists_host("api.example.com")


# ---------------------------------------------------------------------------
# D1: the allowlist is bound to the client, so bypassing it means finding a
# route into httpx that the request event hook does not sit under. Each test
# below is one such route that was tried.
# ---------------------------------------------------------------------------


def test_no_route_into_the_client_reaches_an_off_allowlist_host():
    """Every public way to issue a request, adversarially."""
    seen: list[str] = []
    client = build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport(seen)
    )
    with client:
        with pytest.raises(AllowlistViolation):
            client.get(OFF)
        with pytest.raises(AllowlistViolation):
            client.request("GET", OFF)
        with pytest.raises(AllowlistViolation):
            client.post(OFF, content=b"x")
        with pytest.raises(AllowlistViolation):
            client.send(client.build_request("GET", OFF))
        with pytest.raises(AllowlistViolation):
            with client.stream("GET", OFF):
                pass
        with pytest.raises(AllowlistViolation):
            request_with_retry(client, "GET", OFF, max_retries=0)
        # ...and the allowed host still works, so this is a gate, not a wall.
        assert client.get(ALLOWED).status_code == 200
    assert seen == [ALLOWED]


def test_userinfo_cannot_disguise_the_host():
    """`https://api.example.com@evil.example.net/x` reads as the allowed host
    to a human and to a naive `startswith`. The allowlist parses the host."""
    seen: list[str] = []
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport(seen)
    ) as client:
        with pytest.raises(AllowlistViolation) as exc:
            client.get("https://api.example.com@evil.example.net/x")
        assert "evil.example.net" in str(exc.value)
        with pytest.raises(AllowlistViolation):
            client.get("https://api.example.com:tok@evil.example.net/x")
    assert seen == []


def test_a_port_does_not_change_the_host_decision():
    """The allowlist is host-based. A port neither smuggles a host in nor
    locks an allowed one out."""
    seen: list[str] = []
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport(seen)
    ) as client:
        assert client.get("https://api.example.com:8443/x").status_code == 200
        with pytest.raises(AllowlistViolation):
            client.get("https://evil.example.net:443/x")
    assert seen == ["https://api.example.com:8443/x"]


def test_a_redirect_target_is_still_checked():
    """Redirects are never followed in production — but if a follower were
    ever reinstated, the guard must sit under each hop rather than only under
    the first. The event hook does; a check at the call site would not."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"Location": OFF})
        return httpx.Response(200, json={"ok": True})

    with build_client(
        allowlist=HostAllowlist(["api.example.com"]),
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(AllowlistViolation):
            client.get(ALLOWED)
    # The first hop was issued; the redirect target never was.
    assert seen == [ALLOWED]


def test_reassigning_event_hooks_cannot_unhook_the_guard():
    """`client.event_hooks = {}` is the obvious way to remove a hook."""
    seen: list[str] = []
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport(seen)
    ) as client:
        client.event_hooks = {}
        with pytest.raises(AllowlistViolation):
            client.get(OFF)
        client.event_hooks = {"request": [lambda r: None], "response": []}
        with pytest.raises(AllowlistViolation):
            client.get(OFF)
    assert seen == []


def test_request_with_retry_does_not_retry_an_allowlist_violation():
    """A policy decision does not become allowed by asking again, and burning
    the retry budget on it delays the real error by seconds."""
    sleeps: list[float] = []
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport()
    ) as client:
        with pytest.raises(AllowlistViolation):
            request_with_retry(
                client, "GET", OFF, max_retries=3, sleeper=sleeps.append
            )
    assert sleeps == []


# ---------------------------------------------------------------------------
# D1: narrowing. `cmd_fetch` genuinely needs a per-call policy, so the client
# has one — and it can only ever restrict.
# ---------------------------------------------------------------------------


def test_narrowing_restricts_and_restores():
    seen: list[str] = []
    with build_client(
        allowlist=HostAllowlist(["api.example.com", "cdn.example.com"]),
        transport=_ok_transport(seen),
    ) as client:
        assert client.get("https://cdn.example.com/a").status_code == 200
        with client.narrowed(HostAllowlist(["api.example.com"])):
            assert client.get(ALLOWED).status_code == 200
            with pytest.raises(AllowlistViolation):
                client.get("https://cdn.example.com/b")
        # Outer policy restored.
        assert client.get("https://cdn.example.com/c").status_code == 200
    assert seen == [
        "https://cdn.example.com/a",
        ALLOWED,
        "https://cdn.example.com/c",
    ]


def test_narrowing_cannot_reach_a_host_the_outer_allowlist_rejects():
    """The whole point. A narrowing that names a new host is a widening
    wearing a narrowing's name, and it fails loudly rather than permitting."""
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport()
    ) as client:
        with pytest.raises(AllowlistViolation) as exc:
            with client.narrowed(HostAllowlist(["evil.example.net"])):
                pass
        assert "never widens" in str(exc.value)
        # Not even partially: one allowed host does not carry an unallowed one in.
        with pytest.raises(AllowlistViolation):
            with client.narrowed(
                HostAllowlist(["api.example.com", "evil.example.net"])
            ):
                pass
        # And nesting cannot climb back out to the outer scope.
        with client.narrowed(HostAllowlist(["api.example.com"])):
            with pytest.raises(AllowlistViolation):
                with client.narrowed(HostAllowlist(["cdn.example.com"])):
                    pass


def test_narrowing_refuses_an_empty_allowlist():
    """`HostAllowlist([])` permits everything, so installing one narrows
    nothing and widens the policy to the whole internet — failure mode 4 in a
    different hat. A raw list is refused too: `["api.example.com"]` and
    `HostAllowlist(["api.example.com"])` must not be interchangeable here."""
    with build_client(
        allowlist=HostAllowlist(["api.example.com"]), transport=_ok_transport()
    ) as client:
        for empty in (HostAllowlist([]), HostAllowlist([""]), HostAllowlist(None)):
            with pytest.raises(ValueError):
                with client.narrowed(empty):
                    pass
        with pytest.raises(TypeError):
            with client.narrowed(["api.example.com"]):
                pass


def test_narrowing_an_unrestricted_client_still_restricts():
    """A client built without an allowlist permits everything, matching
    `check`. Narrowing it is a real restriction, not a no-op."""
    with build_client(transport=_ok_transport()) as client:
        assert client.get(OFF).status_code == 200
        with client.narrowed(HostAllowlist(["api.example.com"])):
            with pytest.raises(AllowlistViolation):
                client.get(OFF)
        assert client.get(OFF).status_code == 200


def test_narrowing_is_restored_when_the_block_raises():
    with build_client(
        allowlist=HostAllowlist(["api.example.com", "cdn.example.com"]),
        transport=_ok_transport(),
    ) as client:
        with pytest.raises(RuntimeError):
            with client.narrowed(HostAllowlist(["api.example.com"])):
                raise RuntimeError("boom")
        assert client.get("https://cdn.example.com/a").status_code == 200
