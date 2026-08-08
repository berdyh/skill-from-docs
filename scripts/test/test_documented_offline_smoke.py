"""The offline smoke sequence that scripts/README.md and probing-tools.md document.

Those docs claim "CI exercises this sequence on every PR". It did not — CI ran
pytest, and nothing in pytest ran the documented commands. This file makes the
claim true, so the sequence cannot rot silently.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skill_from_docs import cmd_consolidate, cmd_validate


def _consolidate_args(workspace: str, **overrides):
    base = dict(
        workspace=workspace,
        merge_probes=True,
        tag=[],
        narrative_dir=None,
        emit_handoff=True,
        sanitize=True,
        dry_run=False,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _validate_args(workspace: str, **overrides):
    base = dict(
        workspace=workspace, strict=False, network=False, json_out=False, allow_host=[]
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_documented_offline_smoke_passes(hcloud_workspace: Path):
    """`consolidate --merge-probes` then `validate` on a seeded workspace →
    verdict pass, exit 0. This is the documented two-command sequence."""
    assert cmd_consolidate.run(_consolidate_args(str(hcloud_workspace))) == 0
    assert (hcloud_workspace / "docs.md").exists()
    assert (hcloud_workspace / "handoff.json").exists()
    assert cmd_validate.run(_validate_args(str(hcloud_workspace))) == 0


def test_documented_offline_smoke_json_verdict(hcloud_workspace: Path, capsys):
    """`validate --json` is a documented stable contract; CI consumers assert
    on `verdict` and `summary`."""
    cmd_consolidate.run(_consolidate_args(str(hcloud_workspace)))
    cmd_validate.run(_validate_args(str(hcloud_workspace), json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pass"
    assert set(payload) >= {"workspace", "verdict", "checks", "warnings", "summary", "checked_at"}


def test_seeded_workspace_produces_archetype4_signals(hcloud_workspace: Path):
    """The harvest this fixture stands in for is archetype 4, so handoff.json
    must carry the OpenAPI content-shape signals skill-creator branches on."""
    cmd_consolidate.run(_consolidate_args(str(hcloud_workspace)))
    handoff = json.loads((hcloud_workspace / "handoff.json").read_text())
    assert handoff["archetype_primary"] == 4
    signals = handoff["content_shape_signals"]
    assert signals["has_openapi_spec"] is True
    assert signals["endpoint_count"] >= 1
