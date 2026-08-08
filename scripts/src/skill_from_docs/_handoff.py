"""The `handoff.json` packet — split out of `cmd_consolidate`.

`cmd_consolidate` is four layers (load / spec-walk / render / handoff) and this
is the last of them. The seam is real: the handoff layer touches the render
layer through exactly one value, `docs_md_text: str`. Keep it that way — if a
second value ever has to cross, the split is wrong, not the parameter list.

`CANONICAL_SECTIONS` lives here rather than next to the renderer because both
the renderer and `_derive_coverage_checklist` read it, and `cmd_consolidate`
imports this module, never the reverse. Two copies of the list used to exist and
they disagreed on how to spell "Rate limits, quotas, versioning" — the heading
said one thing, the checklist matched another.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, NamedTuple

from . import __version__
from ._schema import HANDOFF_VERSION, lint_handoff
from ._spec import json_pointer

if TYPE_CHECKING:  # pragma: no cover - annotations only, no runtime import cycle
    from .cmd_consolidate import ProbeIndex, WalkedSpec


class Section(NamedTuple):
    """One canonical docs.md H2.

    `name` is what `handoff.coverage_checklist` calls the section; `heading` is
    the text after `## ` in docs.md. They differ for exactly one section, which
    is why this pair exists rather than a flat list of strings.
    """

    name: str
    heading: str


CANONICAL_SECTIONS: tuple[Section, ...] = (
    Section("Installation", "Installation"),
    Section("Authentication", "Authentication"),
    Section("Core concepts", "Core concepts"),
    Section("API reference", "API reference"),
    Section("Minimal working example", "Minimal working example"),
    Section("Errors", "Errors"),
    Section("Rate limits", "Rate limits, quotas, versioning"),
    Section("Gotchas", "Gotchas"),
)


def _collect_auth_method_signals(probes: ProbeIndex) -> tuple[str | None, list[str]]:
    """Find the auth-discovery probe (if any) and lift its auth_method +
    security_warnings out of the fixture manifest so they can flow into
    handoff.content_shape_signals. Returns (auth_method, warnings).

    skill-creator reads auth_method (`bearer` | `auth_token_header` |
    `api_key_header` | `basic` | `query_string`) to decide what the generated
    integration skill must warn users about and how it loads credentials.
    """
    for fixture, _name in probes:
        if fixture.scope != "auth-discovery":
            continue
        method = fixture.manifest.auth_method
        if method is None:
            continue
        return method, list(fixture.manifest.security_warnings)
    return None, []


def build_handoff(
    workspace: str,
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    probes: ProbeIndex,
    retrieved: str,
    docs_md_text: str,
    *,
    walked: WalkedSpec,
) -> dict[str, Any]:
    info = (spec or {}).get("info", {})
    title = info.get("title", "tool")
    proposed_name = f"{title.lower().replace(' ', '-')}-integration"

    spec_url = (source_map or {}).get("spec_url")
    spec_format = (source_map or {}).get("format")

    provenance_index: dict[str, Any] = {}
    if spec:
        for tag, ops in walked.by_tag.items():
            # tag-level entry for the H3 section in docs.md
            tag_section_key = f"API reference > Tag: {tag}"
            provenance_index.setdefault(tag_section_key, {"sources": [], "probes": []})
            provenance_index[tag_section_key]["sources"].append(
                {
                    "type": "spec",
                    "url": spec_url,
                    "pointer": f"/tags/{tag}",
                    "raw_file": "raw/spec.json",
                }
            )
            for path, method, _op in ops:
                section_key = f"API reference > Tag: {tag} > {method} {path}"
                provenance_index.setdefault(section_key, {"sources": [], "probes": []})
                pointer = json_pointer(path, method)
                provenance_index[section_key]["sources"].append(
                    {
                        "type": "spec",
                        "url": spec_url,
                        "pointer": pointer,
                        "raw_file": "raw/spec.json",
                    }
                )
                for probe, filename in probes.for_path(path):
                    provenance_index[section_key]["probes"].append(
                        {
                            "method": probe.request.method,
                            "url": probe.request.url,
                            "status": probe.response.status,
                            "scope": probe.scope,
                            "fixture": f"probes/{filename}",
                        }
                    )

    # H9: populate coverage_checklist from sections actually present in docs.md.
    coverage_checklist = _derive_coverage_checklist(docs_md_text, spec_url)

    # H9: derive 3-5 suggested test cases from spec tags + endpoint summaries.
    suggested_test_cases = _derive_test_cases(spec, title)

    # H9: pull user_declared_scope from manifest if a prior probe run recorded
    # a --scope. Otherwise leave empty (harvest agent fills it in).
    declared_scope, declared_languages = _read_user_declarations(workspace, spec)

    # Lift auth_method + security_warnings from any auth-discovery probe so
    # skill-creator can read them from handoff.content_shape_signals and
    # decide what the generated integration skill must warn users about.
    auth_method, auth_security_warnings = _collect_auth_method_signals(probes)

    content_shape_signals: dict[str, Any] = {
        "has_openapi_spec": bool(spec),
        "spec_url": spec_url,
        "spec_format": spec_format,
        "endpoint_count": walked.endpoint_count,
        "tag_count": walked.tag_count,
    }
    if auth_method is not None:
        content_shape_signals["auth_method"] = auth_method
        content_shape_signals["security_warnings"] = auth_security_warnings

    handoff = {
        "version": HANDOFF_VERSION,
        "proposed_name": proposed_name,
        "tool_summary": info.get("description", "")[:1024],
        "user_declared_scope": declared_scope,
        "user_declared_languages": declared_languages,
        "archetype_primary": 4 if spec else None,
        "content_shape_signals": content_shape_signals,
        "coverage_checklist": coverage_checklist,
        "gap_list": [],
        "provenance_index": provenance_index,
        "image_inventory": [],
        "suggested_test_cases": suggested_test_cases,
        "harvest_metadata": {
            "retrieved_date": retrieved[:10],
            "tool_version": __version__,
            "raw_page_count": _count_raw_files(workspace),
            "docs_md_token_count": len(docs_md_text.split()),
        },
    }
    # Detect TODO markers to add to gap_list.
    for line_num, line in enumerate(docs_md_text.splitlines(), start=1):
        if "<!-- TODO" in line:
            handoff["gap_list"].append({"line": line_num, "text": line.strip()})

    # Assert the packet we emit satisfies its own contract. skill-creator reads
    # this file in another process; a shape error should fail here, loudly,
    # rather than surface downstream as a confusing interview.
    problems = lint_handoff(handoff)
    if problems:  # pragma: no cover - guards against future edits to this dict
        raise AssertionError(
            "internal error: emitted handoff.json violates its own contract: "
            + "; ".join(problems)
        )
    return handoff


def _derive_coverage_checklist(
    docs_md_text: str, spec_url: str | None
) -> list[dict[str, Any]]:
    """H9: walk docs.md and decide coverage status for each canonical section.
    A section is `covered` if it has a non-empty body (not just
    `_Not documented upstream._`), `partial` if it has a `<!-- TODO -->`
    marker, and `missing` otherwise.

    This deliberately re-parses the markdown the caller just rendered rather
    than reading the structured `WalkedSpec`. The checklist is a statement about
    what *actually got written*, not about what should have been: a renderer bug
    that drops a section has to show up here as `missing`, and it only can if
    this reads the artifact. Feeding it structured data would make it agree with
    the renderer by construction and stop being a check.
    """
    lines = docs_md_text.splitlines()
    out: list[dict[str, Any]] = []
    for section in CANONICAL_SECTIONS:
        status = "missing"
        # Find the heading; capture the body lines until the next H2.
        marker = f"## {section.heading}"
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(marker)), None)
        if idx is not None:
            body = []
            j = idx + 1
            while j < len(lines) and not lines[j].startswith("## "):
                body.append(lines[j])
                j += 1
            body_text = "\n".join(body).strip()
            if not body_text or body_text == "_Not documented upstream._":
                status = "missing"
            elif "<!-- TODO" in body_text:
                status = "partial"
            else:
                status = "covered"
        sources = [spec_url] if spec_url else []
        out.append({"name": section.name, "status": status, "sources": sources})
    return out


def _derive_test_cases(
    spec: dict[str, Any] | None, title: str
) -> list[dict[str, Any]]:
    """H9: derive 3-5 trigger-phrase test cases from the spec. We surface the
    first three endpoint summaries as natural-language phrases plus two
    catch-all phrases (`use {title}`, `integrate with {title}`).
    """
    cases: list[dict[str, Any]] = []
    if spec:
        paths = spec.get("paths") or {}
        seen = 0
        for path, methods in paths.items():
            if seen >= 3:
                break
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if not isinstance(op, dict):
                    continue
                summary = op.get("summary") or f"{method.upper()} {path}"
                cases.append(
                    {
                        "trigger_phrase": f"{summary} via {title}",
                        "endpoint": f"{method.upper()} {path}",
                        "status": "suggestion",
                    }
                )
                seen += 1
                break
    # Always pad with two catch-all phrases so the list is in the 3-5 range.
    cases.append(
        {
            "trigger_phrase": f"use {title}",
            "endpoint": None,
            "status": "suggestion",
        }
    )
    cases.append(
        {
            "trigger_phrase": f"integrate with {title}",
            "endpoint": None,
            "status": "suggestion",
        }
    )
    # Cap at 5
    return cases[:5]


def _read_user_declarations(
    workspace: str, spec: dict[str, Any] | None
) -> tuple[str, list[str]]:
    """H9: peek at manifest.json for a `--scope` arg from a previous run; peek
    at `info.x-language` for spec-declared SDK languages. Both default to
    empty (harvest agent fills them in)."""
    declared_scope = ""
    declared_languages: list[str] = []
    try:
        from ._manifest import load_manifest

        data = load_manifest(workspace)
        for run in data.get("runs", []):
            args = run.get("args") or {}
            scope = args.get("scope")
            if isinstance(scope, str) and scope and scope != "ad-hoc":
                declared_scope = scope
                break
    except Exception:
        pass
    if spec is not None:
        x_lang = (spec.get("info") or {}).get("x-language")
        if isinstance(x_lang, list):
            declared_languages = [str(x) for x in x_lang]
        elif isinstance(x_lang, str):
            declared_languages = [x_lang]
    return declared_scope, declared_languages


def _count_raw_files(workspace: str) -> int:
    raw = os.path.join(workspace, "raw")
    if not os.path.isdir(raw):
        return 0
    return sum(1 for _ in os.listdir(raw))
