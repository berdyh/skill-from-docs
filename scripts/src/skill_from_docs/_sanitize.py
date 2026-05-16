"""Prompt-injection guard.

Sanitizes spec-derived `description` strings and community-source narrative
content before it lands in `docs.md`. The goal is to neutralize three kinds of
injection vectors:

1. Fake provenance comments — escape `<!--` and `-->` so attacker-supplied
   text cannot impersonate a `<!-- source: ... -->` footer.
2. Fake markdown headings — escape line-leading `#` so attacker text cannot
   create a section break and confuse a downstream parser.
3. Agent-instruction patterns — detect strings like "you are an assistant",
   "ignore previous instructions", `<system>` tags. We strip them and log a
   stderr warning naming the source pointer.

Bypass via `--no-sanitize-descriptions` (documented as a security risk).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass


# Order matters: longer match first.
_AGENT_PATTERNS = [
    re.compile(r"(?i)ignore\s+(previous|prior|all)\s+instructions"),
    re.compile(r"(?i)you\s+are\s+(an?\s+)?(assistant|ai|model|claude|gpt|chatbot)"),
    re.compile(r"(?i)disregard\s+(previous|prior|all)\s+instructions"),
    re.compile(r"</?system>", re.IGNORECASE),
    re.compile(r"</?role>", re.IGNORECASE),
    re.compile(r"</?instructions>", re.IGNORECASE),
]

# Leading-# at start of line. Use a function-based replacement to keep indent.
_LINE_HEADING_RE = re.compile(r"^(#)", re.MULTILINE)


@dataclass
class SanitizeResult:
    text: str
    detections: list[str]


def sanitize_text_for_markdown(text: str, *, source_pointer: str | None = None) -> str:
    """H8: apply the same escape rules regardless of source field name. Used
    by `consolidate` when emitting heading content (tag names, paths,
    operation IDs) sourced from raw spec strings.

    For inline use inside a heading or table cell, newlines and carriage
    returns are flattened to spaces so attacker-controlled text cannot break
    out of the heading line into a sibling block.
    """
    if not isinstance(text, str):
        return text
    cleaned = sanitize_text(text, source_pointer=source_pointer).text
    # Flatten line breaks so embedded text can't escape a single-line context
    # (heading, table cell).
    cleaned = cleaned.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return cleaned


def sanitize_text(text: str, *, source_pointer: str | None = None) -> SanitizeResult:
    """Apply the full prompt-injection guard to a string.

    Returns the sanitized text plus a list of human-readable detections.
    """
    if not isinstance(text, str):
        return SanitizeResult(text=text, detections=[])

    detections: list[str] = []
    out = text

    # 1. Escape provenance-comment markers.
    if "<!--" in out:
        out = out.replace("<!--", "<!- -")
    if "-->" in out:
        out = out.replace("-->", "- ->")

    # 2. Escape line-leading `#`.
    out = _LINE_HEADING_RE.sub(r"\\\1", out)

    # 3. Detect & strip agent-instruction patterns.
    for pat in _AGENT_PATTERNS:
        if pat.search(out):
            detections.append(pat.pattern)
            out = pat.sub("[stripped]", out)

    if detections and source_pointer is not None:
        msg = (
            f"sanitize: stripped agent-instruction patterns at {source_pointer}: "
            + ", ".join(detections)
        )
        print(msg, file=sys.stderr)

    return SanitizeResult(text=out, detections=detections)


def sanitize_spec_descriptions(
    spec: dict, *, source_pointer: str = "<spec>"
) -> tuple[dict, list[str]]:
    """Walk a spec dict and sanitize every string field named 'description'
    or 'summary' or 'title'. Returns the (possibly-mutated) spec and a list
    of source pointers where detections occurred.
    """
    detections: list[str] = []
    targets = {"description", "summary", "title"}

    def _walk(node, path: str):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                child = f"{path}/{k}"
                if isinstance(v, str) and k in targets:
                    result = sanitize_text(v, source_pointer=child)
                    if result.detections:
                        detections.append(child)
                    node[k] = result.text
                else:
                    _walk(v, child)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}/{i}")

    _walk(spec, source_pointer)
    return spec, detections
