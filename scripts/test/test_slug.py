"""Tests for `_slug` — workspace naming and workspace discovery.

Two defects motivate this file.

The slug was documented as `<host>-<path-tail>` and implemented as the bare
hostname, so every GitHub raw URL collided on `raw.githubusercontent.com`. The
documented rule would not have fixed it either: the *tail* of
`/OwnerA/agent-tools/main/openapi.json` is `openapi.json`, the least
distinguishing part of the URL.

The harder half is the other direction. A slug that changes when the same URL is
re-typed with a trailing slash, a different scheme, or different capitalisation
breaks Phase 0.5 cache detection silently — the workspace is still there, but
nothing looks for it under that name. So equivalence gets as many tests as
disambiguation does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from skill_from_docs import _slug


AGENT_TOOLS_A = "https://raw.githubusercontent.com/OwnerA/agent-tools/main/openapi.json"
AGENT_TOOLS_B = "https://raw.githubusercontent.com/OwnerB/agent-tools/main/openapi.json"


# --------------------------------------------------------------------------
# Disambiguation
# --------------------------------------------------------------------------


def test_same_named_repos_from_different_owners_do_not_collide():
    """The `agent-tools` case `SKILL.md` names, which the old slug got wrong."""
    assert _slug.slug_from_url(AGENT_TOOLS_A) != _slug.slug_from_url(AGENT_TOOLS_B)
    assert _slug.slug_from_url(AGENT_TOOLS_A) == (
        "raw.githubusercontent.com-ownera-agent-tools"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Forge URLs: owner + repo survive, the ref and the view do not.
        (AGENT_TOOLS_A, "raw.githubusercontent.com-ownera-agent-tools"),
        (
            "https://raw.githubusercontent.com/o/r/refs/heads/main/README.md",
            "raw.githubusercontent.com-o-r",
        ),
        (
            "https://github.com/humanlayer/12-factor-agents/tree/main/content",
            "github.com-humanlayer-12-factor-agents-content",
        ),
        (
            "https://gitlab.com/group/subgroup/project/-/raw/main/openapi.json",
            "gitlab.com-group-subgroup-project",
        ),
        # Docs sites: the path is the identity, so it is kept.
        ("https://docs.stripe.com/api", "docs.stripe.com-api"),
        (
            "https://api-docs.didox.uz/ru/integration-registration",
            "api-docs.didox.uz-ru-integration-registration",
        ),
        ("https://api.hetzner.cloud/v1/locations", "api.hetzner.cloud-v1-locations"),
        # No path at all: host alone, as before.
        ("https://example.com", "example.com"),
        # A segment without a recognised extension is not a filename.
        ("https://example.com/v3/api-docs", "example.com-v3-api-docs"),
        # A *non*-generic filename keeps its stem and loses its extension.
        ("https://example.com/o/r/main/hetzner.yaml", "example.com-o-r-hetzner"),
    ],
)
def test_slug_table(source: str, expected: str):
    assert _slug.slug_from_url(source) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "swagger.json",
        "spec.json",
        "schema.json",
        "index.html",
        "README.md",
    ],
)
def test_generic_spec_filenames_carry_no_information(filename: str):
    assert (
        _slug.slug_from_url(f"https://example.com/acme/widgets/main/{filename}")
        == "example.com-acme-widgets"
    )


@pytest.mark.parametrize(
    "noise", ["main", "master", "trunk", "blob", "raw", "tree", "refs/heads", "tags"]
)
def test_vcs_ref_and_view_segments_are_dropped(noise: str):
    assert (
        _slug.slug_from_url(f"https://example.com/acme/widgets/{noise}/thing")
        == "example.com-acme-widgets-thing"
    )


# --------------------------------------------------------------------------
# Equivalence — the same tool, spelled differently, is one workspace
# --------------------------------------------------------------------------


CANONICAL = "https://docs.stripe.com/api"

EQUIVALENT_SPELLINGS = {
    "trailing slash": "https://docs.stripe.com/api/",
    "http scheme": "http://docs.stripe.com/api",
    "explicit port": "https://docs.stripe.com:443/api",
    "non-default port": "https://docs.stripe.com:8443/api",
    "userinfo": "https://user:pa55w0rd@docs.stripe.com/api",
    "query string": "https://docs.stripe.com/api?token=SEKRIT&key=abc",
    "fragment": "https://docs.stripe.com/api#authentication",
    "upper case": "https://DOCS.Stripe.com/API",
    "percent-encoded": "https://docs.stripe.com/%61pi",
}


@pytest.mark.parametrize(
    "spelling", sorted(EQUIVALENT_SPELLINGS), ids=sorted(EQUIVALENT_SPELLINGS)
)
def test_equivalent_spellings_produce_one_slug(spelling: str):
    """A slug that moves when the URL is re-typed loses the cache silently."""
    assert _slug.slug_from_url(EQUIVALENT_SPELLINGS[spelling]) == _slug.slug_from_url(
        CANONICAL
    )


# --------------------------------------------------------------------------
# A slug must never become a place a credential is stored
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "https://petstore:hunter2@api.example.com/acme/widgets",
        "https://api.example.com/acme/widgets?api_key=hunter2",
        "https://api.example.com/acme/widgets?token=hunter2#hunter2",
        "https://hunter2@api.example.com/acme/widgets",
    ],
)
def test_credentials_never_reach_the_directory_name(source: str):
    """Userinfo, query and fragment are dropped, not sanitized into the path.

    `raw/source-map.json` at 0o600 is the only artifact allowed to hold an
    un-redacted URL. A directory name is world-readable and lands in shell
    history, `ls` output and manifest paths.
    """
    slug = _slug.slug_from_url(source)
    assert "hunter2" not in slug
    assert slug == "api.example.com-acme-widgets"
    assert "hunter2" not in _slug.default_workspace(source)


# --------------------------------------------------------------------------
# Safety: the result is one usable path component
# --------------------------------------------------------------------------


SAFETY_INPUTS = [
    "@-",
    "",
    "..",
    "../..",
    "/",
    "./local/petstore/openapi.json",
    "/home/me/specs/hetzner.yaml",
    "openapi.json",
    "https://example.com/../../etc/passwd",
    "https://example.com/" + "verylongsegment/" * 40 + "openapi.json",
    "https://" + "a" * 200 + ".example.com/x",
    "https://example.com/-leading-dash/.leading-dot",
    "https://[::1]:8080/v1/spec",
    "not a url at all",
    "ftp://example.com/spec.json",
]


@pytest.mark.parametrize("source", SAFETY_INPUTS)
def test_slug_is_a_safe_bounded_non_empty_path_component(source: str):
    slug = _slug.slug_from_url(source)
    assert slug, f"empty slug for {source!r}"
    assert "/" not in slug and "\\" not in slug
    assert os.sep not in slug
    assert ".." not in slug
    assert slug not in (".", "..")
    assert not slug.startswith(("-", "."))
    assert not slug.endswith(("-", "."))
    assert len(slug) <= _slug.MAX_SLUG_LEN
    # It must survive being used as a directory name, unchanged.
    assert os.path.basename(os.path.join("/root", slug)) == slug


def test_long_slugs_are_truncated_but_still_distinct():
    """Truncation alone would re-introduce the collision, so a digest of the
    whole slug replaces the discarded tail."""
    base = "https://example.com/" + "o" * 70 + "/"
    a = _slug.slug_from_url(base + "alpha/openapi.json")
    b = _slug.slug_from_url(base + "beta/openapi.json")
    assert len(a) == _slug.MAX_SLUG_LEN and len(b) == _slug.MAX_SLUG_LEN
    assert a != b


def test_only_the_first_three_path_segments_identify_a_project():
    """A deliberate ceiling: deeper subpages of one docs site share a workspace,
    which is what cache detection wants. Disambiguation happens in the segments
    that name owner/repo (or group/subgroup/project), which come first."""
    assert _slug.slug_from_url(
        "https://docs.example.com/v1/api/charges/create"
    ) == _slug.slug_from_url("https://docs.example.com/v1/api/charges/refund")


def test_stdin_and_local_paths():
    assert _slug.slug_from_url("@-") == "stdin"
    assert _slug.slug_from_url("./local/petstore/openapi.json") == "petstore"
    assert _slug.slug_from_url("/home/me/specs/hetzner.yaml") == "hetzner"


def test_slug_is_deterministic_across_processes():
    """`hash()` is salted per process; two check ids in this package were once
    derived from it and changed on every run. The truncation digest must not
    repeat that."""
    source = "https://example.com/" + "verylongsegment/" * 40 + "openapi.json"
    code = (
        "from skill_from_docs._slug import slug_from_url;"
        f"print(slug_from_url({source!r}))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    outputs = set()
    for seed in ("0", "1", "12345"):
        env["PYTHONHASHSEED"] = seed
        outputs.add(
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout.strip()
        )
    assert len(outputs) == 1, outputs


# --------------------------------------------------------------------------
# Migration notice
# --------------------------------------------------------------------------


def test_old_slug_collided_on_the_bare_host():
    """Pin what the bug was, so the notice path keeps pointing somewhere real."""
    assert _slug.legacy_slug_from_url(AGENT_TOOLS_A) == "raw.githubusercontent.com"
    assert _slug.legacy_slug_from_url(AGENT_TOOLS_A) == _slug.legacy_slug_from_url(
        AGENT_TOOLS_B
    )


def test_legacy_notice_names_both_paths_when_only_the_old_one_exists(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = Path(_slug.legacy_workspace(AGENT_TOOLS_A))
    old.mkdir(parents=True)

    notice = _slug.legacy_workspace_notice(AGENT_TOOLS_A)
    assert notice is not None
    assert str(old) in notice
    assert _slug.default_workspace(AGENT_TOOLS_A) in notice
    assert "--workspace" in notice


def test_legacy_notice_does_not_move_anything(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = Path(_slug.legacy_workspace(AGENT_TOOLS_A))
    old.mkdir(parents=True)
    (old / "docs.md").write_text("harvested")

    _slug.legacy_workspace_notice(AGENT_TOOLS_A)

    assert (old / "docs.md").read_text() == "harvested"
    assert not Path(_slug.default_workspace(AGENT_TOOLS_A)).exists()


def test_no_legacy_notice_when_the_new_workspace_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    Path(_slug.legacy_workspace(AGENT_TOOLS_A)).mkdir(parents=True)
    Path(_slug.default_workspace(AGENT_TOOLS_A)).mkdir(parents=True)
    assert _slug.legacy_workspace_notice(AGENT_TOOLS_A) is None


def test_no_legacy_notice_when_nothing_was_harvested(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _slug.legacy_workspace_notice(AGENT_TOOLS_A) is None


def test_no_legacy_notice_when_the_slug_did_not_change(tmp_path: Path, monkeypatch):
    """A bare host URL slugs the same under both rules; there is nothing to say."""
    monkeypatch.setenv("HOME", str(tmp_path))
    source = "https://example.com"
    assert _slug.slug_from_url(source) == _slug.legacy_slug_from_url(source)
    Path(_slug.default_workspace(source)).mkdir(parents=True)
    assert _slug.legacy_workspace_notice(source) is None
