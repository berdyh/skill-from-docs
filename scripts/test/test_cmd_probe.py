"""Tests for `openapi-harvest probe`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest

from skill_from_docs import cmd_probe


def _args(**overrides):
    base = dict(
        url="https://api.example.com/v1/locations",
        method="GET",
        header=[],
        data=None,
        output=None,
        scope="case-study",
        no_redact=False,
        redact_body_key=[],
        redact_body_pattern=[],
        allow_host=["api.example.com"],
        max_retries=3,
        follow_redirects=False,
        dry_run=False,
        timeout=2.0,
        workspace=None,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _mock(handler):
    return httpx.MockTransport(handler)


def test_capture_redacts_headers_by_default(tmp_path: Path):
    def h(req):
        return httpx.Response(
            200,
            json={"locations": [{"id": 1}], "token": "sneaky"},
            headers={"Set-Cookie": "s=1"},
        )

    args = _args(
        workspace=str(tmp_path),
        header=["Authorization: Bearer real-secret"],
    )
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    fixture_path = next((tmp_path / "probes").iterdir())
    data = json.loads(fixture_path.read_text())
    assert data["request"]["headers"]["Authorization"] == "<redacted>"
    assert data["response"]["body"]["token"] == "<redacted>"
    # httpx response headers are lowercased; redaction regex is case-insensitive.
    assert data["response"]["headers"]["set-cookie"] == "<redacted>"


def test_json_request_body_keys_are_redacted(tmp_path: Path):
    """Regression: a JSON request body left as a string bypassed key redaction.

    `redact_body` only redacts by key while walking a dict, so a body kept as
    text wrote `{"password": "hunter2"}` verbatim into the fixture under the
    default policy.
    """

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data='{"password": "hunter2", "api_key": "sk-live-123", "region": "eu"}',
    )
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    fixture_path = next((tmp_path / "probes").iterdir())
    raw = fixture_path.read_text()
    assert "hunter2" not in raw
    assert "sk-live-123" not in raw
    body = json.loads(raw)["request"]["body"]
    assert body["password"] == "<redacted>"
    assert body["api_key"] == "<redacted>"
    # Non-sensitive keys survive — this is redaction, not deletion.
    assert body["region"] == "eu"


def test_form_encoded_request_body_is_structured(tmp_path: Path):
    """Form bodies are parsed into a dict so key-based redaction can reach
    them; non-sensitive keys round-trip unchanged."""

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data="name=widget&count=3",
    )
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    fixture_path = next((tmp_path / "probes").iterdir())
    assert json.loads(fixture_path.read_text())["request"]["body"] == {
        "name": "widget",
        "count": "3",
    }


def test_dry_run_redacts_json_request_body(tmp_path: Path, capsys):
    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data='{"password": "hunter2"}',
        dry_run=True,
    )
    rc = cmd_probe.run(args, transport=_mock(lambda req: httpx.Response(200)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert json.loads(out)["request"]["body"]["password"] == "<redacted>"


def test_no_redact_keeps_headers(tmp_path: Path):
    def h(req):
        return httpx.Response(200, json={"token": "kept"})

    args = _args(
        workspace=str(tmp_path),
        header=["Authorization: Bearer real-secret"],
        no_redact=True,
    )
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    fixture_path = next((tmp_path / "probes").iterdir())
    data = json.loads(fixture_path.read_text())
    assert data["request"]["headers"]["Authorization"] == "Bearer real-secret"
    assert data["response"]["body"]["token"] == "kept"


def test_redact_body_key_extra(tmp_path: Path):
    def h(req):
        return httpx.Response(200, json={"custom_secret": "kill"})

    args = _args(
        workspace=str(tmp_path), redact_body_key=["custom_secret"]
    )
    cmd_probe.run(args, transport=_mock(h))
    fixture_path = next((tmp_path / "probes").iterdir())
    data = json.loads(fixture_path.read_text())
    assert data["response"]["body"]["custom_secret"] == "<redacted>"


def test_redact_body_pattern(tmp_path: Path):
    def h(req):
        return httpx.Response(200, json={"note": "key abc-456-def lives here"})

    args = _args(workspace=str(tmp_path), redact_body_pattern=[r"abc-\d+-def"])
    cmd_probe.run(args, transport=_mock(h))
    fixture_path = next((tmp_path / "probes").iterdir())
    data = json.loads(fixture_path.read_text())
    assert "abc-456-def" not in data["response"]["body"]["note"]


def test_dry_run_redacts_and_skips_network(tmp_path: Path, capsys):
    def h(req):
        raise AssertionError("should not be called")

    args = _args(
        workspace=str(tmp_path),
        header=["Authorization: Bearer real-secret"],
        dry_run=True,
    )
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    out = capsys.readouterr().out
    assert "<redacted>" in out
    # no fixture written
    assert not list((tmp_path / "probes").iterdir())


def test_429_with_retry_after_is_honored(tmp_path: Path):
    counter = {"n": 0}
    sleeps: list[float] = []

    def h(req):
        counter["n"] += 1
        if counter["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"err": "x"})
        return httpx.Response(200, json={"ok": True})

    args = _args(workspace=str(tmp_path))
    rc = cmd_probe.run(
        args, transport=_mock(h), sleeper=sleeps.append
    )
    assert rc == 0
    assert sleeps == [1.0]


def test_5xx_exponential_backoff_max_3(tmp_path: Path):
    counter = {"n": 0}
    sleeps: list[float] = []

    def h(req):
        counter["n"] += 1
        return httpx.Response(503, json={"err": "down"})

    args = _args(workspace=str(tmp_path), max_retries=3)
    rc = cmd_probe.run(args, transport=_mock(h), sleeper=sleeps.append)
    assert rc == 2  # network error after retries
    # 3 retries with backoff = 1s, 2s, 4s
    assert sleeps == [1.0, 2.0, 4.0]


def test_transient_network_errors_are_retried(tmp_path: Path):
    """B2: `probe` is the subcommand most likely to hit a flaky live API and
    the only one exposing `--max-retries` — and it was the one that did NOT
    retry network errors, because its local retry loop was forked from
    `request_with_retry` before that helper grew the behaviour. The fork is
    gone; this pins what deleting it bought."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def h(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("connection reset", request=req)
        return httpx.Response(200, json={"ok": True})

    args = _args(workspace=str(tmp_path), max_retries=3)
    assert cmd_probe.run(args, transport=_mock(h), sleeper=sleeps.append) == 0
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_network_errors_still_give_up_after_max_retries(tmp_path: Path):
    """Retrying is bounded by --max-retries, and exhausting it is exit 2."""
    sleeps: list[float] = []

    def h(req):
        raise httpx.ConnectError("connection reset", request=req)

    args = _args(workspace=str(tmp_path), max_retries=2)
    assert cmd_probe.run(args, transport=_mock(h), sleeper=sleeps.append) == 2
    assert sleeps == [1.0, 2.0]


