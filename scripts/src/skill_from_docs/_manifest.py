"""Workspace `manifest.json` — read, write, append-run, hash-verify."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import __version__
from ._redaction import redact_url


MANIFEST_FILENAME = "manifest.json"


def _redact_recursive(value: Any) -> Any:
    """Walk a JSON-like value and apply redact_url to any string that looks
    like an http(s) URL with sensitive query params. Used so manifest entries
    never persist raw credential-bearing URLs to disk. (B1)
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
    os.makedirs(workspace, exist_ok=True)
    path = manifest_path(workspace)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


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
    """Re-hash every recorded input/output path and return a list of
    mismatches as human-readable strings. Empty list = all OK.
    """
    data = load_manifest(workspace)
    failures: list[str] = []
    for run in data.get("runs", []):
        for kind in ("inputs", "outputs"):
            for entry in run.get(kind, []):
                rel = entry.get("path")
                want = entry.get("sha256")
                if not rel or not want:
                    continue
                full = rel if os.path.isabs(rel) else os.path.join(workspace, rel)
                if not os.path.exists(full):
                    failures.append(f"missing: {rel}")
                    continue
                got = sha256_file(full)
                if got != want:
                    failures.append(f"hash mismatch: {rel} (want {want[:8]}, got {got[:8]})")
    return failures


def file_entry(workspace: str, path: str) -> dict[str, Any]:
    """Build an `{path, sha256}` entry for a file inside the workspace."""
    full = path if os.path.isabs(path) else os.path.join(workspace, path)
    rel = os.path.relpath(full, workspace) if os.path.isabs(path) else path
    return {"path": rel, "sha256": sha256_file(full)}
