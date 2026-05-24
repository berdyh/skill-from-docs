"""Tests for `openapi-harvest quick-diff`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skill_from_docs import cmd_quick_diff


def _args(fixture: str, spec: str, **overrides):
    base = dict(
        fixture=fixture, spec=spec, output=None, source_map=None, strict=False
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write(p: Path, data) -> str:
    p.write_text(json.dumps(data))
    return str(p)


def test_additive_drift(tmp_path: Path, capsys):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/things": {
                "get": {
                    "operationId": "list",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["items"],
                                        "properties": {
                                            "items": {"type": "array"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    fixture = {
        "scope": "case-study",
        "request": {"method": "GET", "url": "https://x/things", "headers": {}, "body": None},
        "response": {
            "status": 200,
            "headers": {},
            "body": {"items": [], "unexpected_field": 1},
            "timing_ms": 1,
        },
        "manifest": {"tool_version": "0.1.0", "captured_at": "2026", "spec_url_at_capture": None, "spec_sha256_at_capture": None},
    }
    s = _write(tmp_path / "spec.json", spec)
    f = _write(tmp_path / "f.json", fixture)
    rc = cmd_quick_diff.run(_args(f, s))
    out = capsys.readouterr().out
    assert rc == 0
    assert "additive" in out
    assert "unexpected_field" in out


def test_subtractive_drift(tmp_path: Path, capsys):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/things": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["mandatory"],
                                        "properties": {
                                            "mandatory": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    fixture = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "https://x/things", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {}, "timing_ms": 1},
        "manifest": {"tool_version": "0.1.0", "captured_at": "", "spec_url_at_capture": None, "spec_sha256_at_capture": None},
    }
    s = _write(tmp_path / "spec.json", spec)
    f = _write(tmp_path / "f.json", fixture)
    rc = cmd_quick_diff.run(_args(f, s, strict=True))
    out = capsys.readouterr().out
    assert "subtractive" in out
    assert "mandatory" in out
    assert rc == 1  # strict mode


def test_type_mismatch(tmp_path: Path, capsys):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/things": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"n": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    fixture = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "https://x/things", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {"n": "not-an-int"}, "timing_ms": 0},
        "manifest": {"tool_version": "0.1.0", "captured_at": "", "spec_url_at_capture": None, "spec_sha256_at_capture": None},
    }
    s = _write(tmp_path / "spec.json", spec)
    f = _write(tmp_path / "f.json", fixture)
    cmd_quick_diff.run(_args(f, s))
    out = capsys.readouterr().out
    assert "type_mismatch" in out


def test_placeholder_detection(tmp_path: Path, capsys):
    spec = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "paths": {}}
    fixture = {
        "scope": "ad-hoc",
        "request": {"method": "GET", "url": "https://x/y", "headers": {}, "body": None},
        "response": {"status": 200, "headers": {}, "body": {"name": "string"}, "timing_ms": 0},
        "manifest": {"tool_version": "", "captured_at": "", "spec_url_at_capture": None, "spec_sha256_at_capture": None},
    }
    s = _write(tmp_path / "spec.json", spec)
    f = _write(tmp_path / "f.json", fixture)
    cmd_quick_diff.run(_args(f, s))
    out = capsys.readouterr().out
    assert "placeholder" in out


def test_link_header_surfaced(fixtures_dir: Path, tmp_path: Path, capsys):
    fixture_path = fixtures_dir / "locations-with-link-header.json"
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/locations": {
                "get": {
                    "responses": {"200": {"description": "OK"}}
                }
            }
        },
    }
    s = _write(tmp_path / "spec.json", spec)
    cmd_quick_diff.run(_args(str(fixture_path), s))
    out = capsys.readouterr().out
    assert "headers" in out
    assert "Link" in out or "link" in out


def test_spec_sha256_mismatch(fixtures_dir: Path, tmp_path: Path, capsys):
    # use a fixture that records a spec_sha256_at_capture
    fixture_path = fixtures_dir / "locations-200.json"
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "different", "version": "9"},
        "paths": {},
    }
    s = _write(tmp_path / "spec.json", spec)
    cmd_quick_diff.run(_args(str(fixture_path), s))
    out = capsys.readouterr().out
    assert "spec_revision" in out


def test_quick_diff_output_has_drift_validation_provenance(
    fixtures_dir: Path, tmp_path: Path, capsys
):
    """H10: quick-diff output must carry a `<!-- probe: ... scope: drift-validation ... -->`
    comment naming the fixture path."""
    fixture_path = fixtures_dir / "locations-200.json"
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {"/locations": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    s = _write(tmp_path / "spec.json", spec)
    cmd_quick_diff.run(_args(str(fixture_path), s))
    out = capsys.readouterr().out
    assert "<!-- probe:" in out
    assert "scope: drift-validation" in out
    assert "fixture:" in out
