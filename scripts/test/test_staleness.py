"""Tests for the GitHub-commits-based staleness check."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx
import pytest

from skill_from_docs import cmd_fetch
from skill_from_docs._http import build_client


def _make_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return routes.get(url, httpx.Response(404, text=""))

    transport = httpx.MockTransport(handler)
    return build_client(transport=transport, timeout=5.0)


def test_stale_mirror_warns(fixtures_dir: Path):
    stale = fixtures_dir / "github-stale-mirror.json"
    routes = {
        "https://api.github.com/repos/foo/bar/commits?path=spec.json&sha=main&per_page=1": httpx.Response(
            200, text=stale.read_text(), headers={"Content-Type": "application/json"}
        ),
    }
    client = _make_client(routes)
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://raw.githubusercontent.com/foo/bar/main/spec.json",
        90,
        client,
        log=messages.append,
    )
    client.close()
    assert any("days old" in m for m in messages), messages


def test_fresh_mirror_silent(fixtures_dir: Path):
    fresh = fixtures_dir / "github-fresh-mirror.json"
    routes = {
        "https://api.github.com/repos/foo/bar/commits?path=spec.json&sha=main&per_page=1": httpx.Response(
            200, text=fresh.read_text(), headers={"Content-Type": "application/json"}
        ),
    }
    client = _make_client(routes)
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://raw.githubusercontent.com/foo/bar/main/spec.json",
        365 * 5,  # threshold far in past
        client,
        log=messages.append,
    )
    client.close()
    assert not any("days old" in m for m in messages)


def test_non_github_mirror_is_skipped():
    client = _make_client({})
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://example.com/openapi.json", 90, client, log=messages.append
    )
    client.close()
    assert any("non-GitHub" in m for m in messages)
