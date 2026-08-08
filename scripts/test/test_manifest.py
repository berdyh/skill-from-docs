"""Unit tests for workspace manifest read/write/verify."""

from __future__ import annotations

import json
from pathlib import Path

from skill_from_docs._manifest import (
    file_entry,
    load_manifest,
    now_iso,
    record_run,
    superseded_mismatches,
    verify_hashes,
    write_manifest,
)


def _record(ws: Path, path: str, subcommand: str = "consolidate") -> None:
    record_run(
        str(ws), subcommand=subcommand, args={}, started_at=now_iso(),
        finished_at=now_iso(), outputs=[file_entry(str(ws), path)],
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


def test_fetch_records_allow_host_per_run(tmp_path: Path):
    """A12: `cmd_fetch` writes the same per-run audit record as `cmd_probe`,
    and only probe's was covered — deleting fetch's left the suite green."""
    import argparse

    import httpx

    from skill_from_docs import cmd_fetch

    spec = json.dumps({"openapi": "3.0.3", "info": {"title": "x", "version": "1"}})
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, content=spec.encode(), headers={"Content-Type": "application/json"}
        )
    )
    args = argparse.Namespace(
        source="https://api.example.com/openapi.json", output_spec=None,
        output_source_map=None, no_resolve=True, user_agent=None, timeout=5.0,
        staleness_days=0, staleness_api_host=None, staleness_api_style=None,
        count_endpoints=False,
        allow_host=["api.example.com", "cdn.example.com"],
        workspace=str(tmp_path), quiet=True,
    )
    assert cmd_fetch.run(args, transport=transport) == 0
    run = json.loads((tmp_path / "manifest.json").read_text())["runs"][-1]
    assert run["subcommand"] == "fetch"
    assert run["args"]["allow_host"] == ["api.example.com", "cdn.example.com"]


def test_auth_records_allow_host_per_run(tmp_path: Path):
    """A12: same record, same gap, in `cmd_auth`."""
    import argparse

    import httpx

    from skill_from_docs import cmd_auth

    args = argparse.Namespace(
        endpoint="https://api.example.com/v1/x", token="tok", output=None,
        short_circuit=True, include_query_auth=False, basic_creds=None,
        basic_creds_env=None, spec=None, bad_token_pattern="bad",
        allow_host=["api.example.com", "cdn.example.com"],
        follow_redirects=False, timeout=5.0, workspace=str(tmp_path), quiet=True,
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    assert cmd_auth.run(args, transport=transport) == 0
    run = json.loads((tmp_path / "manifest.json").read_text())["runs"][-1]
    assert run["subcommand"] == "auth"
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


def test_an_appended_run_hides_an_edit_from_verify_hashes_but_not_from_the_audit(tmp_path: Path):
    """A9: newest-wins means a file can be edited and then re-attested.

    Writing a new digest for a file you just edited satisfies `verify_hashes`
    completely — that is the price of the newest-wins rule, and it is not being
    undone here. What must not also be true is that the manifest stops recording
    the contradiction: the superseded entry is still on disk, and something has
    to be able to see it.
    """
    ws = tmp_path
    target = ws / "docs.md"

    target.write_text("harvested\n")
    _record(ws, "docs.md")
    target.write_text("edited by hand\n")
    _record(ws, "docs.md")  # the attacker's (or the re-run's) fresh attestation

    assert verify_hashes(str(ws)) == []
    findings = superseded_mismatches(str(ws))
    assert [rel for rel, _msg in findings] == ["docs.md"]
    assert "1 earlier run (consolidate)" in findings[0][1]


def test_superseded_mismatches_is_silent_when_the_digest_never_changed(tmp_path: Path):
    """Two runs over unchanged input record the same digest twice. That is the
    single most common shape in a real manifest and must produce nothing."""
    ws = tmp_path
    (ws / "docs.md").write_text("harvested\n")
    _record(ws, "docs.md")
    _record(ws, "docs.md")

    assert superseded_mismatches(str(ws)) == []


def test_superseded_mismatches_defers_to_verify_hashes_on_a_live_mismatch(tmp_path: Path):
    """When the *newest* digest is the one that mismatches, `verify_hashes`
    already fails the workspace. Reporting the older entries too would print an
    advisory alongside the error that supersedes it."""
    ws = tmp_path
    target = ws / "docs.md"

    target.write_text("first\n")
    _record(ws, "docs.md")
    target.write_text("second\n")
    _record(ws, "docs.md")
    target.write_text("tampered, and not re-attested\n")

    assert [f for f in verify_hashes(str(ws)) if "hash mismatch" in f]
    assert superseded_mismatches(str(ws)) == []


def test_superseded_mismatches_ignores_a_path_that_is_gone(tmp_path: Path):
    """A deleted file is `verify_hashes`'s "missing:" failure, not an advisory."""
    ws = tmp_path
    target = ws / "docs.md"

    target.write_text("first\n")
    _record(ws, "docs.md")
    target.write_text("second\n")
    _record(ws, "docs.md")
    target.unlink()

    assert [f for f in verify_hashes(str(ws)) if f.startswith("missing:")]
    assert superseded_mismatches(str(ws)) == []


def test_file_entry_handles_a_relative_workspace(tmp_path: Path, monkeypatch):
    """`consolidate myws` passes a relative workspace, so docs_path is relative
    too. Joining the workspace onto it again looked for `myws/myws/docs.md` and
    crashed after docs.md had already been written."""
    ws = tmp_path / "myws"
    ws.mkdir()
    (ws / "docs.md").write_text("hi\n")
    monkeypatch.chdir(tmp_path)

    for workspace, path in (
        ("myws", "myws/docs.md"),   # both cwd-relative, as consolidate passes them
        ("myws", "docs.md"),        # workspace-relative
        (str(ws), str(ws / "docs.md")),  # both absolute
    ):
        assert file_entry(workspace, path)["path"] == "docs.md"
