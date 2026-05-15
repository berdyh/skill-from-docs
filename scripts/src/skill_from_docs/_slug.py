"""Derive a workspace slug from a URL or local path."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse


_SLUG_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def slug_from_url(source: str) -> str:
    """Return a host-derived slug for a URL, or a sanitized path stem.

    Examples:
        >>> slug_from_url("https://api.hetzner.cloud/v1/locations")
        'api.hetzner.cloud'
        >>> slug_from_url("https://raw.githubusercontent.com/foo/bar/main/openapi.json")
        'raw.githubusercontent.com'
    """
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        host = parsed.netloc.split("@")[-1]
        host = host.split(":")[0]
        return _SLUG_SAFE.sub("-", host).strip("-")

    # local path or stdin
    if source == "@-":
        return "stdin"
    base = os.path.basename(source) or "workspace"
    stem = os.path.splitext(base)[0]
    return _SLUG_SAFE.sub("-", stem).strip("-") or "workspace"


def default_workspace(source: str) -> str:
    """Return the default workspace path for a given source URL/path."""
    home = os.path.expanduser("~")
    slug = slug_from_url(source)
    return os.path.join(home, ".claude", "skill-from-docs", slug)
