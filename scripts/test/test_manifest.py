"""Unit tests for workspace manifest read/write/verify."""

from __future__ import annotations

import json
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


def test_probe_records_allow_host_per_run(tmp_path):
    """The manifest is an audit trail: which run was permitted to reach which
    host. It is deliberately NOT read back as an allowlist input — see
    references/probing-tools.md."""
    import argparse

    import httpx

    from skill_from_docs import cmd_probe

    args = argparse.Namespace(
        url="https://api.example.com/v1/x", method="GET", header=[], data=None,
        output=None, scope="ad-hoc", no_redact=False, redact_body_key=[],
        redact_body_pattern=[], allow_host=["api.example.com", "cdn.example.com"],
        max_retries=3, follow_redirects=False, dry_run=False, timeout=2.0,
        workspace=str(tmp_path), quiet=True,
    )
    rc = cmd_probe.run(args, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert rc == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    run = manifest["runs"][-1]
    assert run["subcommand"] == "probe"
    assert run["args"]["allow_host"] == ["api.example.com", "cdn.example.com"]


def test_allowed_hosts_is_never_read_back_as_an_allowlist(tmp_path):
    """Regression guard for a documented-but-refused feature: a manifest that
    names a host must not grant `probe` permission to reach it."""
    import argparse

    import httpx

    from skill_from_docs import cmd_probe

    (tmp_path / "manifest.json").write_text(json.dumps({
        "runs": [{"subcommand": "fetch", "args": {"allow_host": ["evil.example.net"]}}],
        "allowed_hosts": ["evil.example.net"],
    }))
    args = argparse.Namespace(
        url="https://evil.example.net/x", method="GET", header=[], data=None,
        output=None, scope="ad-hoc", no_redact=False, redact_body_key=[],
        redact_body_pattern=[], allow_host=["api.example.com"],
        max_retries=3, follow_redirects=False, dry_run=False, timeout=2.0,
        workspace=str(tmp_path), quiet=True,
    )
    rc = cmd_probe.run(args, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert rc == 1  # blocked by --allow-host, not widened by the manifest


def test_verify_hashes_uses_the_newest_entry_per_path(tmp_path: Path):
    """Re-running a subcommand must not make `validate` fail.

    `record_run` appends, so a second run leaves the first run's now-superseded
    digest for the same path in the manifest. Verifying every historical entry
    reported `hash mismatch` for a workspace whose only sin was being
    regenerated — once per superseded run.
    """
    ws = tmp_path
    target = ws / "docs.md"

    target.write_text("first\n")
    record_run(
        str(ws), subcommand="consolidate", args={}, started_at=now_iso(),
        finished_at=now_iso(), outputs=[file_entry(str(ws), "docs.md")],
    )
    target.write_text("second\n")
    record_run(
        str(ws), subcommand="consolidate", args={}, started_at=now_iso(),
        finished_at=now_iso(), outputs=[file_entry(str(ws), "docs.md")],
    )

    assert verify_hashes(str(ws)) == []
    # Both runs are still on record — this is about which one describes disk,
    # not about pruning the audit trail.
    assert len(load_manifest(str(ws))["runs"]) == 2

    target.write_text("tampered\n")
    assert [f for f in verify_hashes(str(ws)) if "docs.md" in f]
