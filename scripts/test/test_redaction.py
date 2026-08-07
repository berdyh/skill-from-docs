"""Unit tests for the redaction policy."""

from __future__ import annotations


from skill_from_docs._redaction import (
    REDACTED,
    compile_patterns,
    redact_body,
    redact_headers,
    redact_url,
)


def test_header_redaction_covers_default_set():
    headers = {
        "Authorization": "Bearer secret",
        "X-API-Key": "k-123",
        "X-Auth-Token": "t-456",
        "Cookie": "session=xyz",
        "Set-Cookie": "session=xyz; Path=/",
        "Location": "https://attacker.example.com/?token=stolen",
        "Content-Type": "application/json",
    }
    out = redact_headers(headers)
    assert out["Authorization"] == REDACTED
    assert out["X-API-Key"] == REDACTED
    assert out["X-Auth-Token"] == REDACTED
    assert out["Cookie"] == REDACTED
    assert out["Set-Cookie"] == REDACTED
    assert out["Location"] == REDACTED
    assert out["Content-Type"] == "application/json"


def test_body_key_redaction_default_keys():
    body = {
        "user": "alice",
        "token": "super-secret",
        "nested": {"api_key": "abc", "fine": "ok"},
        "list": [{"password": "p1"}, {"label": "x"}],
    }
    out = redact_body(body)
    assert out["token"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["fine"] == "ok"
    assert out["list"][0]["password"] == REDACTED
    assert out["list"][1]["label"] == "x"


def test_body_pattern_redaction():
    body = {"description": "call with key abc-123-def"}
    patterns = compile_patterns([r"abc-\d+-def"])
    out = redact_body(body, patterns=patterns)
    assert "abc-" not in out["description"]
    assert REDACTED in out["description"]


def test_url_query_redaction():
    url = "https://api.example.com/things?api_key=secret&page=1"
    out = redact_url(url)
    assert "secret" not in out
    assert REDACTED in out
    assert "page=1" in out


def test_set_cookie_and_location_redaction():
    headers = {"Set-Cookie": "x=1", "Location": "/anywhere"}
    out = redact_headers(headers)
    assert out["Set-Cookie"] == REDACTED
    assert out["Location"] == REDACTED


def test_http_client_ignores_env_proxies(monkeypatch):
    """B4: trust_env=False prevents HTTP_PROXY/HTTPS_PROXY hijacking of
    token-bearing requests through an attacker-controlled proxy."""
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:9999")
    monkeypatch.setenv("NO_PROXY", "")

    from skill_from_docs._http import build_client

    with build_client() as client:
        # httpx merges env proxies into client config when trust_env is True.
        # With trust_env=False the client never reads the env, so:
        assert client.trust_env is False
        # No proxy mounts should be installed from env.
        for mount in getattr(client, "_mounts", {}):
            # If any mount is configured, it should not be the attacker host.
            # (Default httpx.Client has no env-derived mounts when trust_env=False.)
            assert "attacker.example" not in repr(mount)


def test_url_query_redaction_in_manifest_and_auth_markdown(tmp_path):
    """B1: URL with sensitive query params must be redacted in manifest entries
    and in `auth` markdown output."""
    from skill_from_docs._manifest import record_run, load_manifest

    raw_url = "https://api.example.com/x?api_key=secret&token=abc&page=2"
    record_run(
        str(tmp_path),
        subcommand="probe",
        args={"url": raw_url, "method": "GET", "scope": "ad-hoc"},
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
    )
    data = load_manifest(str(tmp_path))
    persisted = data["runs"][0]["args"]["url"]
    assert "api_key=<redacted>" in persisted
    assert "token=<redacted>" in persisted
    assert "page=2" in persisted
    assert "secret" not in persisted
    assert "abc" not in persisted

    # Verify auth markdown emission via _format_markdown
    from skill_from_docs.cmd_auth import _format_markdown

    md = _format_markdown(
        {
            "endpoint": raw_url,
            "captured_at": "2026-01-01T00:00:00Z",
            "unauthenticated": {"status": 401, "www_authenticate": None, "body": {}},
            "bad_token": {"status": 401, "body": {}},
            "attempts": [],
            "winner": None,
            "rate_limit_headers": {},
        }
    )
    assert "secret" not in md
    assert "api_key=<redacted>" in md or "<redacted>" in md