def test_redirect_blocked_by_default(tmp_path: Path):
    def h(req):
        # Return a 302 with a Location header. With follow_redirects=False
        # we capture this exact response and DO NOT auto-follow.
        return httpx.Response(
            302, headers={"Location": "https://attacker.example.com/?token=stolen"}
        )

    args = _args(workspace=str(tmp_path))
    rc = cmd_probe.run(args, transport=_mock(h))
    assert rc == 0
    fixture_path = next((tmp_path / "probes").iterdir())
    data = json.loads(fixture_path.read_text())
    assert data["response"]["status"] == 302
    # Location is redacted by default (httpx lowercases response headers)
    assert data["response"]["headers"]["location"] == "<redacted>"


def test_host_allowlist_violation_exits_1(tmp_path: Path):
    args = _args(
        url="https://attacker.example.com/x",
        workspace=str(tmp_path),
        allow_host=["api.example.com"],
    )
    rc = cmd_probe.run(args)
    assert rc == 1


def test_missing_allow_host_exits_1(tmp_path: Path):
    args = _args(workspace=str(tmp_path), allow_host=[])
    rc = cmd_probe.run(args)
    assert rc == 1


def test_redirects_are_never_followed(tmp_path: Path):
    """There is no flag that turns redirect-following on. Following one safely
    would mean reproducing httpx's cross-origin credential stripping on top of
    the allowlist check; the capability is not worth two subtle guards."""
    from skill_from_docs import openapi_harvest

    parser = openapi_harvest.build_parser()
    base = ["probe", "https://api.example.com/x", "--scope", "ad-hoc", "--allow-host", "api.example.com"]
    assert parser.parse_args(base).follow_redirects is False
    assert parser.parse_args(base + ["--no-follow-redirects"]).follow_redirects is False
    with pytest.raises(SystemExit):
        parser.parse_args(base + ["--follow-redirects"])


