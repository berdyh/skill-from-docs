"""Tests for `openapi-harvest consolidate`."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse


from skill_from_docs import cmd_consolidate
from skill_from_docs._schema import ProbeFixture


def _args(workspace: str, **overrides):
    base = dict(
        workspace=workspace,
        merge_probes=False,
        tag=[],
        narrative_dir=None,
        emit_handoff=True,
        sanitize=True,
        dry_run=False,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _set_spec(ws: Path, spec) -> None:
    raw = ws / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "spec.json").write_text(json.dumps(spec))


def _set_source_map(ws: Path, sm) -> None:
    raw = ws / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "source-map.json").write_text(json.dumps(sm))


def test_spec_only(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir(exist_ok=True)
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    rc = cmd_consolidate.run(_args(str(tmp_path)))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "# Tiny API" in docs
    assert "## API reference" in docs
    assert "### Tag: Locations" in docs
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    assert handoff["archetype_primary"] == 4
    assert handoff["content_shape_signals"]["endpoint_count"] == 2


def test_spec_plus_probes(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    shutil.copy(
        fixtures_dir / "locations-200.json", tmp_path / "probes" / "locations-200.json"
    )
    rc = cmd_consolidate.run(_args(str(tmp_path), merge_probes=True))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "<!-- probe:" in docs


def test_spec_plus_narrative(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "narrative").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    (tmp_path / "narrative" / "installation.md").write_text("Use pip.")
    (tmp_path / "narrative" / "rate-limits.md").write_text("100 req/hour.")
    rc = cmd_consolidate.run(_args(str(tmp_path)))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "Use pip." in docs
    assert "100 req/hour." in docs


def test_tag_filter_single(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    rc = cmd_consolidate.run(_args(str(tmp_path), tag=["Locations"]))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "### Tag: Locations" in docs


def test_tag_filter_none_match(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    rc = cmd_consolidate.run(_args(str(tmp_path), tag=["DoesNotExist"]))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "_No endpoints match the filter._" in docs


def test_probe_references_unknown_endpoint_warns(tmp_path: Path, fixtures_dir: Path, capsys):
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    # probe URL doesn't match spec's /locations
    unknown_probe = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "https://api.example.com/unknown", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {}, "timing_ms": 0},
        "manifest": {"tool_version": "", "captured_at": "", "spec_url_at_capture": None, "spec_sha256_at_capture": None},
    }
    (tmp_path / "probes" / "unknown.json").write_text(json.dumps(unknown_probe))
    cmd_consolidate.run(_args(str(tmp_path), merge_probes=True, quiet=False))
    err = capsys.readouterr().err
    assert "does not match any spec endpoint" in err


def test_partial_coverage_emits_todo(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    # No probes for the Locations tag
    cmd_consolidate.run(_args(str(tmp_path), merge_probes=True))
    docs = (tmp_path / "docs.md").read_text()
    assert "<!-- TODO" in docs


def test_prompt_injection_sanitized(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "poisoned-spec.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(_args(str(tmp_path), quiet=False))
    docs = (tmp_path / "docs.md").read_text()
    # the injected "<!-- source: ... evil -->" markers escaped
    assert "<!- -" in docs or "Ignore previous instructions" not in docs
    # The agent-instruction pattern should be stripped:
    assert "[stripped]" in docs or "Ignore" not in docs


def test_dry_run_does_not_write(tmp_path: Path, fixtures_dir: Path, capsys):
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    rc = cmd_consolidate.run(_args(str(tmp_path), dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Tiny API" in out
    assert not (tmp_path / "docs.md").exists()
    assert not (tmp_path / "handoff.json").exists()


def test_handoff_emission(tmp_path: Path, fixtures_dir: Path):
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(_args(str(tmp_path)))
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    assert handoff["version"] == 1
    assert handoff["archetype_primary"] == 4
    assert handoff["content_shape_signals"]["has_openapi_spec"] is True
    assert "provenance_index" in handoff


def test_consolidate_narrative_emits_provenance(tmp_path: Path, fixtures_dir: Path):
    """H6: narrative sections must carry a `<!-- source: ... raw_file: narrative/... -->`
    provenance comment so `validate` accepts them."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "narrative").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    (tmp_path / "narrative" / "authentication.md").write_text("Use a Bearer token.")
    (tmp_path / "narrative" / "errors.md").write_text("HTTP status codes.")
    (tmp_path / "narrative" / "gotchas.md").write_text("Watch out for ratelimits.")
    rc = cmd_consolidate.run(_args(str(tmp_path)))
    assert rc == 0
    docs = (tmp_path / "docs.md").read_text()
    # Authentication section already had provenance — keep that working.
    assert "Use a Bearer token." in docs
    # Errors + Gotchas now must have provenance comments
    assert "## Errors" in docs
    assert "raw_file: narrative/errors.md" in docs
    assert "raw_file: narrative/gotchas.md" in docs


