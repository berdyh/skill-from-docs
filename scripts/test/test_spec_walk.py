"""Every subcommand must agree on what counts as an operation.

`fetch` counted `trace` and `consolidate` did not, so one workspace reported two
different endpoint counts and the TRACE operation appeared in
`raw/source-map.json` with no matching section in `docs.md`. These tests pin the
agreement rather than any one command's answer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skill_from_docs import cmd_consolidate, cmd_fetch
from skill_from_docs._spec import count_operations, iter_operations, json_pointer


def _fetch_args(**overrides):
    base = dict(
        source=None,
        output_spec=None,
        output_source_map=None,
        no_resolve=True,
        user_agent=None,
        timeout=5.0,
        staleness_days=0,
        staleness_api_host=None,
        staleness_api_style=None,
        count_endpoints=False,
        allow_host=[],
        workspace=None,
        quiet=True,
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


def test_iter_operations_skips_non_operation_keys(fixtures_dir: Path):
    """A path item legally carries `summary` and `parameters` beside methods."""
    spec = json.loads((fixtures_dir / "trace-openapi.json").read_text())
    ops = list(iter_operations(spec))
    assert [(p, m) for p, m, _ in ops] == [("/echo", "get"), ("/echo", "trace")]


def test_iter_operations_tolerates_junk():
    assert list(iter_operations(None)) == []
    assert list(iter_operations({})) == []
    assert list(iter_operations({"paths": "not a dict"})) == []
    assert list(iter_operations({"paths": {"/x": "not a dict"}})) == []
    assert list(iter_operations({"paths": {"/x": {"get": "not a dict"}}})) == []


def test_json_pointer_escapes_tilde_before_slash():
    # `~1` must survive as `~01`, not be re-escaped into `~001`.
    assert json_pointer("/a~1b", "GET") == "/paths/~1a~01b/get"


def test_trace_op_counted_identically_by_fetch_and_consolidate(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """One workspace, one endpoint count. Both numbers include the TRACE op."""
    spec_file = str(fixtures_dir / "trace-openapi.json")

    cmd_fetch.run(_fetch_args(source=spec_file, workspace=str(tmp_path), count_endpoints=True))
    counted_by_fetch = int(capsys.readouterr().out.strip())

    assert cmd_fetch.run(_fetch_args(source=spec_file, workspace=str(tmp_path))) == 0
    assert cmd_consolidate.run(_consolidate_args(str(tmp_path))) == 0
    handoff = json.loads((tmp_path / "handoff.json").read_text())

    assert counted_by_fetch == 2
    assert handoff["content_shape_signals"]["endpoint_count"] == counted_by_fetch


def test_trace_op_reaches_docs_and_source_map(tmp_path: Path, fixtures_dir: Path):
    """The op the source map records must be the op docs.md documents."""
    spec_file = str(fixtures_dir / "trace-openapi.json")
    cmd_fetch.run(_fetch_args(source=spec_file, workspace=str(tmp_path)))
    cmd_consolidate.run(_consolidate_args(str(tmp_path)))

    source_map = json.loads((tmp_path / "raw" / "source-map.json").read_text())
    assert "/echo:trace" in source_map["operations"]
    assert "`TRACE /echo`" in (tmp_path / "docs.md").read_text()


def test_count_operations_matches_iter_operations(fixtures_dir: Path):
    for name in ("tiny-openapi-3.json", "tiny-openapi-3.1.json", "trace-openapi.json"):
        spec = json.loads((fixtures_dir / name).read_text())
        assert count_operations(spec) == len(list(iter_operations(spec)))


def test_iter_operations_skips_non_string_path_keys():
    """YAML does not require mapping keys to be strings, so a remote spec can
    hand us `paths: {1: {...}}`. json_pointer would AttributeError on it and
    replace the numeric exit-code contract with a traceback."""
    assert list(iter_operations({"paths": {1: {"get": {}}}})) == []
    assert [p for p, _m, _o in iter_operations({"paths": {1: {"get": {}}, "/ok": {"get": {}}}})] == [
        "/ok"
    ]
