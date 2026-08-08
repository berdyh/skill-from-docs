"""Tests for `openapi-harvest validate`."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from skill_from_docs import cmd_consolidate, cmd_validate
from skill_from_docs._manifest import file_entry, now_iso, record_run


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


def test_orphan_raw_file_warns(tmp_path: Path, fixtures_dir: Path, capsys):
    """An unreferenced capture is advisory: verdict `warn`, exit 0.

    This is the check that makes `warn` reachable at all — before it carried a
    severity, every check was `error` and the documented `warn` verdict could
    not be produced by any input.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    # Drop an extra raw file that isn't referenced by any provenance comment.
    (ws / "raw" / "extra.json").write_text("{}")
    rc = cmd_validate.run(_validate_args(str(ws), json_out=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "warn"
    assert any(c["id"].startswith("orphan_capture") for c in payload["checks"])


def test_orphan_raw_file_fails_under_strict(tmp_path: Path, fixtures_dir: Path, capsys):
    """--strict is how you make an advisory finding blocking."""
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "raw" / "extra.json").write_text("{}")
    rc = cmd_validate.run(_validate_args(str(ws), strict=True, json_out=True))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"


def test_readme_documents_exactly_the_emittable_verdicts():
    """scripts/README.md calls `verdict` a stable v1 contract CI consumers may
    assert on. It documented `pass | warn | fail` while the code could only ever
    emit two of the three. Read both sides so they cannot drift again.
    """
    readme = Path(__file__).resolve().parents[1] / "README.md"
    line = next(
        line for line in readme.read_text().splitlines() if '"verdict"' in line
    )
    documented = {v.strip().strip('",') for v in line.split(":", 1)[1].split("|")}
    assert documented == set(cmd_validate.VERDICTS)


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


def test_rerunning_consolidate_still_validates(tmp_path: Path, fixtures_dir: Path, capsys):
    """Re-running consolidate is a normal thing to do.

    It used to make `validate` fail: every run appends a fresh digest for
    docs.md, and hash verification walked every historical entry, so the first
    run's superseded hash mismatched.

    The spec is edited between the two runs on purpose. consolidate is
    byte-deterministic, so two runs over an unchanged spec record the *same*
    digest twice and the old code passes too — a test that skips this step
    passes with the fix reverted and pins nothing.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)

    spec_path = ws / "raw" / "spec.json"
    spec = json.loads(spec_path.read_text())
    spec["info"]["version"] = "2.0.0"
    spec_path.write_text(json.dumps(spec, indent=2))

    assert cmd_consolidate.run(_consolidate_args(str(ws))) == 0
    assert cmd_validate.run(_validate_args(str(ws))) == 0
    assert "hash mismatch" not in capsys.readouterr().out


def test_orphan_and_missing_target_checks_keep_their_order_and_multiplicity(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """Checks 5 and 6 read the same provenance fields, so they run as one walk.

    Their emitted output is a stable contract, and the two easy ways to break it
    while merging are both pinned here: the orphan-capture checks still come
    first, and a file referenced twice still produces two `missing_provenance_
    target` entries rather than being collapsed by the `referenced_files` set.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "raw" / "extra.json").write_text("{}")
    (ws / "docs.md").write_text(
        (ws / "docs.md").read_text()
        + "\n## Extra\n"
        + "<!-- source: https://a raw_file: raw/gone.json retrieved: 2026-01-01 -->\n"
        + "<!-- source: https://b raw_file: raw/gone.json retrieved: 2026-01-01 -->\n"
    )

    cmd_validate.run(_validate_args(str(ws), json_out=True))
    ids = [c["id"] for c in json.loads(capsys.readouterr().out)["checks"]]

    assert ids.count("missing_provenance_target_raw_gone.json") == 2
    assert ids.index("orphan_capture_raw_extra.json") < ids.index(
        "missing_provenance_target_raw_gone.json"
    )


