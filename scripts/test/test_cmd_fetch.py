"""Tests for `openapi-harvest fetch`."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
import pytest

from skill_from_docs import cmd_fetch


def _make_args(**overrides):
    base = dict(
        source="https://api.example.com/openapi.json",
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


def test_offallowlist_renderer_url_reports_allowlist_error(
    tmp_path: Path, fixtures_dir: Path, capsys
):
    """The renderer points at a host the user didn't allow. That should exit 1
    naming the allowlist, not fall through to common-path probing and die with
    a misleading 'could not discover an OpenAPI spec'."""
    html = (fixtures_dir / "swagger-ui-shell.html").read_text()
    transport = _transport(
        {
            "https://docs.example.com/api/": httpx.Response(
                200, text=html, headers={"Content-Type": "text/html"}
            ),
        }
    )
    args = _make_args(
        source="https://docs.example.com/api/",
        workspace=str(tmp_path),
        allow_host=["docs.example.com"],  # api.example.com deliberately absent
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 1
    err = capsys.readouterr().err
    assert "api.example.com" in err
    assert "could not discover" not in err


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


def test_fetch_url_missing_allow_host_exits_1(tmp_path: Path, capsys):
    """B2: fetching a remote URL with no --allow-host must exit 1."""
    transport = _transport({})
    args = _make_args(
        source="https://example.com/spec.json",
        workspace=str(tmp_path),
        allow_host=[],
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--allow-host" in err


def test_fetch_url_allow_host_mismatch_exits_1(tmp_path: Path):
    """B2: --allow-host that doesn't include the source host must exit 1."""
    transport = _transport({})
    args = _make_args(
        source="https://example.com/spec.json",
        workspace=str(tmp_path),
        allow_host=["api.example.com"],
    )
    rc = cmd_fetch.run(args, transport=transport)
    assert rc == 1


def test_fetch_local_path_skips_allow_host(tmp_path: Path, fixtures_dir: Path):
    """B2: local file paths skip allow-host check — they're not network calls."""
    args = _make_args(
        source=str(fixtures_dir / "tiny-openapi-3.json"),
        workspace=str(tmp_path),
        allow_host=[],  # not required for local
    )
    rc = cmd_fetch.run(args)
    assert rc == 0


def test_staleness_only_allows_api_github_com():
    """B2: the staleness check function constructs a URL that always targets
    api.github.com — never user-controllable."""
    import httpx as _httpx

    captured_urls: list[str] = []

    def handler(req: _httpx.Request) -> _httpx.Response:
        captured_urls.append(str(req.url))
        return _httpx.Response(
            200,
            json=[{"commit": {"committer": {"date": "2026-04-01T00:00:00Z"}}}],
        )

    transport = _httpx.MockTransport(handler)
    with _httpx.Client(transport=transport, trust_env=False) as client:
        cmd_fetch._check_staleness(
            "https://raw.githubusercontent.com/owner/repo/main/openapi.json",
            days=1,
            client=client,
            log=lambda m: None,
        )
    assert captured_urls
    for url in captured_urls:
        host = re.match(r"https://([^/]+)/", url).group(1)
        assert host == "api.github.com"


def test_external_ref_https_outside_allowlist_rejected(tmp_path: Path):
    """B3: external $ref to a host outside the allowlist exits 3."""
    poisoned = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"$ref": "https://attacker.com/x.json"}}
        },
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(poisoned))
    args = _make_args(
        source=str(spec_path),
        workspace=str(tmp_path / "ws"),
        allow_host=["api.example.com"],
        no_resolve=False,
    )
    rc = cmd_fetch.run(args)
    assert rc == 3


def test_external_ref_warns_but_succeeds_under_no_resolve(tmp_path: Path, capsys):
    """--no-resolve does not dereference, so a hostile $ref is not fatal — but
    the spec still lands on disk, so it must not land silently."""
    poisoned = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"$ref": "https://attacker.com/x.json"}},
            "/z": {"get": {"$ref": "file:///etc/passwd"}},
        },
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(poisoned))
    ws = tmp_path / "ws"
    args = _make_args(
        source=str(spec_path),
        workspace=str(ws),
        allow_host=["api.example.com"],
        no_resolve=True,
    )
    rc = cmd_fetch.run(args)
    assert rc == 0

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "attacker.com" in err
    assert "file://" in err
    # Warned, not silently swallowed — and not escalated to an error either.
    assert "ERROR" not in err

    # The refs are preserved verbatim; we warn about the artifact, we don't rewrite it.
    written = json.loads((ws / "raw" / "spec.json").read_text())
    assert written["paths"]["/y"]["get"]["$ref"] == "https://attacker.com/x.json"


def test_local_sibling_ref_still_works_under_no_resolve(tmp_path: Path, capsys):
    """A legitimate local multi-file spec: sibling refs are fatal on the resolve
    path (prance would read the file), so --no-resolve is the escape hatch. It
    must stay open."""
    multifile = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {"/y": {"get": {"$ref": "./components.yaml#/get"}}},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(multifile))
    args = _make_args(
        source=str(spec_path),
        workspace=str(tmp_path / "ws"),
        allow_host=["api.example.com"],
        no_resolve=True,
    )
    assert cmd_fetch.run(args) == 0
    assert "WARNING" in capsys.readouterr().err


def test_external_ref_https_in_allowlist_accepted(tmp_path: Path):
    """B3: external $ref to a host in the allowlist is accepted (prance may
    or may not actually fetch — that's not our concern here)."""
    ok = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"$ref": "https://api.example.com/sub.json"}}
        },
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(ok))
    args = _make_args(
        source=str(spec_path),
        workspace=str(tmp_path / "ws"),
        allow_host=["api.example.com"],
        no_resolve=False,
    )
    # Validation passes; prance may fail to resolve which is fine —
    # `_resolve_refs` returns the original spec in that case.
    rc = cmd_fetch.run(args)
    assert rc == 0


def test_external_ref_file_scheme_rejected(tmp_path: Path):
    """B3: external $ref with file:// scheme always exits 3."""
    poisoned = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"$ref": "file:///etc/passwd"}}
        },
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(poisoned))
    args = _make_args(
        source=str(spec_path),
        workspace=str(tmp_path / "ws"),
        allow_host=["api.example.com"],
        no_resolve=False,
    )
    rc = cmd_fetch.run(args)
    assert rc == 3


def test_internal_ref_always_accepted(tmp_path: Path):
    """B3: internal `#/...` $refs are always safe — never collected as external."""
    ok = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/y": {"get": {"responses": {"200": {
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Y"}
                }}
            }}}}
        },
        "components": {"schemas": {"Y": {"type": "object"}}},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(ok))
    args = _make_args(
        source=str(spec_path),
        workspace=str(tmp_path / "ws"),
        allow_host=[],
        no_resolve=False,
    )
    rc = cmd_fetch.run(args)
    assert rc == 0


def test_source_map_json_pointers_correct(tmp_path: Path):
    """H7: source-map pointers must be RFC-6901-encoded once, not twice.
    For `/v1/locations` the expected pointer is `/paths/~1v1~1locations/get`."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/v1/locations": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/v1/server_types/{id}": {
                "get": {"responses": {"200": {"description": "ok"}}}
            },
        },
    }
    sm = cmd_fetch._build_source_map(spec, spec_url=None, sha256="x")
    assert sm["operations"]["/v1/locations:get"]["original_pointer"] == (
        "/paths/~1v1~1locations/get"
    )
    assert sm["operations"]["/v1/server_types/{id}:get"]["original_pointer"] == (
        "/paths/~1v1~1server_types~1{id}/get"
    )
