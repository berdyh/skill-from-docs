"""Unit tests for provenance comment emit + parse."""

from __future__ import annotations

from skill_from_docs._provenance import (
    emit_probe,
    emit_source,
    find_all_provenance,
    parse_comment,
)


def test_emit_source_roundtrip():
    c = emit_source(
        "https://example.com/spec.json",
        retrieved="2026-05-01",
        raw_file="raw/spec.json",
        spec_pointer="/paths/~1v1~1locations/get",
    )
    parsed = parse_comment(c)
    assert parsed is not None
    assert parsed.kind == "source"
    assert parsed.fields["source"] == "https://example.com/spec.json"
    assert parsed.fields["retrieved"] == "2026-05-01"
    assert parsed.fields["raw_file"] == "raw/spec.json"
    assert parsed.fields["spec-pointer"] == "/paths/~1v1~1locations/get"


def test_emit_probe_roundtrip():
    c = emit_probe(
        "GET",
        "https://api.example.com/v1/locations",
        status=200,
        retrieved="2026-05-01",
        scope="case-study",
        fixture="probes/locations-200.json",
    )
    parsed = parse_comment(c)
    assert parsed is not None
    assert parsed.kind == "probe"
    assert parsed.fields["probe"] == "GET https://api.example.com/v1/locations"
    assert parsed.fields["status"] == "200"
    assert parsed.fields["scope"] == "case-study"
    assert parsed.fields["fixture"] == "probes/locations-200.json"


def test_find_all_provenance_in_markdown():
    md = """
# Doc

## Section

body

<!-- source: https://x.example.com retrieved: 2026-05-01 -->

## Other

<!-- probe: GET https://api.example.com/v1/x status: 200 retrieved: 2026-05-01 scope: case-study -->
"""
    entries = find_all_provenance(md)
    assert len(entries) == 2
    assert entries[0].kind == "source"
    assert entries[1].kind == "probe"
