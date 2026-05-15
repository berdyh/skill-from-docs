"""Tests for `openapi-harvest auth`."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from skill_from_docs import cmd_auth


def _args(**overrides):
    base = dict(
        endpoint="https://api.example.com/v1/locations",
        token="real-secret-token",
        output=None,
        short_circuit=True,
        include_query_auth=False,
        basic_creds=None,
        bad_token_pattern=cmd_auth.FIXED_BAD_TOKEN,
        allow_host=["api.example.com"],
        follow_redirects=False,
        timeout=2.0,
        workspace=None,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _make_transport(handler):
    return httpx.MockTransport(handler)


def test_bearer_success_short_circuits(tmp_path: Path, capsys):
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        seen.append(auth)
        if auth == "Bearer real-secret-token":
            return httpx.Response(200, json={"locations": []}, headers={"X-RateLimit-Limit": "3600"})
        if auth == "" or auth == "Bearer aaaaaaaa-bad-token-bbbbbbbb":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(401, json={"error": "unauthorized"})

    args = _args(workspace=str(tmp_path))
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0
    # baseline + bad-token + Bearer success (short-circuits)
    # Bearer is first in cascade, so we should NOT see Token / X-API-Key
    bearer_count = sum(1 for s in seen if s == "Bearer real-secret-token")
    assert bearer_count == 1
    assert "Token real-secret-token" not in seen


def test_all_patterns_fail_exits_4(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    args = _args(workspace=str(tmp_path))
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 4


def test_bad_token_is_fixed_string(tmp_path: Path):
    """The fixed bad token must be the literal string, never derived from
    the real token. We check this by inspecting the seen headers.
    """
    seen_bt: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        if "aaaaaaaa" in auth:
            seen_bt.append(auth)
        return httpx.Response(401, json={})

    args = _args(workspace=str(tmp_path))
    cmd_auth.run(args, transport=_make_transport(handler))
    assert seen_bt
    assert seen_bt[0] == "Bearer aaaaaaaa-bad-token-bbbbbbbb"


def test_basic_auth_only_when_opt_in(tmp_path: Path):
    seen_basic: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("Authorization", "").startswith("Basic "):
            seen_basic.append(True)
        return httpx.Response(401)

    # default: Basic is NOT in cascade
    cmd_auth.run(_args(workspace=str(tmp_path)), transport=_make_transport(handler))
    assert seen_basic == []

    # opt-in
    cmd_auth.run(
        _args(workspace=str(tmp_path), basic_creds="u:p"),
        transport=_make_transport(handler),
    )
    assert seen_basic == [True]


def test_query_auth_only_when_opt_in(tmp_path: Path):
    seen_q: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.query:
            seen_q.append(str(req.url.query))
        return httpx.Response(401)

    cmd_auth.run(_args(workspace=str(tmp_path)), transport=_make_transport(handler))
    assert seen_q == []

    cmd_auth.run(
        _args(workspace=str(tmp_path), include_query_auth=True),
        transport=_make_transport(handler),
    )
    # 4 query-string variants attempted
    assert len(seen_q) == 4


def test_host_allowlist_violation_exits_1(tmp_path: Path):
    args = _args(
        endpoint="https://attacker.example.com/v1/locations",
        workspace=str(tmp_path),
        allow_host=["api.example.com"],
    )
    rc = cmd_auth.run(args)
    assert rc == 1


def test_missing_allow_host_exits_1(tmp_path: Path):
    args = _args(workspace=str(tmp_path), allow_host=[])
    rc = cmd_auth.run(args)
    assert rc == 1
