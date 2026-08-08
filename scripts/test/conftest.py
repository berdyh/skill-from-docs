"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def hcloud_workspace(tmp_path: Path) -> Path:
    """Workspace pre-populated from the hcloud-offline fixtures.

    This is the layout the documented offline smoke test produces — note the
    `raw/` and `probes/` subdirectories, which a flat `cp` of the fixture
    directory does not create.
    """
    raw = tmp_path / "raw"
    raw.mkdir()
    probes = tmp_path / "probes"
    probes.mkdir()
    src = FIXTURES / "hcloud-offline"
    shutil.copy(src / "spec.json", raw / "spec.json")
    shutil.copy(src / "source-map.json", raw / "source-map.json")
    for name in ("locations-200.json", "datacenters-200.json", "server_types-200.json"):
        shutil.copy(src / name, probes / name)
    return tmp_path


def make_mock_transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Build a transport that returns canned responses by URL.

    `routes` keys are full URLs; the value is the response to return.
    Unknown URLs return 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404, text=""))

    return httpx.MockTransport(handler)
