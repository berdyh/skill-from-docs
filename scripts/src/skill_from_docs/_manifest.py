"""Workspace `manifest.json` — read, write, append-run, hash-verify."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import __version__
from ._io import write_json
from ._redaction import redact_url


MANIFEST_FILENAME = "manifest.json"


def _redact_recursive(value: Any) -> Any:
    """Walk a JSON-like value and apply redact_url to any string that looks
    like an http(s) URL with sensitive query params. Used so manifest entries
    never persist raw credential-bearing URLs to disk. (B1)

    This is unconditional, and `manifest.json` is therefore **not** where A8's
    fetchable spec URL lives — that is `raw/source-map.json`'s `fetch_url`.
    Putting it here would need an exemption from this walk, which is the
    per-call-site judgement §D2 rejected a write-boundary choke point for
    reintroducing; the manifest is also read-modify-written on every run, where
    the source map is written once. Do not add one.
    """
    if isinstance(value, dict):
        return {k: _redact_recursive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_recursive(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return redact_url(value)
    return value


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_path(workspace: str) -> str:
    return os.path.join(workspace, MANIFEST_FILENAME)


def load_manifest(workspace: str) -> dict[str, Any]:
    """Read manifest.json, creating an empty skeleton if missing."""
    path = manifest_path(workspace)
    if not os.path.exists(path):
        return {"tool_version": __version__, "runs": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_manifest(workspace: str, data: dict[str, Any]) -> None:
    """Replace `manifest.json` atomically.

    `record_run` reads-modifies-writes this file on every subcommand, so it is
    the one artifact an interrupt could truncate into a state `verify_hashes`
    reports as a corrupt workspace. `_io.write_text` swaps it in with
    `os.replace`, so a reader sees either the previous run's manifest or this
    one.
    """
    os.makedirs(workspace, exist_ok=True)
    write_json(manifest_path(workspace), data)


def record_run(
    workspace: str,
    *,
    subcommand: str,
    args: dict[str, Any],
    started_at: str,
    finished_at: str,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
) -> None:
    """Append a run entry to manifest.json."""
    data = load_manifest(workspace)
    data.setdefault("tool_version", __version__)
    data.setdefault("runs", [])
    data["runs"].append(
        {
            "subcommand": subcommand,
            "args": _redact_recursive(args),
            "started_at": started_at,
            "finished_at": finished_at,
            "inputs": _redact_recursive(inputs or []),
            "outputs": _redact_recursive(outputs or []),
        }
    )
    write_manifest(workspace, data)


def verify_hashes(workspace: str) -> list[str]:
    """Re-hash each recorded path against its **most recent** recorded digest
    and return mismatches as human-readable strings. Empty list = all OK.

    Only the newest entry per path is checked. `record_run` appends, so a
    second `consolidate` leaves the first run's now-superseded `docs.md` digest
    in the manifest; verifying every historical entry made `validate` report
    `hash mismatch: docs.md` for a workspace whose only sin was being
    regenerated — once per superseded run. The manifest is still a complete
    append-only audit trail; this is only about which entry claims to describe
    the file currently on disk.
    """
    data = load_manifest(workspace)
    latest: dict[str, str] = {}
    for run in data.get("runs", []):
        for kind in ("inputs", "outputs"):
            for entry in run.get(kind, []):
                rel = entry.get("path")
                want = entry.get("sha256")
                if rel and want:
                    latest[rel] = want

    failures: list[str] = []
    for rel, want in latest.items():
        full = rel if os.path.isabs(rel) else os.path.join(workspace, rel)
        if not os.path.exists(full):
            failures.append(f"missing: {rel}")
            continue
        got = sha256_file(full)
        if got != want:
            failures.append(f"hash mismatch: {rel} (want {want[:8]}, got {got[:8]})")
    return failures


def superseded_mismatches(workspace: str) -> list[tuple[str, str]]:
    """Return `(path, message)` for every recorded path whose *older* digests no
    longer describe the file on disk. Empty list = nothing superseded.

    `verify_hashes` checks only the newest entry per path, which is what stops a
    second `consolidate` from failing `validate`. The cost is that tamper
    detection became "the file matches the newest claim" rather than "the file
    matches every claim ever made" — editing a file and appending a run entry
    recording the new digest now verifies clean. This function re-exposes the
    difference the newest-wins rule hides, so the `_manifest` docstring's
    "complete append-only audit trail" is checkable rather than merely asserted.

    It is deliberately advisory: two runs of `consolidate` over a changed spec
    legitimately record two different `docs.md` digests, and that is the common
    case, not an attack. Callers must not let it move a verdict.

    Silent in the two cases another check already owns: the path is missing, or
    the *newest* digest itself mismatches (both are `verify_hashes` failures).
    """
    data = load_manifest(workspace)
    history: dict[str, list[tuple[str, str]]] = {}
    for run in data.get("runs", []):
        subcommand = run.get("subcommand") or "an earlier run"
        for kind in ("inputs", "outputs"):
            for entry in run.get(kind, []):
                rel = entry.get("path")
                want = entry.get("sha256")
                if rel and want:
                    history.setdefault(rel, []).append((want, subcommand))

    findings: list[tuple[str, str]] = []
    for rel, entries in history.items():
        full = rel if os.path.isabs(rel) else os.path.join(workspace, rel)
        if not os.path.exists(full):
            continue
        got = sha256_file(full)
        if got != entries[-1][0]:
            continue
        stale = [(sha, sub) for sha, sub in entries[:-1] if sha != got]
        if not stale:
            continue
        subs = ", ".join(sorted({sub for _sha, sub in stale}))
        plural = "run" if len(stale) == 1 else "runs"
        findings.append(
            (
                rel,
                f"superseded digest: {rel} — {len(stale)} earlier {plural} ({subs}) "
                f"recorded a different digest. This is the normal result of "
                f"re-running {subs} over changed input; it is worth investigating "
                f"only if no such re-run happened.",
            )
        )
    return findings


def file_entry(workspace: str, path: str) -> dict[str, Any]:
    """Build an `{path, sha256}` entry for a file inside the workspace.

    `path` may be absolute, workspace-relative, or cwd-relative — callers pass
    all three. Resolving both sides against the cwd first is what keeps them
    equivalent: the old form joined `workspace` onto any non-absolute path, so
    `consolidate myws` (a relative workspace, and `docs_path` therefore also
    relative) looked for `myws/myws/docs.md` and crashed with FileNotFoundError
    after docs.md had already been written.
    """
    abs_workspace = os.path.abspath(workspace)
    full = os.path.abspath(path)
    if not os.path.exists(full):
        # Not cwd-relative — try it as workspace-relative before giving up.
        full = os.path.abspath(os.path.join(abs_workspace, path))
    return {"path": os.path.relpath(full, abs_workspace), "sha256": sha256_file(full)}
