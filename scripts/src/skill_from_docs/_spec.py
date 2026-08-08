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
