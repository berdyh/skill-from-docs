"""The CLI surface, pinned.

Parent parsers put `--allow-host` in one place, which is what makes its help
text reach all four subcommands that need it — and also what makes it possible
to hand a flag to a subcommand that never took one. These tests spell out the
exact option set, default and type of every subcommand so that mistake is a test
failure rather than a shipped surprise.
"""

import argparse

import pytest

from skill_from_docs.openapi_harvest import build_parser


# The full accepted-option set per subcommand, transcribed from the parsers as
# they stood before parent parsers were introduced. Anything added or removed
# here is a CLI change and must be a deliberate one.
EXPECTED_OPTIONS = {
    "fetch": {
        "-h/--help", "--allow-host", "--timeout", "--workspace", "-q/--quiet",
        "-o/--output-spec", "--output-source-map", "--no-resolve", "--user-agent",
        "--staleness-days", "--staleness-api-host", "--staleness-api-style",
        "--count-endpoints",
    },
    "auth": {
        "-h/--help", "--allow-host", "--no-follow-redirects", "--timeout",
        "--workspace", "-q/--quiet", "--token", "-o/--output", "--short-circuit",
        "--no-short-circuit", "--include-query-auth", "--basic-creds",
        "--basic-creds-env", "--spec", "--bad-token-pattern",
    },
    "probe": {
        "-h/--help", "--allow-host", "--no-follow-redirects", "--timeout",
        "--workspace", "-q/--quiet", "-X/--method", "-H/--header", "-d/--data",
        "-o/--output", "--scope", "--no-redact", "--redact-body-key",
        "--redact-body-pattern", "--max-retries", "--dry-run",
    },
    "quick-diff": {"-h/--help", "-o/--output", "--source-map", "--strict"},
    "consolidate": {
        "-h/--help", "-q/--quiet", "--merge-probes", "--tag", "--narrative-dir",
        "--emit-handoff", "--no-emit-handoff", "--no-sanitize-descriptions",
        "--dry-run",
    },
    "validate": {"-h/--help", "--allow-host", "--strict", "--network", "--json"},
}

EXPECTED_POSITIONALS = {
    "fetch": ["source"],
    "auth": ["endpoint"],
    "probe": ["url"],
    "quick-diff": ["fixture", "spec"],
    "consolidate": ["workspace"],
    "validate": ["workspace"],
}

# Per-subcommand, deliberately different: `auth` fires a cascade of probes at one
# endpoint, `fetch`/`probe` download a spec or capture an arbitrary response.
EXPECTED_TIMEOUT_DEFAULTS = {"fetch": 30.0, "auth": 10.0, "probe": 30.0}


def _sub(name: str) -> argparse.ArgumentParser:
    parser = build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices[name]


def _options(p: argparse.ArgumentParser) -> set[str]:
    return {"/".join(a.option_strings) for a in p._actions if a.option_strings}


@pytest.mark.parametrize("name", sorted(EXPECTED_OPTIONS))
def test_accepted_options_are_exactly_what_they_were(name):
    assert _options(_sub(name)) == EXPECTED_OPTIONS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_POSITIONALS))
def test_positionals_are_unchanged(name):
    p = _sub(name)
    assert [a.dest for a in p._actions if not a.option_strings] == EXPECTED_POSITIONALS[name]


@pytest.mark.parametrize("name,default", sorted(EXPECTED_TIMEOUT_DEFAULTS.items()))
def test_timeout_defaults_did_not_get_unified(name, default):
    """A shared parent must not flatten these into one number."""
    args = build_parser().parse_args(_minimal_argv(name))
    assert args.timeout == default


def _minimal_argv(name: str) -> list[str]:
    return {
        "fetch": ["fetch", "spec.json"],
        "auth": ["auth", "https://api.example.com/v1", "--token", "t"],
        "probe": ["probe", "https://api.example.com/v1", "--scope", "ad-hoc"],
    }[name]


def test_allow_host_help_is_the_same_text_everywhere():
    texts = set()
    for name in ("fetch", "auth", "probe", "validate"):
        action = next(a for a in _sub(name)._actions if a.option_strings == ["--allow-host"])
        texts.add(action.help)
    assert len(texts) == 1, "the security-critical flag must read the same on every subcommand"

    help_text = texts.pop()
    # It is required by four subcommands but only ever explained by one before
    # this. Name all four, and say what an empty value means — `--allow-host ""`
    # is truthy but builds a permit-everything allowlist.
    for phrase in ("auth", "probe", "fetch", "validate --network", "non-empty"):
        assert phrase in help_text


def test_allow_host_still_appends_and_defaults_to_empty():
    args = build_parser().parse_args(
        ["probe", "https://a.example/x", "--scope", "ad-hoc", "--allow-host", "a", "--allow-host", "b"]
    )
    assert args.allow_host == ["a", "b"]
    bare = build_parser().parse_args(["probe", "https://a.example/x", "--scope", "ad-hoc"])
    assert bare.allow_host == []


def test_no_follow_redirects_is_still_a_no_op_that_stays_false():
    for argv in (
        ["probe", "https://a.example/x", "--scope", "ad-hoc"],
        ["probe", "https://a.example/x", "--scope", "ad-hoc", "--no-follow-redirects"],
        ["auth", "https://a.example/x", "--token", "t"],
        ["auth", "https://a.example/x", "--token", "t", "--no-follow-redirects"],
    ):
        assert build_parser().parse_args(argv).follow_redirects is False


def test_parent_parsers_hand_out_independent_actions():
    """`parents=` copies actions by reference, so a shared instance would make
    two subcommands share one Action — and one default."""
    fetch_timeout = next(a for a in _sub("fetch")._actions if a.option_strings == ["--timeout"])
    auth_timeout = next(a for a in _sub("auth")._actions if a.option_strings == ["--timeout"])
    assert fetch_timeout is not auth_timeout
