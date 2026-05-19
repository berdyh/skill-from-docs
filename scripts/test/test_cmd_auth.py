"""Tests for `openapi-harvest auth`."""

from __future__ import annotations

import argparse
import json
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
        basic_creds_env=None,
        spec=None,
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


def test_auth_emits_fixture_and_provenance_comment(tmp_path: Path, capsys):
    """H5: auth must write a probes/auth-<host>-<status>.json fixture and
    the markdown must carry a `scope: auth-discovery` provenance comment so
    `validate` can index it as a source."""

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        if auth == "Bearer real-secret-token":
            return httpx.Response(200, json={"ok": True}, headers={"X-RateLimit-Limit": "10"})
        return httpx.Response(
            401,
            json={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer realm=api"},
        )

    args = _args(workspace=str(tmp_path))
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0

    # Fixture written under probes/
    probes_dir = tmp_path / "probes"
    fixtures = list(probes_dir.glob("auth-*.json"))
    assert len(fixtures) == 1, f"expected 1 auth fixture, got {fixtures}"
    fixture = json.loads(fixtures[0].read_text())
    assert fixture["scope"] == "auth-discovery"
    assert fixture["request"]["url"].startswith("https://api.example.com")

    # Markdown carries the provenance comment
    out = capsys.readouterr().out
    assert "<!-- probe:" in out
    assert "scope: auth-discovery" in out
    assert f"fixture: probes/{fixtures[0].name}" in out


def test_auth_url_redacted_in_markdown(tmp_path: Path, capsys):
    """B1: a credential-bearing endpoint URL must be redacted in the captured
    markdown."""

    def handler(req):
        return httpx.Response(401, json={})

    args = _args(
        endpoint="https://api.example.com/v1/x?api_key=ABC123&token=DEF",
        workspace=str(tmp_path),
    )
    cmd_auth.run(args, transport=_make_transport(handler))
    out = capsys.readouterr().out
    assert "ABC123" not in out
    assert "DEF" not in out
    assert "<redacted>" in out




# ---------------------------------------------------------------------------
# Auth-method policy tests: header preferred / query warned / basic env-var /
# spec-aware filtering.
# ---------------------------------------------------------------------------


def test_basic_creds_env_resolves_from_environment(tmp_path: Path, monkeypatch):
    """--basic-creds-env reads USER:PASS from the named env var (preferred path)."""
    monkeypatch.setenv("EXAMPLE_BASIC_CREDS", "alice:s3cret")

    seen_auth_headers: list[str] = []

    def handler(req):
        auth = req.headers.get("Authorization", "")
        seen_auth_headers.append(auth)
        # Bearer succeeds first — Basic never runs but we verify the resolver works.
        return httpx.Response(200 if auth.startswith("Bearer ") else 401, json={})

    args = _args(
        workspace=str(tmp_path),
        basic_creds_env="EXAMPLE_BASIC_CREDS",
        short_circuit=False,  # exercise the full cascade including Basic
    )
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0
    # Basic header is base64-encoded alice:s3cret = YWxpY2U6czNjcmV0
    assert any("Basic YWxpY2U6czNjcmV0" in h for h in seen_auth_headers)


def test_basic_creds_env_missing_var_exits_1(tmp_path: Path, capsys):
    """--basic-creds-env pointing at an unset env var exits 1."""
    args = _args(
        workspace=str(tmp_path),
        basic_creds_env="NONEXISTENT_VAR_12345",
    )
    rc = cmd_auth.run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "NONEXISTENT_VAR_12345" in err
    assert "is not set or is empty" in err


def test_basic_creds_and_env_are_mutually_exclusive(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("EXAMPLE_BASIC_CREDS", "alice:pass")
    args = _args(
        workspace=str(tmp_path),
        basic_creds="alice:pass",
        basic_creds_env="EXAMPLE_BASIC_CREDS",
    )
    rc = cmd_auth.run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_basic_creds_cli_warns_about_shell_history(tmp_path: Path, capsys):
    """--basic-creds (CLI) emits a stderr warning recommending --basic-creds-env."""

    def handler(req):
        return httpx.Response(401, json={})

    args = _args(
        workspace=str(tmp_path),
        basic_creds="alice:s3cret",
    )
    cmd_auth.run(args, transport=_make_transport(handler))
    err = capsys.readouterr().err
    assert "shell history" in err
    assert "--basic-creds-env" in err


def test_security_guidance_emitted_when_winner_is_query_string(tmp_path: Path, capsys):
    """Query-string winner triggers the security-guidance markdown section."""

    def handler(req):
        # Only succeed for query-string ?api_key=
        if "api_key=real-secret-token" in str(req.url):
            return httpx.Response(200, json={})
        return httpx.Response(401, json={})

    args = _args(
        workspace=str(tmp_path),
        include_query_auth=True,
    )
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Security guidance" in out
    assert "auth_method: `query_string`" in out
    assert "logs, proxies" in out  # the specific warning text


def test_security_guidance_emitted_when_winner_is_basic(tmp_path: Path, capsys):
    """Basic winner triggers an env-vars guidance line."""

    def handler(req):
        auth = req.headers.get("Authorization", "")
        # Basic wins; Bearer does not
        if auth.startswith("Basic "):
            return httpx.Response(200, json={})
        return httpx.Response(401, json={})

    args = _args(
        workspace=str(tmp_path),
        basic_creds="alice:s3cret",
    )
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Security guidance" in out
    assert "auth_method: `basic`" in out
    assert "environment variables" in out


def test_security_guidance_absent_when_winner_is_bearer(tmp_path: Path, capsys):
    """Bearer / API-key-header winners do NOT emit security guidance."""

    def handler(req):
        return httpx.Response(200, json={})  # Bearer wins immediately

    args = _args(workspace=str(tmp_path))
    rc = cmd_auth.run(args, transport=_make_transport(handler))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Security guidance" not in out


def test_spec_filtering_keeps_only_bearer_when_spec_declares_bearer(tmp_path: Path):
    """When spec declares bearer-only, the cascade tries only Bearer (+ tolerance aliases)."""
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        }
    }))

    attempted: list[str] = []

    def handler(req):
        attempted.append(req.headers.get("Authorization", ""))
        # Don't succeed; we just want to inspect what was attempted.
        return httpx.Response(401, json={})

    args = _args(
        workspace=str(tmp_path / "ws"),
        spec=str(spec_path),
        short_circuit=False,
    )
    cmd_auth.run(args, transport=_make_transport(handler))
    # Only Bearer / Token-alias / raw Authorization should be tried.
    # NOT X-API-Key, NOT X-Auth-Token, NOT Api-Key, NOT Token custom-header.
    for h in attempted:
        # The unauth baseline + bad-token also fire — skip those.
        if not h or h.startswith("Bearer aaaaaaaa-bad-token"):
            continue
        # Allowed: Bearer real-secret-token, Token real-secret-token, raw real-secret-token
        assert h in (
            "Bearer real-secret-token",
            "Token real-secret-token",
            "real-secret-token",
        ), f"Unexpected pattern attempted: {h!r}"


