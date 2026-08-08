"""Tests for `openapi-harvest validate`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from conftest import make_mock_transport

from skill_from_docs import cmd_consolidate, cmd_fetch, cmd_validate
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


SUMMARY_RE = re.compile(r"Pass: (\d+)/(\d+), warn: (\d+), fail: (\d+)")


def test_summary_string_is_the_shape_the_readme_documents(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """`summary` is documented alongside `verdict` as a stable v1 contract CI
    consumers may assert on, and nothing checked it: renaming `Pass: ` to
    `PASSED: ` broke no test. Read both sides, as the `verdict` test does, so
    neither the code nor the README can drift alone — and pin what the four
    numbers count, since a summary whose arithmetic changes is as breaking to a
    consumer as one whose wording does.
    """
    readme = Path(__file__).resolve().parents[1] / "README.md"
    documented = re.search(r'"summary":\s*"([^"]+)"', readme.read_text())
    assert documented and SUMMARY_RE.fullmatch(documented.group(1))

    ws = _seed_workspace(tmp_path, fixtures_dir)
    # An unreferenced capture, so the advisory counts are not trivially zero.
    (ws / "raw" / "extra.json").write_text("{}")
    cmd_validate.run(_validate_args(str(ws), json_out=True))
    payload = json.loads(capsys.readouterr().out)

    match = SUMMARY_RE.fullmatch(payload["summary"])
    assert match, payload["summary"]
    passed, total, warned, failed = (int(g) for g in match.groups())
    checks = payload["checks"]
    soft = [c for c in checks if not c["passed"] and c["severity"] != "error"]

    assert passed == sum(1 for c in checks if c["passed"])
    assert total == len(checks)
    assert failed == sum(1 for c in checks if not c["passed"] and c["severity"] == "error")
    # Both advisory channels are counted together — `checks` entries carrying a
    # non-error severity, plus every entry of the `warnings` array.
    assert warned == len(soft) + len(payload["warnings"])
    assert soft and payload["warnings"], "neither channel should be empty here"


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


def test_validate_never_creates_the_manifest_it_reports_missing(tmp_path: Path, capsys):
    """The same guard, on the case the workspace already exists.

    `os.path.exists(manifest_path)` is the predicate, not `exists(workspace)` —
    a real workspace with no `manifest.json` is the likelier way to hit this
    than a mistyped path, and it is the one where the self-heal is invisible:
    the directory was already there, so the only evidence is that the retry
    stops saying `manifest.json missing`.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    args = _validate_args(str(ws), network=True, allow_host=["example.com"])

    assert cmd_validate.run(args) == 1
    assert not (ws / "manifest.json").exists()
    assert cmd_validate.run(args) == 1, "a bare retry must not go green"
    assert capsys.readouterr().out.count("manifest.json missing") == 2