def test_handoff_coverage_checklist_populated(tmp_path: Path, fixtures_dir: Path):
    """H9: handoff.coverage_checklist must list the 8 canonical sections
    with `name` + `status` (covered | partial | missing)."""
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(_args(str(tmp_path)))
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    cc = handoff["coverage_checklist"]
    assert isinstance(cc, list)
    names = {item["name"] for item in cc}
    expected = {
        "Installation",
        "Authentication",
        "Core concepts",
        "API reference",
        "Minimal working example",
        "Errors",
        "Rate limits",
        "Gotchas",
    }
    assert expected.issubset(names), f"missing items: {expected - names}"
    for item in cc:
        assert item["status"] in ("covered", "partial", "missing")


def test_handoff_suggested_test_cases_count(tmp_path: Path, fixtures_dir: Path):
    """H9: handoff.suggested_test_cases must have 3-5 derived suggestions."""
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(_args(str(tmp_path)))
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    stc = handoff["suggested_test_cases"]
    assert isinstance(stc, list)
    assert 3 <= len(stc) <= 5
    for entry in stc:
        assert entry.get("status") == "suggestion"


def test_sanitize_tag_name_with_heading_injection(tmp_path: Path):
    """H8: a tag name like `Foo\\n# INJECTED` must be sanitized so no orphan
    H1 leaks into docs.md output."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"tags": ["Foo\n# INJECTED HEADING\n"], "responses": {"200": {"description": "ok"}}}}
        },
    }
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "spec.json").write_text(json.dumps(spec))
    cmd_consolidate.run(_args(str(tmp_path)))
    docs = (tmp_path / "docs.md").read_text()
    # No line in docs.md should be a literal `# INJECTED HEADING`
    for line in docs.splitlines():
        assert line.strip() != "# INJECTED HEADING"


def test_sanitize_path_with_html_comment_injection(tmp_path: Path):
    """H8: a path containing `<!--` must be sanitized so it can't open a
    fake provenance comment."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/foo/<!--inject-->": {
                "get": {"tags": ["T"], "responses": {"200": {"description": "ok"}}}
            }
        },
    }
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "spec.json").write_text(json.dumps(spec))
    cmd_consolidate.run(_args(str(tmp_path)))
    docs = (tmp_path / "docs.md").read_text()
    # The endpoint heading must not contain a literal `<!--`
    headings = [ln for ln in docs.splitlines() if ln.startswith("#### `")]
    for h in headings:
        assert "<!--" not in h


def test_handoff_propagates_auth_method_from_auth_discovery_probe(tmp_path: Path):
    """Auth-discovery probes carry auth_method + security_warnings in their
    manifest. consolidate must lift them into handoff.content_shape_signals
    so skill-creator can decide what warnings the generated skill emits."""
    (tmp_path / "raw").mkdir()
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {"/x": {"get": {"summary": "x"}}},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    }
    (tmp_path / "raw" / "spec.json").write_text(json.dumps(spec))

    # Synthetic auth-discovery probe fixture.
    probes_dir = tmp_path / "probes"
    probes_dir.mkdir()
    fixture = {
        "scope": "auth-discovery",
        "request": {"method": "GET", "url": "https://api.example.com/x", "headers": {}, "body": None},
        "response": {"status": 401, "headers": {}, "body": None, "timing_ms": None},
        "manifest": {
            "tool_version": "0.1.0",
            "captured_at": "2026-05-16T00:00:00Z",
            "spec_url_at_capture": None,
            "spec_sha256_at_capture": None,
            "auth_method": "query_string",
            "security_warnings": [
                "Query-string credentials leak into logs, proxies, CDN caches.",
            ],
            "winner_pattern": "query ?api_key=",
            "bad_token_status": 401,
            "attempts": [],
        },
    }
    (probes_dir / "auth-api-example-com-401.json").write_text(json.dumps(fixture))

    cmd_consolidate.run(_args(str(tmp_path), merge_probes=True))
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    signals = handoff["content_shape_signals"]
    assert signals.get("auth_method") == "query_string"
    warnings = signals.get("security_warnings") or []
    assert any("logs" in w for w in warnings)