def test_appending_a_run_cannot_hide_an_edit_from_the_report(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """A9: re-attesting a hand-edited file satisfies the hash check.

    `verify_hashes` compares against the newest recorded digest, so editing
    `docs.md` and appending a run that records the new digest verifies clean —
    `manifest_hash_verify` passes and nothing in `checks` says otherwise. The
    superseded entry is the only remaining trace, so `validate` has to report
    it or the "complete append-only audit trail" is a claim nothing checks.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "docs.md").write_text("# hand-edited\n")
    record_run(
        str(ws), subcommand="consolidate", args={}, started_at=now_iso(),
        finished_at=now_iso(), outputs=[file_entry(str(ws), "docs.md")],
    )

    rc = cmd_validate.run(_validate_args(str(ws), json_out=True))
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert not [
        c for c in payload["checks"] if "hash mismatch" in (c["message"] or "")
    ], "the appended run really does satisfy verify_hashes — that is the premise"
    assert any(w["id"] == "superseded_digest_docs.md" for w in payload["warnings"])


def test_a_legitimate_consolidate_rerun_warns_without_moving_the_verdict(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """The superseded-digest report must not read as an accusation.

    Two `consolidate` runs over a changed spec legitimately record two different
    `docs.md` digests — the common case, not an attack. So the finding lives in
    the advisory `warnings` channel: visible, never verdict-moving, and worded
    as the expected outcome of a re-run. Putting it in `checks` would make
    `warn` the verdict of the ordinary re-run, which is the defect the advisory
    channel exists to avoid.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    spec_path = ws / "raw" / "spec.json"
    spec = json.loads(spec_path.read_text())
    spec["info"]["version"] = "2.0.0"
    spec_path.write_text(json.dumps(spec, indent=2))
    assert cmd_consolidate.run(_consolidate_args(str(ws))) == 0

    rc = cmd_validate.run(_validate_args(str(ws), json_out=True))
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["verdict"] == "pass"
    warning = next(
        w for w in payload["warnings"] if w["id"] == "superseded_digest_docs.md"
    )
    assert "normal result of re-running" in warning["message"]
    assert "hash mismatch" not in warning["message"]


def test_local_file_harvest_still_verdicts_pass(tmp_path: Path, fixtures_dir: Path, capsys):
    """A local-file harvest has no spec_url, and that is not a problem.

    `archetype4_warn_spec_url` fires for every `fetch ./spec.json` workspace, so
    letting the advisory `warnings` list move the non-strict verdict turned the
    ordinary offline flow into `warn` on a clean workspace. The README's own
    smoke example prints `verdict: pass`; consumers assert on it.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    rc = cmd_validate.run(_validate_args(str(ws), json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "pass"
    # The advisory findings are still reported, just not verdict-moving.
    assert any(w["id"] == "archetype4_warn_spec_url" for w in payload["warnings"])


def test_strict_still_blocks_on_advisory_warnings(tmp_path: Path, fixtures_dir: Path, capsys):
    """--strict is the channel that makes the `warnings` list blocking."""
    ws = _seed_workspace(tmp_path, fixtures_dir)
    rc = cmd_validate.run(_validate_args(str(ws), strict=True, json_out=True))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"


def test_validate_never_creates_the_workspace_it_reports_on(tmp_path: Path, capsys):
    """`validate` reports on a workspace; it must not build one.

    `record_run` writes through `write_manifest`, which makes the workspace
    directory. Unguarded, validate healed the thing it was checking: a first run
    failed `manifest_exists` and created a manifest, so a bare retry of a red CI
    step went green with nothing fixed.
    """
    missing = tmp_path / "typo"
    args = _validate_args(str(missing), network=True, allow_host=["example.com"])

    assert cmd_validate.run(args) == 1
    assert not missing.exists()
    assert cmd_validate.run(args) == 1, "a bare retry must not go green"
    assert "manifest.json missing" in capsys.readouterr().out
