"""Unit tests for the redaction policy."""

from __future__ import annotations


from urllib.parse import parse_qsl, urlparse

from skill_from_docs._redaction import (
    DEFAULT_BODY_KEYS,
    REDACTED,
    SENSITIVE_QUERY_KEYS,
    compile_patterns,
    redact_body,
    redact_headers,
    redact_text,
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


def test_redact_url_preserves_percent_encoding():
    """parse_qsl hands back DECODED text; writing it back raw corrupted the URL.
    `?q=one%26two` became `?q=one&two` — one parameter silently became two, in a
    fixture that claims to record the request actually sent."""
    cases = {
        "https://x/a?q=one%26two": "https://x/a?q=one%26two",
        "https://x/a?filter=a%3Db": "https://x/a?filter=a%3Db",
        "https://x/a?tag=a%2Bb": "https://x/a?tag=a%2Bb",
        "https://x/a?n=%C3%A9": "https://x/a?n=%C3%A9",
    }
    for src, expected in cases.items():
        assert redact_url(src) == expected


def test_redact_url_roundtrips_to_the_same_pairs():
    for src in (
        "https://x/a?q=one%26two&name=x%20y",
        "https://x/a?tag=a%2Bb&other=plain",
        "https://x/a?k[]=1&k[]=2",
    ):
        before = parse_qsl(urlparse(src).query, keep_blank_values=True)
        after = parse_qsl(urlparse(redact_url(src)).query, keep_blank_values=True)
        assert before == after


def test_redact_url_is_idempotent():
    """`probe` redacts the URL into the fixture and `quick-diff` redacts it
    again for its report. The damage used to compound across those two passes:
    `a%2Bb` became `a+b` in the fixture, then `a b` in the report."""
    for src in (
        "https://x/a?tag=a%2Bb&token=secret",
        "https://x/a?q=one%26two",
        "https://api.x/t?client_secret=REAL",
    ):
        once = redact_url(src)
        assert redact_url(once) == once


def test_query_keys_cover_the_same_credentials_as_body_keys():
    """A credential is no less sensitive for arriving in a query string — and a
    query string additionally reaches logs, proxies and CDN caches."""
    for key in DEFAULT_BODY_KEYS:
        assert key.lower() in SENSITIVE_QUERY_KEYS, key
    redacted = redact_url("https://api.x/oauth/token?client_secret=REAL&refresh_token=R2")
    assert "REAL" not in redacted
    assert "R2" not in redacted


def test_redacted_sentinel_is_not_percent_encoded():
    """The sentinel stays readable in fixtures and logs — the one deliberate
    exception to re-encoding."""
    assert redact_url("https://x/a?token=abc") == "https://x/a?token=<redacted>"


def test_encoded_sensitive_key_is_still_recognised():
    """Decoding before the sensitivity check is what makes this work."""
    assert redact_url("https://x/a?%74oken=secret") == "https://x/a?token=<redacted>"


def test_redact_text_redacts_urls_embedded_in_free_text():
    """`redact_url` only helps when you are holding a URL. Credentials also
    travel inside strings that merely contain one — an exception message."""
    msg = 'connection failed for https://api.example.com/v1?api_key=s3cr3t&page=2 (retrying)'
    out = redact_text(msg)
    assert "s3cr3t" not in out
    assert "api_key=<redacted>" in out
    assert "page=2" in out
    assert out.startswith("connection failed for ")
    assert out.endswith(" (retrying)")


def test_redact_text_leaves_credential_free_text_alone():
    assert redact_text("no urls here") == "no urls here"
    assert redact_text("see https://example.com/docs") == "see https://example.com/docs"


def test_redact_text_handles_several_urls_in_one_string():
    out = redact_text("a https://x.test/?token=A then https://y.test/?token=B")
    assert "A" not in out.split("then")[0].split("token=")[1]
    assert out.count("<redacted>") == 2


def test_redact_url_redacts_userinfo_credentials():
    """`https://user:pass@host/...` passes the allowlist — urlparse().hostname
    strips userinfo — so the credential reaches source-map.json, docs.md,
    handoff.json and every fixture's spec_url_at_capture."""
    assert redact_url("https://user:s3cr3t@api.example.com/spec.json") == (
        f"https://{REDACTED}@api.example.com/spec.json"
    )
    assert redact_url("https://tok3n@api.example.com:8443/x") == (
        f"https://{REDACTED}@api.example.com:8443/x"
    )
    out = redact_url("https://u:p@h/v1?api_key=k&page=2")
    assert "p@" not in out and "=k" not in out
    assert "page=2" in out
    # No userinfo: the netloc is left exactly as it was.
    assert redact_url("https://api.example.com/x?a=1") == "https://api.example.com/x?a=1"


def test_redact_url_does_not_gratuitously_encode_benign_values():
    """A recorded URL is an audit artifact. Only the characters that could
    split one parameter into two are escaped."""
    assert redact_url("https://h/p?filter=a/b,c") == "https://h/p?filter=a/b,c"
    assert redact_url("https://h/p?cb=https://x.test/done") == "https://h/p?cb=https://x.test/done"
    # ...but the split-causing ones still are, which is the bug this guards.
    assert redact_url("https://h/p?q=one%26two") == "https://h/p?q=one%26two"


def test_redact_text_is_idempotent():
    """cmd_auth redacts URLs elsewhere, so an error string can already contain
    the sentinel; redacting twice must not append a second one."""
    once = redact_text("failed for https://h/p?token=abc")
    assert once == redact_text(once)
    assert once.count(REDACTED) == 1
