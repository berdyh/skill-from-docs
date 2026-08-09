"""Tests for the portable staleness check.

Covers the four built-in mirror-host recognizers (GitHub, GitLab, Gitea via
codeberg, Bitbucket), the self-hosted explicit-flag path, the flag-pairing
contract, and the actionable note shown for unrecognized hosts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from conftest import make_mock_transport

from skill_from_docs import cmd_fetch


# ---------------------------------------------------------------------------
# GitHub (existing baseline)
# ---------------------------------------------------------------------------


def test_staleness_github_warns_when_stale(fixtures_dir: Path):
    stale = fixtures_dir / "github-stale-mirror.json"
    api_url = (
        "https://api.github.com/repos/foo/bar/commits"
        "?path=spec.json&sha=main&per_page=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=stale.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://raw.githubusercontent.com/foo/bar/main/spec.json",
        90,
        log=messages.append,
        transport=transport,
    )
    assert any("days old" in m for m in messages), messages
    assert any("github" in m for m in messages), messages


def test_staleness_github_fresh_silent(fixtures_dir: Path):
    fresh = fixtures_dir / "github-fresh-mirror.json"
    api_url = (
        "https://api.github.com/repos/foo/bar/commits"
        "?path=spec.json&sha=main&per_page=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=fresh.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://raw.githubusercontent.com/foo/bar/main/spec.json",
        365 * 5,
        log=messages.append,
        transport=transport,
    )
    assert not any("days old" in m for m in messages)


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


def test_staleness_gitlab_via_gitlab_raw(fixtures_dir: Path):
    """Source URL is a gitlab.com raw URL. The staleness call must go to
    gitlab.com/api/v4 with the project encoded as `owner%2Frepo`, and the
    response must be parsed via `committed_date` (not GitHub's nested path)."""
    fresh = fixtures_dir / "gitlab-fresh-mirror.json"
    # Note: owner%2Frepo URL-encoding in the API path.
    api_url = (
        "https://gitlab.com/api/v4/projects/foo%2Fbar/repository/commits"
        "?path=openapi.yaml&ref_name=main&per_page=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=fresh.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://gitlab.com/foo/bar/-/raw/main/openapi.yaml",
        365 * 5,  # fresh — no warning expected
        log=messages.append,
        transport=transport,
    )
    # Successful parse, no "days old" warning because the date is recent.
    assert not any("days old" in m for m in messages)
    # And no fall-through note about unsupported hosts.
    assert not any("unavailable" in m for m in messages)


# ---------------------------------------------------------------------------
# Gitea (codeberg)
# ---------------------------------------------------------------------------


def test_staleness_gitea_via_codeberg_raw(fixtures_dir: Path):
    """codeberg.org uses the Gitea API at /api/v1/...; date lives at
    commit.committer.date (GitHub-shaped). Branch-pinned raw URLs use the
    `/raw/branch/{branch}/...` form."""
    fresh = fixtures_dir / "gitea-fresh-mirror.json"
    api_url = (
        "https://codeberg.org/api/v1/repos/foo/bar/commits"
        "?path=openapi.json&sha=main&limit=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=fresh.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://codeberg.org/foo/bar/raw/branch/main/openapi.json",
        365 * 5,
        log=messages.append,
        transport=transport,
    )
    assert not any("days old" in m for m in messages)
    assert not any("unavailable" in m for m in messages)


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------


def test_staleness_bitbucket_via_bitbucket_raw(fixtures_dir: Path):
    """Bitbucket Cloud's API lives at api.bitbucket.org/2.0; the commit date
    is `values[0].date` rather than a nested commit.committer path."""
    fresh = fixtures_dir / "bitbucket-fresh-mirror.json"
    api_url = (
        "https://api.bitbucket.org/2.0/repositories/team/repo/commits"
        "?include=main&path=openapi.json&pagelen=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=fresh.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://bitbucket.org/team/repo/raw/main/openapi.json",
        365 * 5,
        log=messages.append,
        transport=transport,
    )
    assert not any("days old" in m for m in messages)
    assert not any("unavailable" in m for m in messages)


# ---------------------------------------------------------------------------
# Self-hosted via explicit flags
# ---------------------------------------------------------------------------


def test_staleness_self_hosted_explicit_flags(fixtures_dir: Path):
    """git.example.com is not on the built-in list. With explicit
    --staleness-api-host + --staleness-api-style gitea, the check should
    target git.example.com/api/v1/..."""
    fresh = fixtures_dir / "gitea-fresh-mirror.json"
    api_url = (
        "https://git.example.com/api/v1/repos/foo/bar/commits"
        "?path=openapi.json&sha=main&limit=1"
    )
    transport = make_mock_transport(
        {api_url: httpx.Response(200, text=fresh.read_text())}
    )
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://git.example.com/foo/bar/raw/branch/main/openapi.json",
        365 * 5,
        log=messages.append,
        transport=transport,
        explicit_host="git.example.com",
        explicit_style="gitea",
    )
    # No unavailable-host note; the explicit flags satisfied the resolver.
    assert not any("unavailable" in m for m in messages)


def test_staleness_unknown_host_emits_actionable_note():
    """When neither auto-derive nor explicit flags resolve a target, the
    contributor gets a clear stderr note naming the flags that would enable
    the check."""
    transport = make_mock_transport({})
    messages: list[str] = []
    cmd_fetch._check_staleness(
        "https://api.example.com/openapi.json",
        90,
        log=messages.append,
        transport=transport,
    )
    full = "\n".join(messages)
    assert "staleness check unavailable" in full
    assert "--staleness-api-host" in full
    assert "--staleness-api-style" in full
    assert "github|gitlab|gitea|bitbucket" in full or "github" in full


# ---------------------------------------------------------------------------
# Flag-pairing contract (must use both flags together)
# ---------------------------------------------------------------------------


def _fetch_args(**overrides) -> argparse.Namespace:
    base = dict(
        source="https://api.example.com/spec.json",
        output_spec=None,
        output_source_map=None,
        no_resolve=False,
        user_agent=None,
        timeout=5.0,
        staleness_days=90,
        staleness_api_host=None,
        staleness_api_style=None,
        count_endpoints=False,
        allow_host=["api.example.com"],
        workspace=None,
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_staleness_flags_must_be_paired__only_host_fails(tmp_path: Path, capsys):
    args = _fetch_args(
        workspace=str(tmp_path),
        staleness_api_host="git.example.com",
        staleness_api_style=None,
    )
    rc = cmd_fetch.run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be passed together" in err


def test_staleness_flags_must_be_paired__only_style_fails(tmp_path: Path, capsys):
    args = _fetch_args(
        workspace=str(tmp_path),
        staleness_api_host=None,
        staleness_api_style="gitea",
    )
    rc = cmd_fetch.run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be passed together" in err
