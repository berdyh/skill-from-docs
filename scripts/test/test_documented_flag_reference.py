"""`references/probing-tools.md` must name every flag the CLI accepts.

Drift #8 of the 2026-08 documentation sweep was 26 flags with zero mentions in
any doc — `--output-spec`, `--user-agent`, `--workspace`, `--redact-body-key`,
`--short-circuit` among them. Prose covering "the interesting ones" is exactly
how that happens: a flag added to a `cmd_*` parser has no reason to remind
anybody it also needs a row in the reference table.

This is one of the few documentation claims that *is* mechanically checkable —
it is a set-membership question, not a semantic one — so it gets a test rather
than a `docs-guard` grep. It deliberately checks only presence. Whether the
documented default and description are *correct* is not something any test can
answer, and pretending otherwise is the failure mode `DEFERRED.md` calls "a
documented control that does not exist".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_from_docs.openapi_harvest import build_parser


DOC = Path(__file__).resolve().parents[2] / "references" / "probing-tools.md"

# `-h/--help` is argparse's, identical everywhere, and documenting it per
# subcommand would be noise.
IGNORED = {"-h", "--help"}


def _subparsers():
    parser = build_parser()
    for action in parser._actions:
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            return action.choices
    raise AssertionError("no subparsers found on the top-level parser")


def _declared(subparser) -> tuple[set[str], set[str]]:
    """Return (long option strings, positional dests) for one subcommand."""
    options: set[str] = set()
    positionals: set[str] = set()
    for action in subparser._actions:
        if action.option_strings:
            options.update(
                opt
                for opt in action.option_strings
                if opt.startswith("--") and opt not in IGNORED
            )
        else:
            positionals.add(action.dest)
    return options, positionals


@pytest.mark.parametrize("subcommand", sorted(_subparsers()))
def test_every_flag_appears_in_the_reference(subcommand: str):
    doc = DOC.read_text(encoding="utf-8")
    options, positionals = _declared(_subparsers()[subcommand])

    missing = sorted(opt for opt in options if f"`{opt}" not in doc)
    assert not missing, (
        f"`openapi-harvest {subcommand}` accepts {missing}, which "
        f"{DOC.name} never names. Add a row to its 'Complete flag reference' "
        "section — a flag nobody documents is a flag nobody finds."
    )

    missing_pos = sorted(
        dest for dest in positionals if f"`{dest.upper()}`" not in doc
    )
    assert not missing_pos, (
        f"`openapi-harvest {subcommand}` takes positional {missing_pos}, which "
        f"{DOC.name} never names as `{'/'.join(p.upper() for p in missing_pos)}`."
    )