def test_proxy_authorization_is_redacted(tmp_path: Path):
    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path),
        header=["Proxy-Authorization: Basic c2VjcmV0"],
    )
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    raw = next((tmp_path / "probes").iterdir()).read_text()
    assert "c2VjcmV0" not in raw


def test_form_encoded_request_body_keys_are_redacted(tmp_path: Path):
    """An OAuth2 token request is the most credential-dense body this tool
    captures, and it is form-encoded, not JSON."""

    def h(req):
        return httpx.Response(200, json={"access_token": "x"})

    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data="grant_type=password&client_secret=SUPERSECRET&password=hunter2&scope=read",
        scope="auth-discovery",
    )
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    raw = next((tmp_path / "probes").iterdir()).read_text()
    assert "hunter2" not in raw
    body = json.loads(raw)["request"]["body"]
    assert body["password"] == "<redacted>"
    assert body["scope"] == "read"


def test_free_text_body_is_not_mistaken_for_a_form(tmp_path: Path):
    def h(req):
        return httpx.Response(200, json={"ok": True})

    for payload in ("just some free text", "<xml>a=b</xml>", "a=1\nb=2"):
        ws = tmp_path / payload[:4].replace("<", "_").replace("/", "_")
        args = _args(workspace=str(ws), method="POST", data=payload)
        assert cmd_probe.run(args, transport=_mock(h)) == 0
        assert json.loads(next((ws / "probes").iterdir()).read_text())["request"]["body"] == payload


def test_empty_allow_host_string_is_rejected(tmp_path: Path, capsys):
    """`--allow-host ""` from an unset shell var is a truthy arg list but an
    empty, permit-everything allowlist."""
    args = _args(workspace=str(tmp_path), allow_host=[""])
    assert cmd_probe.run(args, transport=_mock(lambda r: httpx.Response(200))) == 1
    assert "--allow-host" in capsys.readouterr().err


def test_base64_padded_form_value_still_redacted(tmp_path: Path):
    """`=` inside a value (base64 padding) is the common shape for a client
    secret; an over-strict form heuristic would drop it back to raw text."""

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data="client_secret=c2VjcmV0dmFsdWU=&grant_type=client_credentials",
        scope="auth-discovery",
    )
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    raw = next((tmp_path / "probes").iterdir()).read_text()
    assert "c2VjcmV0dmFsdWU" not in raw
    assert json.loads(raw)["request"]["body"]["client_secret"] == "<redacted>"


def test_repeated_form_keys_are_preserved(tmp_path: Path):
    """`scope=read&scope=write` must not collapse to the last value — the
    fixture claims to record the request that was actually sent."""

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path), method="POST", data="scope=read&scope=write&grant_type=x"
    )
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    body = json.loads(next((tmp_path / "probes").iterdir()).read_text())["request"]["body"]
    assert body["scope"] == ["read", "write"]


def test_base64_blob_body_is_not_mistaken_for_a_form(tmp_path: Path):
    """A padded base64 blob partitions into key=<blob>, sep='=', value=''.
    Converting it would move the secret into a dict KEY."""

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(
        workspace=str(tmp_path),
        method="POST",
        data="c2VjcmV0LWFwaS1rZXktYWJjMTIz=",
        redact_body_pattern=[r"c2VjcmV0[A-Za-z0-9+/=-]*"],
    )
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    raw = next((tmp_path / "probes").iterdir()).read_text()
    assert "c2VjcmV0LWFwaS1rZXktYWJjMTIz" not in raw


