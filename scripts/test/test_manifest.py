"""Unit tests for workspace manifest read/write/verify."""

from __future__ import annotations

from pathlib import Path

from skill_from_docs._manifest import (
    file_entry,
    load_manifest,
    now_iso,
    record_run,
    verify_hashes,
    write_manifest,
)


def test_write_and_load(tmp_path: Path):
    ws = tmp_path
    write_manifest(str(ws), {"tool_version": "0.1.0", "runs": []})
    data = load_manifest(str(ws))
    assert data["tool_version"] == "0.1.0"
    assert data["runs"] == []


def test_record_run_appends(tmp_path: Path):
    ws = tmp_path
    record_run(
        str(ws),
        subcommand="fetch",
        args={"source": "x"},
        started_at=now_iso(),
        finished_at=now_iso(),
    )
    record_run(
        str(ws),
        subcommand="probe",
        args={"url": "y"},
        started_at=now_iso(),
        finished_at=now_iso(),
    )
    data = load_manifest(str(ws))
    assert len(data["runs"]) == 2
    assert data["runs"][0]["subcommand"] == "fetch"
    assert data["runs"][1]["subcommand"] == "probe"


def test_hash_verify_detects_modification(tmp_path: Path):
    ws = tmp_path
    target = ws / "raw" / "spec.json"
    target.parent.mkdir()
    target.write_text('{"a": 1}\n')
    record_run(
        str(ws),
        subcommand="fetch",
        args={},
        started_at=now_iso(),
        finished_at=now_iso(),
        outputs=[file_entry(str(ws), str(target))],
    )
    assert verify_hashes(str(ws)) == []
    target.write_text('{"a": 2}\n')
    failures = verify_hashes(str(ws))
    assert failures
    assert "hash mismatch" in failures[0]
