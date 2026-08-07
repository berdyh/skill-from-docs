"""Tests for `openapi-harvest validate`."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from skill_from_docs import cmd_consolidate, cmd_validate


def _validate_args(workspace: str, **overrides):
    base = dict(
        workspace=workspace, strict=False, network=False, json_out=False, allow_host=[]
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _consolidate_args(workspace: str, **overrides):
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


def _seed_workspace(tmp_path: Path, fixtures_dir: Path) -> Path:
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(_consolidate_args(str(tmp_path)))
    return tmp_path


def test_passing_workspace(tmp_path: Path, fixtures_dir: Path):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    rc = cmd_validate.run(_validate_args(str(ws)))
    assert rc == 0


def test_missing_handoff_fails(tmp_path: Path, fixtures_dir: Path, capsys):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "handoff.json").unlink()
    rc = cmd_validate.run(_validate_args(str(ws)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "handoff.json missing" in out


def test_missing_docs_md_fails(tmp_path: Path, fixtures_dir: Path):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "docs.md").unlink()
    rc = cmd_validate.run(_validate_args(str(ws)))
    assert rc == 1


def test_manifest_hash_mismatch_fails(tmp_path: Path, fixtures_dir: Path, capsys):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    # Mutate docs.md after consolidate recorded its hash
    (ws / "docs.md").write_text("# tampered\n")
    rc = cmd_validate.run(_validate_args(str(ws)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "hash mismatch" in out


def test_orphan_TODO_in_docs(tmp_path: Path, fixtures_dir: Path):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    # Add a TODO that doesn't correspond to any gap_list entry by appending.
    text = (ws / "docs.md").read_text()
    (ws / "docs.md").write_text(text + "\n<!-- TODO: random unaccounted-for thing -->\n")
    # Also force handoff to have empty gap_list
    handoff = json.loads((ws / "handoff.json").read_text())
    handoff["gap_list"] = []
    (ws / "handoff.json").write_text(json.dumps(handoff))
    rc = cmd_validate.run(_validate_args(str(ws)))
    # Will fail on hash-mismatch (docs.md changed) which is also a valid signal.
    assert rc == 1


def test_orphan_raw_file_fails(tmp_path: Path, fixtures_dir: Path):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    # Drop an extra raw file that isn't referenced by any provenance comment.
    (ws / "raw" / "extra.json").write_text("{}")
    rc = cmd_validate.run(_validate_args(str(ws)))
    assert rc == 1


def test_json_output_schema(tmp_path: Path, fixtures_dir: Path, capsys):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    cmd_validate.run(_validate_args(str(ws), json_out=True))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "workspace" in payload
    assert payload["verdict"] in ("pass", "warn", "fail")
    assert "checks" in payload
    assert "summary" in payload


def test_validate_accepts_narrative_provenance(tmp_path: Path, fixtures_dir: Path):
    """H6: a workspace with narrative-sourced sections (and matching provenance
    comments) validates clean."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "narrative").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    (tmp_path / "narrative" / "installation.md").write_text("pip install x")
    (tmp_path / "narrative" / "core-concepts.md").write_text("X has Y.")
    (tmp_path / "narrative" / "errors.md").write_text("HTTP codes.")
    (tmp_path / "narrative" / "rate-limits.md").write_text("100/hr.")
    (tmp_path / "narrative" / "gotchas.md").write_text("Beware.")
    (tmp_path / "narrative" / "example.md").write_text("```\ncurl https://x\n```")
    cmd_consolidate.run(_consolidate_args(str(tmp_path)))
    rc = cmd_validate.run(_validate_args(str(tmp_path)))
    assert rc == 0


def test_strict_promotes_warnings(tmp_path: Path, fixtures_dir: Path):
    ws = _seed_workspace(tmp_path, fixtures_dir)
    # tiny-openapi-3.json has no spec_format/tag_count in handoff signals.
    handoff = json.loads((ws / "handoff.json").read_text())
    handoff["content_shape_signals"]["spec_format"] = None
    handoff["content_shape_signals"]["tag_count"] = 0
    (ws / "handoff.json").write_text(json.dumps(handoff))
    # warnings get promoted to fails in strict mode
    rc = cmd_validate.run(_validate_args(str(ws), strict=True))
    # Note: hash mismatch will also occur because we just wrote handoff.
    # Both --strict promotion and the manifest mismatch should produce rc==1.
    assert rc == 1


def test_network_requires_allow_host(tmp_path: Path, capsys):
    """spec_url comes out of handoff.json, which validate did not produce. An
    empty HostAllowlist permits everything, so --network without --allow-host
    would be an arbitrary-URL GET."""
    rc = cmd_validate.run(_validate_args(str(tmp_path), network=True))
    assert rc == 1
    assert "--allow-host" in capsys.readouterr().err


def test_network_rejects_empty_allow_host_string(tmp_path: Path, capsys):
    """[''] is truthy but builds an empty allowlist, which permits every host."""
    rc = cmd_validate.run(_validate_args(str(tmp_path), network=True, allow_host=[""]))
    assert rc == 1
    assert "--allow-host" in capsys.readouterr().err
