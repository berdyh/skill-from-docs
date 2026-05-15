"""Tests for `openapi-harvest consolidate`."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from skill_from_docs import cmd_consolidate


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
