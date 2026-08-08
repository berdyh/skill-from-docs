"""Atomic workspace writes.

Every artifact this package produces goes through `write_text` (or `write_json`,
which is `write_text` over `json.dumps`). The write is `tmp + os.replace`, so a
reader either sees the previous complete file or the new complete file, never a
truncation.

That matters most for `manifest.json`, which `_manifest.record_run` reads,
mutates and writes back on every subcommand: an interrupt part-way through the
old in-place write left a truncated audit trail, and `validate`'s `verify_hashes`
then reported the whole workspace corrupt. `docs.md` is the same shape — it is
hash-attested by a manifest entry written *after* it, so a half-written `docs.md`
fails verification against the previous run's digest.

Two rules the callers depend on:

- **The temp file is created in the target's own directory.** `os.replace` is
  atomic only within a filesystem; a temp file under `/tmp` would silently
  degrade to a copy across a mount boundary, which is exactly the non-atomic
  write this module exists to remove.
- **`mode` is applied to the temp file before the replace, not after.** A fresh
  temp file gets umask permissions, so `raw/source-map.json` — the one artifact
  holding an un-redacted, possibly credential-bearing URL, created `0o600` on
  purpose — would be silently widened to `0o644` by a naive tmp+replace, and
  would sit world-readable in the window between `replace` and a later `chmod`.
  `_schema.write_source_map` passes `mode=SOURCE_MAP_MODE`; see the module note
  there for why that file is special.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def write_text(path: str, text: str, *, mode: int | None = None) -> None:
    """Atomically write `text` to `path` as UTF-8.

    `mode` is the permission bits the finished file must have. Leave it None to
    reproduce what `open(path, "w")` would have produced: umask-derived
    permissions for a new file, and the *existing* permissions when `path`
    already exists — a plain `open` of an existing file does not touch its mode,
    and neither does this, so an operator who tightened a workspace file does
    not get it widened back on the next run.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = None  # new file: let umask decide, as `open(path, "w")` does

    # mkstemp in the target's own directory keeps os.replace atomic; it also
    # creates 0o600, so the content is never readable by anyone else while it
    # is being written, whatever the final mode turns out to be.
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent)
    try:
        if mode is None:
            # Reproduce `open(path, "w")` on a new file: 0o666 masked by umask.
            # os.umask has no read-only form; setting and restoring is safe here
            # because this is a single-threaded CLI.
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:  # not available on Windows
            fchmod(fd, mode)
        else:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave the previous file — if any — exactly as it was, and do not leave
        # a half-written temp behind. BaseException so a KeyboardInterrupt in
        # the middle of a large write still cleans up.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, data: Any, *, mode: int | None = None) -> None:
    """Atomically write `data` as pretty-printed JSON with a trailing newline.

    `indent=2` plus the final newline is the on-disk shape every JSON artifact in
    a workspace already had; keep it, because `quick-diff` re-hashes
    `raw/spec.json` and compares against the digest `fetch` recorded (DEFERRED.md
    failure mode 5 — two layers must agree on which bytes).

    Serialization happens before the file is touched, so a value JSON cannot
    encode raises without disturbing what is already on disk.
    """
    write_text(path, json.dumps(data, indent=2) + "\n", mode=mode)
