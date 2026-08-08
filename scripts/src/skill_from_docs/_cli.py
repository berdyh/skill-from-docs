"""Parent parsers for the flags more than one subcommand takes.

B7's point was not line count. `--allow-host` is the security-critical flag —
four subcommands refuse to run without it — and until this module existed only
`validate --help` said anything about it at all. Declaring a flag once means its
help text reaches every subcommand that accepts it, and a later correction
cannot land on three parsers and miss the fourth.

Each function returns a **fresh** `ArgumentParser`. That is not incidental:
`parents=` copies action objects by reference, so a single shared instance would
make `auth`'s `--timeout` default and `fetch`'s the same object. Handing out new
parsers is what lets `timeout(default=10.0)` and `timeout(default=30.0)`
coexist.

Adding a flag here does **not** give it to anybody. A subcommand accepts it only
by naming the parent in its own `parents=[...]`, so the set of flags each
subcommand takes stays visible in its own `add_parser`.
"""

from __future__ import annotations

import argparse


def _parent() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(add_help=False)


def allow_host() -> argparse.ArgumentParser:
    """`--allow-host` — the outbound host allowlist.

    Empty means "permit every host", which is why the subcommands that make
    network calls treat an absent or empty value as a user error rather than a
    default. The help says so because the failure it prevents (`--allow-host
    "$UNSET_VAR"` expanding to `[""]`, truthy but permit-everything) is invisible
    from the command line.
    """
    p = _parent()
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="host this command may contact (repeatable). Required for auth, probe, "
        "fetch when the source is a URL, and validate --network; must name at least "
        "one non-empty host, since an empty allowlist permits every host.",
    )
    return p


def workspace_flag() -> argparse.ArgumentParser:
    """`--workspace` for the subcommands that derive one from their target.

    Not for `consolidate` and `validate`: those take the workspace as a
    positional, because they operate on an existing one rather than creating it.
    """
    p = _parent()
    p.add_argument(
        "--workspace",
        metavar="DIR",
        help="workspace directory to write into "
        "(default: ~/.claude/skill-from-docs/<tool-slug>/, derived from the target).",
    )
    return p


def quiet() -> argparse.ArgumentParser:
    p = _parent()
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress progress output on stderr; errors are still printed.",
    )
    return p


def timeout(*, default: float) -> argparse.ArgumentParser:
    """`--timeout`, whose default is per-subcommand and stays that way.

    `auth` uses 10s because it issues a cascade of probes against one endpoint;
    `fetch` and `probe` use 30s because they download a whole spec or capture an
    arbitrary response. Unifying them would be a behaviour change, so the
    default is a parameter.

    Non-positive values are rejected in each `run()` by
    `_http.require_positive_timeout`, not by an argparse `type=` — see that
    function for why.
    """
    p = _parent()
    p.add_argument(
        "--timeout",
        type=float,
        default=default,
        metavar="SECONDS",
        help=f"per-request timeout in seconds (default: {default}). Must be positive.",
    )
    return p


def no_follow_redirects() -> argparse.ArgumentParser:
    """A flag that toggles nothing, kept so existing invocations keep working.

    Redirects are never followed. A 30x to an attacker host is the canonical
    token-leak path, and following one safely means reproducing httpx's
    cross-origin credential stripping on top of the allowlist check — two subtle
    guards to maintain for a capability nothing here needs. The `Location`
    header is captured (redacted) instead.
    """
    p = _parent()
    p.add_argument(
        "--no-follow-redirects",
        dest="follow_redirects",
        action="store_false",
        default=False,
        help="accepted for compatibility; redirects are never followed",
    )
    return p
