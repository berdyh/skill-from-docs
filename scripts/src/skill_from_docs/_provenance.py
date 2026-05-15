"""Provenance comment parser + emitter — single source of truth.

Two shapes exist in the skill's docs:

  Doc/spec source:
    <!-- source: URL retrieved: DATE raw_file: PATH spec-pointer: POINTER mirror: unofficial -->

  Probe source:
    <!-- probe: METHOD URL status: NNN retrieved: DATE scope: LABEL fixture: PATH -->

Both are key/value pairs after the first token. Round-trippable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_COMMENT_RE = re.compile(r"<!--\s*(.*?)\s*-->", re.DOTALL)


@dataclass
class ProvenanceEntry:
    kind: str  # "source" or "probe"
    fields: dict[str, str] = field(default_factory=dict)

    def emit(self) -> str:
        """Render as an HTML comment string."""
        if self.kind == "source":
            head_key = "source"
        elif self.kind == "probe":
            head_key = "probe"
        else:
            head_key = self.kind
        head_val = self.fields.get(head_key, "")
        parts = [f"{head_key}: {head_val}"] if head_val else [head_key]
        for k, v in self.fields.items():
            if k == head_key:
                continue
            parts.append(f"{k}: {v}")
        return "<!-- " + " ".join(parts) + " -->"


def parse_comment(text: str) -> ProvenanceEntry | None:
    """Parse a single HTML comment string. Returns None if not a provenance
    comment.
    """
    m = _COMMENT_RE.search(text.strip())
    if not m:
        # Try parsing as raw inner content.
        inner = text.strip()
    else:
        inner = m.group(1)

    inner = " ".join(inner.split())  # normalize whitespace
    if not inner:
        return None

    # First token must be 'source:' or 'probe:'.
    first_colon = inner.find(":")
    if first_colon < 0:
        return None
    head = inner[:first_colon].strip().lower()
    if head not in ("source", "probe"):
        return None

    # Whitelist of known field names. Anything else (e.g. `https:` in a URL)
    # stays as part of the previous field's value. Order doesn't matter; the
    # parser scans the inner text linearly.
    KNOWN_KEYS = {
        "source",
        "probe",
        "retrieved",
        "raw_file",
        "spec-pointer",
        "mirror",
        "status",
        "scope",
        "fixture",
        "method",
    }

    fields: dict[str, str] = {}
    # Find positions of known KEY: tokens at start-of-string or after whitespace.
    token_re = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_-]*):")
    matches = [
        m for m in token_re.finditer(inner) if m.group(1).lower() in KNOWN_KEYS
    ]
    for i, m2 in enumerate(matches):
        key = m2.group(1).lower()
        val_start = m2.end()
        val_end = matches[i + 1].start() if i + 1 < len(matches) else len(inner)
        value = inner[val_start:val_end].strip()
        fields[key] = value

    if head not in fields:
        return None
    return ProvenanceEntry(kind=head, fields=fields)


def emit_source(
    url: str,
    *,
    retrieved: str,
    raw_file: str | None = None,
    spec_pointer: str | None = None,
    mirror: str | None = None,
) -> str:
    fields = {"source": url, "retrieved": retrieved}
    if raw_file:
        fields["raw_file"] = raw_file
    if spec_pointer:
        fields["spec-pointer"] = spec_pointer
    if mirror:
        fields["mirror"] = mirror
    return ProvenanceEntry(kind="source", fields=fields).emit()


def emit_probe(
    method: str,
    url: str,
    *,
    status: int,
    retrieved: str,
    scope: str,
    fixture: str | None = None,
) -> str:
    fields = {
        "probe": f"{method} {url}",
        "status": str(status),
        "retrieved": retrieved,
        "scope": scope,
    }
    if fixture:
        fields["fixture"] = fixture
    return ProvenanceEntry(kind="probe", fields=fields).emit()


def find_all_provenance(text: str) -> list[ProvenanceEntry]:
    """Return every provenance comment in a body of markdown."""
    out: list[ProvenanceEntry] = []
    for m in _COMMENT_RE.finditer(text):
        entry = parse_comment(m.group(0))
        if entry is not None:
            out.append(entry)
    return out
