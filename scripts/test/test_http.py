"""Unit tests for the shared HTTP substrate."""

from __future__ import annotations

import pytest

from skill_from_docs._http import AllowlistViolation, HostAllowlist, require_allowlist


def test_check_permits_everything_when_empty():
    """`check` gates a request the user asked for; no allowlist means no
    restriction was named."""
    HostAllowlist([]).check("https://anything.example.com/x")
    HostAllowlist(None).check("https://anything.example.com/x")


def test_lists_host_lists_nothing_when_empty():
    """`lists_host` asks whether the user *named* a host. The opposite reading
    of "empty" from `check`, deliberately: callers vetting a target the user
    never typed — an `$ref` inside a downloaded spec — need fail-closed."""
    assert HostAllowlist([]).lists_host("anything.example.com") is False
    assert HostAllowlist(["api.example.com"]).lists_host("api.example.com") is True
    assert HostAllowlist(["API.Example.com"]).lists_host("api.example.COM") is True
    assert HostAllowlist(["api.example.com"]).lists_host("evil.example.net") is False


def test_check_rejects_off_allowlist_host():
    with pytest.raises(AllowlistViolation):
        HostAllowlist(["api.example.com"]).check("https://evil.example.net/x")


def test_require_allowlist_rejects_the_empty_string(capsys):
    """argparse `append` turns an unset shell variable into `[""]`, which is
    truthy but builds an allowlist that permits everything. Gates must test the
    constructed object, which is what this helper exists to force."""
    assert require_allowlist([""], subcommand="probe") is None
    assert "allow-host" in capsys.readouterr().err
    assert require_allowlist([], subcommand="probe") is None

    built = require_allowlist(["api.example.com"], subcommand="probe")
    assert built is not None and built.lists_host("api.example.com")
