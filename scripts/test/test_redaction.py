"""Unit tests for the redaction policy."""

from __future__ import annotations

import re

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