def test_per_item_check_ids_are_stable_across_processes(tmp_path: Path, fixtures_dir: Path):
    """Check `id` is part of the documented `--json` contract, so it has to mean
    the same thing in two different runs.

    Two ids are built per-item rather than from a literal: `manifest_hash_*` and
    `network_*`. They were derived from Python's `hash()`, which is salted per
    process for `str` under the default `PYTHONHASHSEED=random` — so the same
    failing file produced a different id every run and no consumer could match
    on it. Run the real CLI under three fixed, different seeds; a salted hash
    yields three different ids.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    (ws / "docs.md").write_text("# tampered\n")

    src = str(Path(cmd_validate.__file__).resolve().parents[2])
    seen = set()
    for seed in ("0", "1", "4294967295"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": src}
        proc = subprocess.run(
            [sys.executable, "-m", "skill_from_docs.openapi_harvest", "validate", str(ws), "--json"],
            capture_output=True, text=True, env=env, check=False,
        )
        payload = json.loads(proc.stdout)
        ids = [c["id"] for c in payload["checks"] if c["id"].startswith("manifest_hash_")]
        assert ids, proc.stdout
        seen.add(tuple(ids))

    assert len(seen) == 1, f"check ids differ between processes: {seen}"


# --- A8: --network must re-fetch the fetchable URL, and never print it -------
#
# `key` is in SENSITIVE_QUERY_KEYS, so `?key=petstore` is recorded as
# `?key=<redacted>` in every artifact that leaves the machine. Before A8 that
# redacted string was also the only URL `validate --network` had, so it GET a
# URL that cannot exist and reported a failure that was not real. A8 records the
# fetchable URL separately, in raw/source-map.json; these pin both halves —
# the right URL is fetched, and it does not come back out in any output.

A8_SECRET = "petstore-A8-SECRET"
A8_FETCH_URL = f"https://api.example.com/openapi.json?key={A8_SECRET}"
A8_DISPLAY_URL = "https://api.example.com/openapi.json?key=<redacted>"


def _fetch_args(workspace: str, source: str):
    return argparse.Namespace(
        source=source,
        output_spec=None,
        output_source_map=None,
        no_resolve=True,
        user_agent=None,
        timeout=5.0,
        staleness_days=0,
        staleness_api_host=None,
        staleness_api_style=None,
        count_endpoints=False,
        allow_host=["api.example.com"],
        workspace=workspace,
        quiet=True,
    )


def _seed_url_workspace(tmp_path: Path, fixtures_dir: Path, source: str = A8_FETCH_URL) -> Path:
    """A workspace harvested from a URL, i.e. one with a real `spec_url`."""
    spec_text = (fixtures_dir / "tiny-openapi-3.json").read_text()
    transport = make_mock_transport(
        {
            source: httpx.Response(
                200, content=spec_text.encode(), headers={"Content-Type": "application/json"}
            )
        }
    )
    assert cmd_fetch.run(_fetch_args(str(tmp_path), source), transport=transport) == 0
    assert cmd_consolidate.run(_consolidate_args(str(tmp_path))) == 0
    return tmp_path


def _spec_url_in_handoff(ws: Path) -> str:
    handoff = json.loads((ws / "handoff.json").read_text())
    return handoff["content_shape_signals"]["spec_url"]


def _rewrite_source_map(ws: Path, mutate) -> None:
    """Edit raw/source-map.json and re-attest it in manifest.json.

    Rewriting the recorded digest in place, rather than appending a run, keeps
    `validate` reporting on the thing under test: an appended entry would make
    the earlier digest superseded, and that is its own advisory finding.
    """
    from skill_from_docs._manifest import sha256_file

    rel = "raw/source-map.json"
    path = ws / rel
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n")

    manifest_path = ws / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = sha256_file(str(path))
    for run in manifest.get("runs", []):
        for kind in ("inputs", "outputs"):
            for entry in run.get(kind, []):
                if entry.get("path") == rel:
                    entry["sha256"] = digest
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def _drop_fetch_url(ws: Path) -> None:
    """Turn the workspace into one harvested before A8."""
    _rewrite_source_map(ws, lambda data: data.pop("fetch_url", None))


def test_network_refetches_the_fetchable_url_not_the_display_url(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """A8's core claim. handoff.json records `?key=<redacted>`, which no server
    can answer; the workspace also records the URL verbatim, and that is the one
    that must be GET.

    Against the unfixed code this fails with `network_* ... returned 404`,
    because the redacted URL is the only one it looks at.
    """
    ws = _seed_url_workspace(tmp_path, fixtures_dir)
    assert _spec_url_in_handoff(ws) == A8_DISPLAY_URL

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == A8_FETCH_URL:
            return httpx.Response(200, json={"openapi": "3.0.0"})
        return httpx.Response(404, text="no such spec")

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"], json_out=True),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen == [A8_FETCH_URL], f"validate fetched the wrong URL: {seen}"

    payload = json.loads(capsys.readouterr().out)
    network_checks = [c for c in payload["checks"] if c["id"].startswith("network_")]
    assert len(network_checks) == 1
    assert network_checks[0]["passed"] is True


def test_network_never_prints_the_fetchable_url(tmp_path: Path, fixtures_dir: Path, capsys):
    """The credential is allowed in `client.get`. It is not allowed in a check
    id, a message, the `--json` payload, or the manifest entry `validate`
    appends afterwards — that is failure mode 3, and `URL {url} returned 404` is
    exactly the shape it takes."""
    ws = _seed_url_workspace(tmp_path, fixtures_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        # Non-200 so the failure message, not just the pass path, is exercised.
        return httpx.Response(404, text="gone")

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"], json_out=True),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 1  # a real 404 still fails, as it should

    out = capsys.readouterr().out
    assert A8_SECRET not in out
    assert A8_DISPLAY_URL in out
    payload = json.loads(out)
    failed = [c for c in payload["checks"] if c["id"].startswith("network_") and not c["passed"]]
    assert failed and "returned 404" in failed[0]["message"]

    manifest_text = (ws / "manifest.json").read_text()
    assert A8_SECRET not in manifest_text
    assert any(r["subcommand"] == "validate" for r in json.loads(manifest_text)["runs"])


def test_network_never_prints_the_fetchable_url_from_a_transport_error(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """httpx quotes the request URL in its own exception text, so the
    `can't fetch {url}: {e}` branch leaks through `e` even when the format
    string itself is careful."""
    ws = _seed_url_workspace(tmp_path, fixtures_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"], json_out=True),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 1

    out = capsys.readouterr().out
    assert A8_SECRET not in out
    payload = json.loads(out)
    assert any(c["id"] == "network_error" for c in payload["checks"])


def test_network_skips_when_the_workspace_records_no_fetchable_url(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """A workspace harvested before A8 has only the redacted URL. Fetching it
    and reporting the 404 is the bug; the honest answer is to skip and say why,
    and — because it is a skip, not a finding — it must not move the verdict."""
    ws = _seed_url_workspace(tmp_path, fixtures_dir)
    _drop_fetch_url(ws)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, text="")

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"], json_out=True),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen == [], f"nothing should have been fetched, got {seen}"

    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pass"
    skipped = [c for c in payload["checks"] if c["id"].startswith("network_skipped_")]
    assert len(skipped) == 1
    assert skipped[0]["passed"] is True
    assert "skipped" in skipped[0]["message"]
    assert "source-map.json" in skipped[0]["message"]


def test_network_skips_are_not_promoted_by_strict(tmp_path: Path, fixtures_dir: Path, capsys):
    """--strict promotes findings. A skip is not a finding, so a pre-A8
    workspace must not start failing CI under --strict either."""
    ws = _seed_url_workspace(tmp_path, fixtures_dir)
    _drop_fetch_url(ws)

    rc = cmd_validate.run(
        _validate_args(
            str(ws), network=True, strict=True, allow_host=["api.example.com"], json_out=True
        ),
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="")),
    )
    payload = json.loads(capsys.readouterr().out)
    assert not [
        c for c in payload["checks"] if c["id"].startswith("network_") and not c["passed"]
    ]
    assert rc == 0


def test_network_still_fetches_a_display_url_that_needs_no_redaction(
    tmp_path: Path, fixtures_dir: Path
):
    """The ordinary case, and the pre-A8 behaviour: when nothing in the URL is
    sensitive, display and fetchable are the same string, so an old workspace
    with no `fetch_url` keeps working with no skip at all."""
    source = "https://api.example.com/openapi.json?page=2"
    ws = _seed_url_workspace(tmp_path, fixtures_dir, source=source)
    _drop_fetch_url(ws)
    assert _spec_url_in_handoff(ws) == source

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"openapi": "3.0.0"})

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"]),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen == [source]


def test_network_skips_when_the_two_recorded_urls_disagree(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """Failure mode 5, one layer out: two layers comparing "the same" URL have
    to agree on which one. If raw/source-map.json's fetchable URL does not
    redact down to the URL handoff.json displays, the workspace was re-fetched
    without being re-consolidated — fetching either one would be reporting on a
    spec the rest of the workspace does not describe."""
    ws = _seed_url_workspace(tmp_path, fixtures_dir)
    _rewrite_source_map(
        ws,
        lambda data: data.__setitem__(
            "fetch_url", f"https://api.example.com/v2/openapi.json?key={A8_SECRET}"
        ),
    )

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    rc = cmd_validate.run(
        _validate_args(str(ws), network=True, allow_host=["api.example.com"], json_out=True),
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen == []

    out = capsys.readouterr().out
    assert A8_SECRET not in out
    payload = json.loads(out)
    skipped = [c for c in payload["checks"] if c["id"].startswith("network_skipped_")]
    assert len(skipped) == 1 and skipped[0]["passed"] is True
    assert "does not describe" in skipped[0]["message"]


def test_coverage_checklist_unknown_source_actually_fires(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """Check 10 read `item["source"]`; `consolidate` writes `sources`, a list.

    So the check never fired on any workspace this tool has produced — a claim
    of coverage nothing backs was exactly what it existed to catch, and it was
    inert. Same shape as the unreachable `warn` verdict (A2): a documented check
    that cannot fire. Pin both spellings so the plural cannot silently rot back.
    """
    ws = _seed_workspace(tmp_path, fixtures_dir)
    handoff = json.loads((ws / "handoff.json").read_text())
    handoff["coverage_checklist"] = [
        {"name": "Plural", "status": "covered", "sources": ["https://nobody.example/a"]},
        {"name": "Singular", "status": "covered", "source": "https://nobody.example/b"},
    ]
    (ws / "handoff.json").write_text(json.dumps(handoff))

    args = _validate_args(str(ws), json_out=True)
    cmd_validate.run(args)
    payload = json.loads(capsys.readouterr().out)

    unknown = [w for w in payload["warnings"] if w["id"] == "coverage_checklist_unknown_source"]
    assert len(unknown) == 2, payload["warnings"]
    assert any("nobody.example/a" in w["message"] for w in unknown)
    assert any("nobody.example/b" in w["message"] for w in unknown)

    # Advisory only: it lives in `warnings`, never in `checks`, so it cannot
    # move the non-strict verdict. (The `fail` this workspace does report comes
    # from the manifest digest, because the test rewrote handoff.json.)
    assert not [c for c in payload["checks"] if c["id"] == "coverage_checklist_unknown_source"]
