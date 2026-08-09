"""Derive a workspace slug from a URL or local path, and locate workspaces.

The slug names a single directory under ``~/.claude/skill-from-docs/``. Two
properties have to hold at once, and they pull against each other:

* **Distinguishing.** Two unrelated projects that share a host must not share a
  workspace. The old slug was the bare hostname, so every GitHub raw URL in the
  world collided on ``raw.githubusercontent.com``.
* **Stable.** The same tool, spelled differently (trailing slash, ``http`` vs
  ``https``, an explicit port, userinfo, a query string, different letter case),
  must produce *one* slug. Phase 0.5 cache detection looks a workspace up by
  slug; a slug that moves when the URL is re-typed loses the cache silently,
  which is worse than the collision it was meant to fix.

The rule: ``<host>`` plus up to three *identifying* path segments, lowercased.
Everything that carries no identity is dropped — userinfo, port, query,
fragment, VCS ref/view segments (``main``, ``master``, ``blob``, ``raw``,
``tree``, ``refs``, ``heads``, ``tags``, ``trunk``, GitLab's ``-``) and a
trailing generic spec filename (``openapi.json``, ``swagger.yaml``,
``spec.json``, ``index.html``, ``README.md``, ...). A non-generic filename keeps
its stem and loses its extension.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import unquote, urlparse


__all__ = [
    "slug_from_url",
    "legacy_slug_from_url",
    "workspace_root",
    "default_workspace",
    "legacy_workspace",
    "legacy_workspace_notice",
]


# Anything outside this class becomes "-". Input is lowercased first, so an
# uppercase letter is *not* punctuation — it is folded, not replaced.
_SLUG_SAFE = re.compile(r"[^a-z0-9._-]+")
_DOT_RUN = re.compile(r"\.{2,}")
_DASH_RUN = re.compile(r"-{2,}")

#: Longest slug we will hand to the filesystem. Well under the 255-byte limit
#: every mainstream filesystem imposes on a single component, with room for the
#: files created inside the directory.
MAX_SLUG_LEN = 80

#: Hex characters of sha256 appended when a slug is truncated. `hashlib`, not
#: `hash()`: Python's builtin is salted per process, so a `hash()`-derived slug
#: would name a different directory on every run.
_DIGEST_LEN = 8

#: Path segments kept, after noise removal. Enough for owner/repo/subpath on a
#: forge, or group/subgroup/project on GitLab.
MAX_PATH_SEGMENTS = 3

#: Segments naming a VCS ref or a hosting-provider view rather than a project.
_NOISE_SEGMENTS = frozenset(
    {
        "raw",
        "blob",
        "tree",
        "refs",
        "heads",
        "tags",
        "main",
        "master",
        "trunk",
        "-",
    }
)

#: A trailing segment counts as a *filename* only if it carries one of these.
#: Without an extension it is treated as a path segment and kept verbatim, so
#: `/v3/api-docs` keeps `api-docs`.
_FILE_EXTENSIONS = frozenset(
    {".json", ".yaml", ".yml", ".html", ".htm", ".md", ".txt"}
)

#: Filename stems that every project shares and therefore identify none.
_GENERIC_STEMS = frozenset(
    {
        "openapi",
        "openapi2",
        "openapi3",
        "swagger",
        "spec",
        "schema",
        "api-docs",
        "apidocs",
        "api_docs",
        "index",
        "readme",
    }
)


def _normalize(text: str) -> str:
    """Lowercase, percent-decode, and reduce to the safe character class."""
    text = unquote(text).lower()
    text = _SLUG_SAFE.sub("-", text)
    text = _DOT_RUN.sub(".", text)
    text = _DASH_RUN.sub("-", text)
    return text.strip("-._")


def _identifying_segments(path: str) -> list[str]:
    """Split a URL path into the segments that actually identify a project."""
    segments = [s for s in path.split("/") if s]
    out: list[str] = []
    for index, segment in enumerate(segments):
        if index == len(segments) - 1:
            stem, ext = os.path.splitext(segment)
            if ext.lower() in _FILE_EXTENSIONS:
                if _normalize(stem) in _GENERIC_STEMS:
                    continue
                segment = stem
        normalized = _normalize(segment)
        if not normalized or normalized in _NOISE_SEGMENTS:
            continue
        out.append(normalized)
    return out


def _finalize(slug: str) -> str:
    """Make `slug` a safe, bounded, non-empty single path component."""
    slug = _DASH_RUN.sub("-", slug).strip("-._")
    if not slug:
        return "workspace"
    if len(slug) > MAX_SLUG_LEN:
        # Truncation alone would let two long URLs share a directory, so the
        # discarded tail is represented by a digest of the whole slug.
        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        keep = MAX_SLUG_LEN - _DIGEST_LEN - 1
        slug = slug[:keep].rstrip("-._") + "-" + digest
    return slug


def _parse(source: str) -> tuple[str, str, str]:
    """Return (scheme, host, path). Empty scheme means "not a URL"."""
    try:
        parsed = urlparse(source)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            # `.hostname` drops userinfo and port and lowercases for us — which
            # is also why no credential can reach a directory name.
            return parsed.scheme, parsed.hostname or "", parsed.path
    except ValueError:
        pass
    return "", "", ""


def slug_from_url(source: str) -> str:
    """Return a stable, distinguishing slug for a URL, local path, or ``@-``.

    Examples:
        >>> slug_from_url("https://raw.githubusercontent.com/OwnerA/agent-tools/main/openapi.json")
        'raw.githubusercontent.com-ownera-agent-tools'
        >>> slug_from_url("https://raw.githubusercontent.com/OwnerB/agent-tools/main/openapi.json")
        'raw.githubusercontent.com-ownerb-agent-tools'
        >>> slug_from_url("https://api.hetzner.cloud/v1/locations")
        'api.hetzner.cloud-v1-locations'
        >>> slug_from_url("@-")
        'stdin'
    """
    scheme, host, path = _parse(source)
    if scheme:
        parts = [_normalize(host)] + _identifying_segments(path)[:MAX_PATH_SEGMENTS]
        return _finalize("-".join(p for p in parts if p))

    if source.strip() == "@-":
        return "stdin"

    # Local path. Keep the last identifying component, which is the basename
    # unless the basename is a generic spec filename.
    cleaned = source.split("?", 1)[0].split("#", 1)[0]
    normalized = os.path.normpath(cleaned) if cleaned else ""
    segments = _identifying_segments(normalized.replace(os.sep, "/"))
    return _finalize(segments[-1] if segments else "")


def legacy_slug_from_url(source: str) -> str:
    """The pre-disambiguation slug: bare hostname, or the basename stem.

    Kept only so :func:`legacy_workspace_notice` can point a user at a harvest
    made before the slug changed. Nothing else should call this.
    """
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        host = parsed.netloc.split("@")[-1]
        host = host.split(":")[0]
        return re.sub(r"[^a-zA-Z0-9._-]+", "-", host).strip("-")

    if source == "@-":
        return "stdin"
    base = os.path.basename(source) or "workspace"
    stem = os.path.splitext(base)[0]
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-") or "workspace"


def workspace_root() -> str:
    """The canonical parent directory every workspace lives under."""
    return os.path.join(os.path.expanduser("~"), ".claude", "skill-from-docs")


def default_workspace(source: str) -> str:
    """Return the default workspace path for a given source URL/path."""
    return os.path.join(workspace_root(), slug_from_url(source))


def legacy_workspace(source: str) -> str:
    """Where a pre-disambiguation harvest of `source` would have been written."""
    return os.path.join(workspace_root(), legacy_slug_from_url(source))


def legacy_workspace_notice(source: str) -> str | None:
    """Explain a slug-change miss, or return None when there is nothing to say.

    A harvest made before the slug changed still sits on disk under the bare
    hostname, but cache detection looks up by the new slug and will not find
    it — so a user with an existing harvest would silently get a fresh one.
    This names both paths. It deliberately does **not** move anything:
    relocating a user's directory without asking is not ours to do.
    """
    new = default_workspace(source)
    old = legacy_workspace(source)
    if os.path.abspath(new) == os.path.abspath(old):
        return None
    if os.path.isdir(new) or not os.path.isdir(old):
        return None
    return (
        "NOTICE: the workspace slug now identifies the project, not just the "
        "host.\n"
        f"  this run writes to: {new}\n"
        f"  an older harvest is intact at: {old}\n"
        "Nothing was moved or deleted. To re-use the older harvest, either move "
        "that directory to the new path yourself, or pass "
        f"`--workspace {old}`."
    )
