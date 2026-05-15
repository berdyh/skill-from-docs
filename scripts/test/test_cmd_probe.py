"""Tests for `openapi-harvest probe`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

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