def test_handoff_omits_auth_method_when_no_auth_probe(tmp_path: Path):
    """Workspaces without auth-discovery probes get no auth_method key.
    Absent = not yet checked; readers must not infer false."""
    (tmp_path / "raw").mkdir()
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {"/x": {"get": {"summary": "x"}}},
    }
    (tmp_path / "raw" / "spec.json").write_text(json.dumps(spec))
    cmd_consolidate.run(_args(str(tmp_path)))
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    signals = handoff["content_shape_signals"]
    assert "auth_method" not in signals
    assert "security_warnings" not in signals


# ---------------------------------------------------------------------------
# ProbeIndex — the parse-once / memoise-per-path replacement for `_match_probe`
# ---------------------------------------------------------------------------


def _probe(url: str, name: str = "p.json", scope: str = "ad-hoc"):
    return (
        ProbeFixture.from_dict(
            {
                "scope": scope,
                "request": {"method": "GET", "url": url, "headers": {}, "body": None},
                "response": {"status": 200, "headers": {}, "body": {}, "timing_ms": 0},
                "manifest": {
                    "tool_version": "",
                    "captured_at": "",
                    "spec_url_at_capture": None,
                    "spec_sha256_at_capture": None,
                },
            }
        ),
        name,
    )


def _linear_scan(entries, path):
    """The pre-index implementation, verbatim: parse per call, suffix match."""
    out = []
    for fixture, name in entries:
        pp = urlparse(fixture.request.url).path
        if pp == path or pp.endswith(path):
            out.append((fixture, name))
    return out


def test_probe_index_equivalent_to_linear_scan():
    """The `path -> [probe]` index must answer exactly what the old
    linear-scan-with-urlparse-per-call answered, in the same order — including
    two probes that match the same path."""
    entries = [
        _probe("https://api.example.com/v1/locations", "a.json"),
        _probe("https://eu.example.com/locations", "b.json"),
        _probe("https://api.example.com/v1/locations?page=2", "c.json"),
        _probe("https://api.example.com/v1/servers", "d.json"),
        _probe("https://api.example.com/v1/servers/actions", "e.json"),
        _probe("relative/locations", "f.json"),
    ]
    index = cmd_consolidate.ProbeIndex(entries)
    for path in (
        "/locations",
        "/v1/locations",
        "/v1/servers",
        "/servers/actions",
        "/nope",
        "locations",
        "",
    ):
        assert index.for_path(path) == _linear_scan(entries, path), path
        assert index.has_match(path) == bool(_linear_scan(entries, path)), path

    # Four probe paths end in `/locations` (the query string is not part of the
    # path); all four come back, in load order.
    hits = index.for_path("/locations")
    assert [name for _f, name in hits] == ["a.json", "b.json", "c.json", "f.json"]

    # `unmatched` is the orphan scan: probes matching none of the spec's paths.
    orphans = index.unmatched(["/v1/locations", "/v1/servers"])
    assert [name for _f, name in orphans] == ["b.json", "e.json", "f.json"]


def test_probe_index_memoises_repeated_lookups():
    """Five passes ask the same questions; the scan must happen once per
    distinct path, not once per call."""
    entries = [_probe("https://api.example.com/v1/x", "a.json")]
    index = cmd_consolidate.ProbeIndex(entries)
    first = index.for_path("/v1/x")
    assert index._cache == {"/v1/x": [0]}
    assert index.for_path("/v1/x") == first
    # A path that matches nothing is cached too — `[]` must not be re-scanned.
    assert index.for_path("/v1/y") == []
    assert index._cache["/v1/y"] == []


