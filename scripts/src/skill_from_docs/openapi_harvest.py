"""openapi-harvest CLI dispatcher.

Single entry point with six subcommands. See `--help` for usage.
"""

from __future__ import annotations

import argparse
import sys

from . import cmd_auth, cmd_consolidate, cmd_fetch, cmd_probe, cmd_quick_diff, cmd_validate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openapi-harvest",
        description="Harvest OpenAPI specs into a skill-from-docs workspace.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    cmd_fetch.add_parser(subparsers)
    cmd_auth.add_parser(subparsers)
    cmd_probe.add_parser(subparsers)
    cmd_quick_diff.add_parser(subparsers)
    cmd_consolidate.add_parser(subparsers)
    cmd_validate.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    if sys.version_info < (3, 10):
        print("ERROR: Python >= 3.10 required", file=sys.stderr)
        return 5
    try:
        return func(args)
    except ImportError as e:
        print(
            f"ERROR: missing dependency: {e}\n"
            f"Fix: pip install -e ~/.claude/skills/skill-from-docs/scripts",
            file=sys.stderr,
        )
        return 5


if __name__ == "__main__":
    sys.exit(main())