def test_nan_body_falls_through_to_text(tmp_path: Path):
    """json.loads accepts Python-only NaN; json.dump would then emit invalid
    JSON into a fixture another process has to read."""

    def h(req):
        return httpx.Response(200, json={"ok": True})

    args = _args(workspace=str(tmp_path), method="POST", data='{"lat": NaN}')
    assert cmd_probe.run(args, transport=_mock(h)) == 0
    raw = next((tmp_path / "probes").iterdir()).read_text()
    json.loads(raw)  # fixture must be strictly valid JSON
    assert json.loads(raw)["request"]["body"] == '{"lat": NaN}'


@pytest.mark.parametrize("bad", [0, 0.0, -1.0])
def test_non_positive_timeout_is_a_config_error_not_a_network_one(
    tmp_path: Path, capsys, bad
):
    """The same class as fetch's A10, one step less severe.

    Nothing rejected the degenerate value, so what happened next was the
    transport's business: against a real socket `request_with_retry` burns its
    whole 1s/2s/4s backoff on a request that could never be issued and then
    reports exit 2 — the code that means "retry" — never naming `--timeout`.
    Under the mock transport here it was worse still and simply passed, which is
    exactly what this test observes without the guard (rc 0, request issued).

    Reject it as user error, before a client exists and before the workspace is
    touched.
    """
    calls: list[str] = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"ok": True})

    args = _args(workspace=str(tmp_path), timeout=bad)
    assert cmd_probe.run(args, transport=_mock(h)) == 1
    assert "--timeout" in capsys.readouterr().err
    assert calls == []
    assert not (tmp_path / "probes").exists()


# --------------------------------------------------------------------------
# Workspace resolution
#
# `probe` used to derive its workspace from its own `--url`. In an archetype-4
# harvest the spec host and the live API host differ, so `fetch` populated one
# directory and `probe` silently created a second one next to it; `consolidate`
# then exited 3 on a workspace the user believed they had just populated. That
# is the documented Hetzner walkthrough, and it did not work.
# --------------------------------------------------------------------------


def _harvested(root: Path, name: str) -> Path:
    ws = root / ".claude" / "skill-from-docs" / name
    (ws / "raw").mkdir(parents=True)
    (ws / "raw" / "spec.json").write_text("{}")
    return ws


def test_probe_without_workspace_writes_into_the_harvested_one(
    tmp_path: Path, monkeypatch
):
    """The regression. `--url` names api.example.com; the harvest lives under a
    raw.githubusercontent.com slug. The fixture must land in the harvest."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _harvested(tmp_path, "raw.githubusercontent.com-acme-widgets")

    args = _args(workspace=None)
    assert cmd_probe.run(args, transport=_mock(lambda r: httpx.Response(200, json={}))) == 0

    assert len(list((ws / "probes").iterdir())) == 1
    siblings = sorted(p.name for p in (tmp_path / ".claude" / "skill-from-docs").iterdir())
    assert siblings == ["raw.githubusercontent.com-acme-widgets"]


def test_probe_without_workspace_refuses_when_no_harvest_exists(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))

    def h(req):
        raise AssertionError("must not reach the network")

    assert cmd_probe.run(_args(workspace=None), transport=_mock(h)) == 1
    err = capsys.readouterr().err
    assert "--workspace" in err and "fetch" in err
    assert not (tmp_path / ".claude" / "skill-from-docs").exists()


def test_probe_without_workspace_refuses_when_ambiguous(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = _harvested(tmp_path, "alpha")
    b = _harvested(tmp_path, "beta")

    def h(req):
        raise AssertionError("must not reach the network")

    assert cmd_probe.run(_args(workspace=None), transport=_mock(h)) == 1
    err = capsys.readouterr().err
    assert str(a) in err and str(b) in err
    assert not list((a / "probes").iterdir()) if (a / "probes").exists() else True


def test_probe_dry_run_needs_no_workspace(tmp_path: Path, monkeypatch, capsys):
    """A dry run writes nothing, so it must not be gated on a harvest."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def h(req):
        raise AssertionError("must not reach the network")

    assert cmd_probe.run(_args(workspace=None, dry_run=True), transport=_mock(h)) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not (tmp_path / ".claude").exists()