def test_unparseable_probe_url_does_not_crash(tmp_path: Path, fixtures_dir: Path, capsys):
    """A fixture whose URL `urlparse` rejects (malformed IPv6 literal) used to
    raise straight out of `_match_probe`, taking the run down with a traceback
    and breaking the numeric exit-code contract. It must now simply match no
    endpoint."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    bad = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "http://[::1", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {}, "timing_ms": 0},
        "manifest": {
            "tool_version": "",
            "captured_at": "",
            "spec_url_at_capture": None,
            "spec_sha256_at_capture": None,
        },
    }
    (tmp_path / "probes" / "bad-url.json").write_text(json.dumps(bad))
    rc = cmd_consolidate.run(_args(str(tmp_path), merge_probes=True, quiet=False))
    assert rc == 0
    assert "does not match any spec endpoint" in capsys.readouterr().err


def test_two_probes_for_one_endpoint_both_reach_docs_and_handoff(tmp_path: Path, fixtures_dir: Path):
    """Both fixtures for the same path must appear, in load order, in docs.md
    and in `handoff.provenance_index`."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    for name in ("locations-a.json", "locations-b.json"):
        shutil.copy(fixtures_dir / "locations-200.json", tmp_path / "probes" / name)
    assert cmd_consolidate.run(_args(str(tmp_path), merge_probes=True)) == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "fixture: probes/locations-a.json" in docs
    assert "fixture: probes/locations-b.json" in docs
    handoff = json.loads((tmp_path / "handoff.json").read_text())
    entry = handoff["provenance_index"]["API reference > Tag: Locations > GET /locations"]
    assert [p["fixture"] for p in entry["probes"]] == [
        "probes/locations-a.json",
        "probes/locations-b.json",
    ]


def test_orphan_probe_scan_emits_one_message_per_probe(tmp_path: Path, fixtures_dir: Path, capsys):
    """Two probe-orphan scans were collapsed into one. They were mutually
    exclusive on `--tag`, so a probe that matches nothing must still produce
    exactly one warning, with the message the guard selects."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "probes").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    unknown = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "https://api.example.com/unknown", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {}, "timing_ms": 0},
        "manifest": {
            "tool_version": "",
            "captured_at": "",
            "spec_url_at_capture": None,
            "spec_sha256_at_capture": None,
        },
    }
    (tmp_path / "probes" / "unknown.json").write_text(json.dumps(unknown))

    cmd_consolidate.run(_args(str(tmp_path), merge_probes=True, quiet=False))
    warns = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("WARN:")]
    assert warns == [
        "WARN: probe GET https://api.example.com/unknown does not match any spec endpoint"
    ]

    cmd_consolidate.run(_args(str(tmp_path), merge_probes=True, tag=["Locations"], quiet=False))
    warns = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("WARN:")]
    assert warns == [
        "WARN: probe GET https://api.example.com/unknown references endpoint outside --tag filter"
    ]


def test_empty_narrative_file_is_treated_as_absent(tmp_path: Path, fixtures_dir: Path):
    """An empty `narrative/example.md` used to render as an empty body plus a
    provenance comment, because that one section tested `"example" in
    narratives` while the other five tested the body's truthiness. All nine
    sections now share one emitter, so an empty file falls back everywhere."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "narrative").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    (tmp_path / "narrative" / "example.md").write_text("")
    (tmp_path / "narrative" / "errors.md").write_text("")
    assert cmd_consolidate.run(_args(str(tmp_path))) == 0
    docs = (tmp_path / "docs.md").read_text()
    assert "<!-- TODO: provide a minimal working example -->" in docs
    assert "raw_file: narrative/example.md" not in docs
    assert "raw_file: narrative/errors.md" not in docs


def test_spec_is_walked_exactly_once(tmp_path: Path, fixtures_dir: Path, monkeypatch):
    """B3: the spec used to be traversed three times per run — twice to group
    by tag and once for the endpoint/tag counts. `WalkedSpec` is that walk."""
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    real = cmd_consolidate.iter_operations
    calls = []

    def counting(spec):
        calls.append(1)
        return real(spec)

    monkeypatch.setattr(cmd_consolidate, "iter_operations", counting)
    assert cmd_consolidate.run(_args(str(tmp_path), merge_probes=True)) == 0
    assert len(calls) == 1, f"spec traversed {len(calls)} times"