def test_spec_filtering_drops_query_when_spec_declares_header(tmp_path: Path, capsys):
    """Prefer-header-automatically: query-string patterns drop even if --include-query-auth."""
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        }
    }))

    seen_urls: list[str] = []

    def handler(req):
        seen_urls.append(str(req.url))
        return httpx.Response(401, json={})

    args = _args(
        workspace=str(tmp_path / "ws"),
        spec=str(spec_path),
        include_query_auth=True,  # explicitly enabled; spec preference overrides
        short_circuit=False,
    )
    cmd_auth.run(args, transport=_make_transport(handler))
    # No request URL should contain ?api_key= / ?token= / ?access_token= / ?key=
    for url in seen_urls:
        assert "api_key=" not in url, f"Query-string auth attempted: {url}"
        assert "?token=" not in url, f"Query-string auth attempted: {url}"
        assert "?access_token=" not in url, f"Query-string auth attempted: {url}"
        # ?key= may legitimately appear elsewhere but never as auth here
    err = capsys.readouterr().err
    assert "prefer-header-automatically" in err.lower() or "header-based" in err


def test_handoff_carries_auth_method_in_fixture_manifest(tmp_path: Path):
    """The auth fixture's manifest carries auth_method + security_warnings so
    consolidate can propagate them into handoff.json."""

    def handler(req):
        return httpx.Response(200, json={})  # Bearer wins

    args = _args(workspace=str(tmp_path))
    cmd_auth.run(args, transport=_make_transport(handler))
    fixtures = list((tmp_path / "probes").glob("auth-*.json"))
    assert len(fixtures) == 1
    data = json.loads(fixtures[0].read_text())
    assert data["manifest"]["auth_method"] == "bearer"
    assert data["manifest"]["security_warnings"] == []


def test_handoff_carries_query_string_warnings_in_fixture_manifest(tmp_path: Path):
    def handler(req):
        if "api_key=real-secret-token" in str(req.url):
            return httpx.Response(200, json={})
        return httpx.Response(401, json={})

    args = _args(workspace=str(tmp_path), include_query_auth=True)
    cmd_auth.run(args, transport=_make_transport(handler))
    fixtures = list((tmp_path / "probes").glob("auth-*.json"))
    data = json.loads(fixtures[0].read_text())
    assert data["manifest"]["auth_method"] == "query_string"
    assert len(data["manifest"]["security_warnings"]) >= 1
    assert "logs" in data["manifest"]["security_warnings"][0]
