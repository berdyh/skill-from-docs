"""Tests for the handoff.json contract linter."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from skill_from_docs import cmd_consolidate
from skill_from_docs._schema import (
    HANDOFF_REQUIRED_KEYS,
    HARVEST_METADATA_KEYS,
    lint_handoff,
)


def _emitted_handoff(tmp_path: Path, fixtures_dir: Path) -> dict:
    (tmp_path / "raw").mkdir()
    shutil.copy(fixtures_dir / "tiny-openapi-3.json", tmp_path / "raw" / "spec.json")
    cmd_consolidate.run(
        argparse.Namespace(
            workspace=str(tmp_path),
            merge_probes=False,
            tag=[],
            narrative_dir=None,
            emit_handoff=True,
            sanitize=True,
            dry_run=False,
            quiet=True,
        )
    )
    return json.loads((tmp_path / "handoff.json").read_text())


def test_real_emitted_handoff_is_clean(tmp_path: Path, fixtures_dir: Path):
    """The contract has to describe what consolidate actually emits — otherwise
    it is a second source of truth that drifts."""
    assert lint_handoff(_emitted_handoff(tmp_path, fixtures_dir)) == []


def test_every_required_key_is_reported_when_dropped(tmp_path: Path, fixtures_dir: Path):
    handoff = _emitted_handoff(tmp_path, fixtures_dir)
    for key in HANDOFF_REQUIRED_KEYS:
        partial = {k: v for k, v in handoff.items() if k != key}
        assert any(key in p for p in lint_handoff(partial)), key


def test_wrong_types_are_reported(tmp_path: Path, fixtures_dir: Path):
    handoff = _emitted_handoff(tmp_path, fixtures_dir)
    handoff["provenance_index"] = ["not", "an", "object"]
    handoff["gap_list"] = {}
    problems = lint_handoff(handoff)
    assert any("provenance_index must be object" in p for p in problems)
    assert any("gap_list must be array" in p for p in problems)


def test_unsupported_version_is_reported(tmp_path: Path, fixtures_dir: Path):
    handoff = _emitted_handoff(tmp_path, fixtures_dir)
    handoff["version"] = 99
    assert any("unsupported handoff version 99" in p for p in lint_handoff(handoff))


def test_harvest_metadata_subkeys_checked(tmp_path: Path, fixtures_dir: Path):
    handoff = _emitted_handoff(tmp_path, fixtures_dir)
    for key in HARVEST_METADATA_KEYS:
        meta = {k: v for k, v in handoff["harvest_metadata"].items() if k != key}
        problems = lint_handoff({**handoff, "harvest_metadata": meta})
        assert any(f"harvest_metadata missing key: {key}" == p for p in problems), key


def test_non_object_rejected():
    assert lint_handoff([1, 2, 3]) == [
        "handoff.json must be a JSON object, got list"
    ]
