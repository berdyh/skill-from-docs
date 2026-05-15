"""Tests for `openapi-harvest fetch`."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
import pytest

from skill_from_docs import cmd_fetch
from skill_from_docs.cmd_fetch import RENDERER_PATTERNS


def _make_args(**overrides):
    base = dict(
        source="https://api.example.com/openapi.json",
        output_spec=None,
        output_source_map=None,
        no_resolve=True,
        user_agent=None,
        timeout=5.0,
        staleness_days=0,
        count_endpoints=False,
        allow_host=["api.example.com"],
        workspace=None,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _transport(routes):
    def h(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return routes.get(url, httpx.Response(404, text=""))

    return httpx.MockTransport(h)


def test_fetch_url_direct(tmp_path: Path, fixtures_dir: Path):
    spec_text = (fixtures_dir / "tiny-openapi-3.json").read_text()
    transport = _transport(
        {
            "https://api.example.com/openapi.json": httpx.Response(
                200,
                content=spec_text.encode(),
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    args = _make_args(workspace=str(tmp_path))
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 0
    out = json.loads((tmp_path / "raw" / "spec.json").read_text())
    assert out["info"]["title"] == "Tiny API"


def test_fetch_local_file(tmp_path: Path, fixtures_dir: Path):
    args = _make_args(
        source=str(fixtures_dir / "tiny-openapi-3.json"),
        workspace=str(tmp_path),
        allow_host=[],
    )
    rc = cmd_fetch.run(args)
    assert rc == 0
    assert (tmp_path / "raw" / "spec.json").exists()
    assert (tmp_path / "raw" / "source-map.json").exists()


@pytest.mark.parametrize(
    "renderer_name,expected_idx",
    [
        ("swagger-ui-shell.html", 4),
        ("redoc-shell.html", 2),
        ("stoplight-shell.html", 1),
        ("scalar-shell.html", 0),
        ("rapidoc-shell.html", 3),
    ],
)
def test_all_5_renderer_regexes(fixtures_dir: Path, renderer_name: str, expected_idx: int):
    html = (fixtures_dir / renderer_name).read_text()
    # _try_renderers iterates in priority order; for each renderer-specific HTML
    # the discovered URL must match.
    discovered = cmd_fetch._try_renderers(html, "https://docs.example.com/")
    assert discovered == "https://api.example.com/openapi.json", f"{renderer_name}: got {discovered}"


def test_renderer_extraction_via_http(tmp_path: Path, fixtures_dir: Path):
    html = (fixtures_dir / "swagger-ui-shell.html").read_text()
    spec_text = (fixtures_dir / "tiny-openapi-3.json").read_text()
    transport = _transport(
        {
            "https://docs.example.com/api/": httpx.Response(
                200, text=html, headers={"Content-Type": "text/html"}
            ),
            "https://api.example.com/openapi.json": httpx.Response(
                200, content=spec_text.encode(), headers={"Content-Type": "application/json"}
            ),
        }
    )
    args = _make_args(
        source="https://docs.example.com/api/",
        workspace=str(tmp_path),
        allow_host=["docs.example.com", "api.example.com"],
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 0
    out = json.loads((tmp_path / "raw" / "spec.json").read_text())
    assert out["info"]["title"] == "Tiny API"


def test_fallback_to_common_paths(tmp_path: Path, fixtures_dir: Path):
    spec_text = (fixtures_dir / "tiny-openapi-3.json").read_text()
    transport = _transport(
        {
            # Direct fetch returns generic HTML that doesn't match any renderer.
            "https://api.example.com/docs": httpx.Response(
                200, text="<html><body>nothing useful</body></html>",
                headers={"Content-Type": "text/html"},
            ),
            "https://api.example.com/openapi.json": httpx.Response(
                200, content=spec_text.encode(),
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    args = _make_args(
        source="https://api.example.com/docs", workspace=str(tmp_path)
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 0


def test_count_endpoints_short_circuits(tmp_path: Path, fixtures_dir: Path, capsys):
    args = _make_args(
        source=str(fixtures_dir / "tiny-openapi-3.json"),
        workspace=str(tmp_path),
        allow_host=[],
        count_endpoints=True,
    )
    rc = cmd_fetch.run(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "2"
    # output files should NOT be written when --count-endpoints
    assert not (tmp_path / "raw" / "spec.json").exists()


def test_all_paths_404_exits_1(tmp_path: Path):
    transport = _transport({})
    args = _make_args(workspace=str(tmp_path))
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 1


def test_host_allowlist_violation(tmp_path: Path):
    transport = _transport({})
    args = _make_args(
        source="https://other.example.com/openapi.json",
        workspace=str(tmp_path),
        allow_host=["api.example.com"],
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 1
