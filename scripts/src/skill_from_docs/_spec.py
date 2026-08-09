"""Shared OpenAPI spec traversal — one definition of "an operation".

The `paths -> {path: {method: operation}}` walk used to be written out four
times (twice in `cmd_fetch`, twice in `cmd_consolidate`), each with its own copy
of the HTTP-method tuple. The copies drifted: `fetch` counted `trace`,
`consolidate` did not, so a spec containing a TRACE operation made
`fetch --count-endpoints` print N while `handoff.json` reported N-1 and the
operation got a `raw/source-map.json` entry but no `docs.md` section.

`trace` is a valid OpenAPI operation and is included. Adding a method now means
editing `HTTP_METHODS` and nothing else.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlparse


HTTP_METHODS: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "trace",
)


def iter_operations(spec: dict[str, Any] | None) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield `(path, lowercase method, operation)` for every operation in `spec`.

    Skips anything that is not shaped like an operation: a non-dict `paths`, a
    non-dict path item (`$ref`, `parameters`, junk), a key that is not an HTTP
    method (`parameters`, `servers`, `summary` all legally live beside methods),
    and a non-dict operation. Callers get only well-formed operations, so none
    of them repeats the guards.
    """
    paths = (spec or {}).get("paths") or {}
    if not isinstance(paths, dict):
        return
    for path, methods in paths.items():
        # YAML does not require mapping keys to be strings, so a remote spec can
        # hand us `paths: {1: {get: ...}}`. Downstream does `path.replace(...)`
        # to build a JSON Pointer, which would raise AttributeError and break
        # the numeric exit-code contract with a traceback.
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            yield path, method.lower(), op


def count_operations(spec: dict[str, Any] | None) -> int:
    return sum(1 for _ in iter_operations(spec))


def json_pointer(path: str, method: str) -> str:
    """RFC 6901 pointer to an operation: `/paths/<escaped path>/<method>`.

    Escape order matters — `~` becomes `~0` before `/` becomes `~1`, otherwise
    the `~` introduced by the second pass gets escaped by the first.
    """
    escaped = path.replace("~", "~0").replace("/", "~1")
    return f"/paths/{escaped}/{method.lower()}"


def declared_api_hosts(spec: dict[str, Any] | None) -> set[str]:
    """Hosts the spec's own `servers` block says the API lives on.

    Relative server URLs ("/v1") name no host and are skipped: they mean "same
    origin as wherever you got this spec", which is the opposite of a mirror.
    """
    hosts: set[str] = set()
    for server in (spec or {}).get("servers") or []:
        if not isinstance(server, dict):
            continue
        url = server.get("url")
        if not isinstance(url, str):
            continue
        try:
            host = urlparse(url).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower())
    return hosts


def classify_mirror(spec_url: str | None, spec: dict[str, Any] | None) -> str | None:
    """Return `"unofficial"` when the spec was served by a host other than the
    API's own, else None.

    This records a *fact* — "this spec came from somewhere other than the API it
    describes" — not a judgement about who maintains it. A vendor's own spec
    published to a code-hosting site is labelled the same as a third-party
    re-host, because from here they are indistinguishable. Downstream should
    read it as "verify this source", not "distrust this source".

    Returns None when either side is unknown, so absence of the label never
    means "verified official".
    """
    if not spec_url:
        return None
    try:
        source_host = urlparse(spec_url).hostname
    except ValueError:
        return None
    declared = declared_api_hosts(spec)
    if not source_host or not declared:
        return None
    return None if source_host.lower() in declared else "unofficial"
