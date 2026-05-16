"""openapi-harvest consolidate — emit docs.md and handoff.json from a workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

from . import __version__
from ._manifest import file_entry, now_iso, record_run, sha256_file
from ._provenance import emit_probe, emit_source
from ._sanitize import sanitize_spec_descriptions, sanitize_text, sanitize_text_for_markdown
from ._schema import ProbeFixture


CANONICAL_H2 = [
    "Coverage status",
    "Installation",
    "Authentication",
    "Core concepts",
    "API reference",
    "Minimal working example",
    "Errors",
    "Rate limits, quotas, versioning",
    "Gotchas",
]


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "consolidate",
        help="emit docs.md + handoff.json",
        description="Walk a workspace and assemble docs.md (canonical H2s, per-tag H3s) plus handoff.json.",
    )
    p.add_argument("workspace", nargs="?")
    p.add_argument("--merge-probes", action="store_true")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--narrative-dir")
    p.add_argument("--emit-handoff", action="store_true", default=True)
    p.add_argument("--no-emit-handoff", dest="emit_handoff", action="store_false")
    p.add_argument("--no-sanitize-descriptions", dest="sanitize", action="store_false", default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=run)


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_spec(workspace: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    spec_path = os.path.join(workspace, "raw", "spec.json")
    map_path = os.path.join(workspace, "raw", "source-map.json")
    if not os.path.exists(spec_path):
        return None, None, None
    spec = _read_json(spec_path)
    source_map = _read_json(map_path) if os.path.exists(map_path) else {}
    return spec, source_map, spec_path


def _load_probes(workspace: str) -> list[tuple[ProbeFixture, str]]:
    """Return [(fixture, filename), ...] from the workspace probes/ directory."""
    probes_dir = os.path.join(workspace, "probes")
    if not os.path.isdir(probes_dir):
        return []
    out: list[tuple[ProbeFixture, str]] = []
    for name in sorted(os.listdir(probes_dir)):
        if not name.endswith(".json"):
            continue
        try:
            data = _read_json(os.path.join(probes_dir, name))
            out.append((ProbeFixture.from_dict(data), name))
        except Exception:
            continue
    return out


def _load_narratives(workspace: str, narrative_dir: str | None) -> dict[str, str]:
    d = narrative_dir or os.path.join(workspace, "narrative")
    if not os.path.isdir(d):
        return {}
    out: dict[str, str] = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        key = os.path.splitext(name)[0]
        out[key] = _read_text(os.path.join(d, name))
    return out


def _group_ops_by_tag(spec: dict[str, Any]) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return by_tag
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in (
                "get", "post", "put", "delete", "patch", "head", "options"
            ):
                continue
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or ["_untagged"]
            for tag in tags:
                by_tag[tag].append((path, method.upper(), op))
    return by_tag


def _filter_tags(by_tag: dict, allowed: list[str]) -> dict:
    if not allowed:
        return by_tag
    return {k: v for k, v in by_tag.items() if k in allowed}


def _spec_pointer(path: str, method: str) -> str:
    escaped = path.replace("~", "~0").replace("/", "~1")
    if path.startswith("/"):
        escaped = f"~1{escaped[2:]}" if escaped.startswith("~1") else escaped
    return f"/paths/{escaped}/{method.lower()}"


def _endpoint_block(
    path: str,
    method: str,
    op: dict[str, Any],
    *,
    spec_url: str | None,
    retrieved: str,
    raw_file: str,
    probes_for_endpoint: list[tuple[ProbeFixture, str]],
) -> list[str]:
    # H8: sanitize path before embedding in a heading so `<!--` / leading `#`
    # / `\n` in attacker-controlled path strings can't break out.
    safe_path = sanitize_text_for_markdown(path, source_pointer=f"paths/{path}")
    safe_method = sanitize_text_for_markdown(
        method, source_pointer=f"paths/{path}/method"
    )
    lines: list[str] = [f"#### `{safe_method} {safe_path}`", ""]
    summary = op.get("summary") or ""
    description = op.get("description") or ""
    if summary:
        # Inline use → flatten newlines.
        lines.append(
            f"**{sanitize_text_for_markdown(summary, source_pointer=f'paths/{path}/summary')}**"
        )
        lines.append("")
    if description:
        # Block use — sanitize_text already escapes injection markers.
        lines.append(sanitize_text(description, source_pointer=f"paths/{path}/description").text)
        lines.append("")
    params = op.get("parameters") or []
    if params:
        lines.append("**Parameters:**")
        for p in params:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "?")
            loc = p.get("in", "?")
            required = " (required)" if p.get("required") else ""
            desc = p.get("description", "")
            lines.append(f"- `{name}` ({loc}){required} — {desc}")
        lines.append("")

    # Responses summary
    responses = op.get("responses") or {}
    if responses:
        lines.append("**Responses:**")
        for code, body in responses.items():
            if not isinstance(body, dict):
                continue
            d = body.get("description", "")
            lines.append(f"- `{code}` — {d}")
        lines.append("")

    pointer = _spec_pointer(path, method)
    if spec_url:
        lines.append(
            emit_source(spec_url, retrieved=retrieved, raw_file=raw_file, spec_pointer=pointer)
        )
    else:
        lines.append(
            emit_source("(local spec)", retrieved=retrieved, raw_file=raw_file, spec_pointer=pointer)
        )

    for probe, filename in probes_for_endpoint:
        lines.append(
            emit_probe(
                probe.request.method,
                probe.request.url,
                status=probe.response.status,
                retrieved=probe.manifest.captured_at or retrieved,
                scope=probe.scope,
                fixture=f"probes/{filename}",
            )
        )
    lines.append("")
    return lines


def _match_probe(probe: ProbeFixture, path: str) -> bool:
    """Match by path suffix."""
    from urllib.parse import urlparse as _u
    pp = _u(probe.request.url).path
    return pp == path or pp.endswith(path)


def _section_or_default(narratives: dict[str, str], key: str, default: str) -> str:
    return narratives.get(key, default)


def _emit_narrative_section(
    lines: list[str],
    narratives: dict[str, str],
    key: str,
    filename: str,
    retrieved: str,
) -> None:
    """H6: write a section body sourced from a narrative file, emitting a
    `<!-- source: narrative file: ... -->` provenance comment so `validate`
    accepts the section. Falls back to `_Not documented upstream._` when no
    narrative exists for the section.
    """
    body = narratives.get(key)
    if body:
        lines.append(body)
        lines.append("")
        lines.append(
            emit_source(
                "(narrative)",
                retrieved=retrieved,
                raw_file=f"narrative/{filename}",
            )
        )
    else:
        lines.append("_Not documented upstream._")


def _build_docs_md(
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    spec_path: str | None,
    probes: list[tuple[ProbeFixture, str]],
    narratives: dict[str, str],
    *,
    tags_filter: list[str],
    merge_probes: bool,
    retrieved: str,
    warnings: list[str],
) -> str:
    title = (spec or {}).get("info", {}).get("title", "API")
    version = (spec or {}).get("info", {}).get("version", "?")
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- version: {version}")
    lines.append(f"- retrieved: {retrieved}")
    if source_map and source_map.get("spec_url"):
        lines.append(f"- spec_url: {source_map['spec_url']}")
    lines.append("")

    # Coverage status
    lines.append("## Coverage status")
    lines.append("")
    lines.append("- [x] OpenAPI spec parsed" if spec else "- [ ] OpenAPI spec not loaded")
    lines.append(
        "- [x] Probes merged" if merge_probes and probes else "- [ ] Probes not merged"
    )
    lines.append("")

    # Installation
    lines.append("## Installation")
    lines.append("")
    _emit_narrative_section(lines, narratives, "installation", "installation.md", retrieved)
    lines.append("")

    # Authentication
    lines.append("## Authentication")
    lines.append("")
    auth_body = narratives.get("authentication")
    spec_url_for_auth = (source_map or {}).get("spec_url")
    if auth_body:
        lines.append(auth_body)
        lines.append("")
        lines.append(
            emit_source(
                spec_url_for_auth or "(narrative)",
                retrieved=retrieved,
                raw_file="narrative/authentication.md",
            )
        )
    else:
        sec = (spec or {}).get("components", {}).get("securitySchemes", {})
        if sec:
            for name, scheme in sec.items():
                if not isinstance(scheme, dict):
                    continue
                lines.append(f"- `{name}`: type=`{scheme.get('type')}` scheme=`{scheme.get('scheme')}`")
            lines.append("")
            lines.append(
                emit_source(
                    spec_url_for_auth or "(local spec)",
                    retrieved=retrieved,
                    raw_file="raw/spec.json",
                    spec_pointer="/components/securitySchemes",
                )
            )
        else:
            lines.append("_Not documented upstream._")
    lines.append("")

    # Core concepts
    lines.append("## Core concepts")
    lines.append("")
    _emit_narrative_section(lines, narratives, "core-concepts", "core-concepts.md", retrieved)
    lines.append("")

    # API reference
    lines.append("## API reference")
    lines.append("")
    if spec:
        spec_url = (source_map or {}).get("spec_url")
        spec_raw_rel = (
            os.path.relpath(spec_path, os.path.dirname(os.path.dirname(spec_path)))
            if spec_path
            else "raw/spec.json"
        )
        by_tag = _group_ops_by_tag(spec)
        by_tag = _filter_tags(by_tag, tags_filter)

        # Detect probes outside the filter
        if tags_filter:
            for probe, _filename in probes:
                hits = 0
                for tag, ops in by_tag.items():
                    if any(_match_probe(probe, path) for path, _m, _op in ops):
                        hits += 1
                if hits == 0:
                    warnings.append(
                        f"probe {probe.request.method} {probe.request.url} references endpoint outside --tag filter"
                    )

        if not by_tag:
            lines.append("_No endpoints match the filter._")
            lines.append("")
        for tag in sorted(by_tag.keys()):
            # H8: sanitize tag name before emitting as a heading AND as part
            # of the spec pointer in the provenance comment. A `\n` in the tag
            # name would otherwise split the comment across lines.
            safe_tag = sanitize_text_for_markdown(
                tag, source_pointer=f"tags/{tag}"
            )
            # For pointer use: keep alphanumerics + dash/underscore; replace
            # anything else with `_`. This keeps the pointer renderable in
            # a single HTML comment.
            pointer_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_tag)[:80]
            lines.append(f"### Tag: {safe_tag}")
            lines.append("")
            # H3 tag-level provenance points back to the spec root
            lines.append(
                emit_source(
                    spec_url or "(local spec)",
                    retrieved=retrieved,
                    raw_file=spec_raw_rel,
                    spec_pointer=f"/tags/{pointer_tag}",
                )
            )
            lines.append("")
            ops = sorted(by_tag[tag], key=lambda t: (t[0], t[1]))
            tagged_probes = (
                [(p, fn) for p, fn in probes if any(_match_probe(p, op_path) for op_path, _, _ in ops)]
                if merge_probes
                else []
            )
            for path, method, op in ops:
                if merge_probes:
                    relevant = [(p, fn) for p, fn in probes if _match_probe(p, path)]
                else:
                    relevant = []
                lines.extend(
                    _endpoint_block(
                        path,
                        method,
                        op,
                        spec_url=spec_url,
                        retrieved=retrieved,
                        raw_file=spec_raw_rel,
                        probes_for_endpoint=relevant,
                    )
                )
            if not tagged_probes and merge_probes:
                lines.append(f"<!-- TODO: no probe captured for tag {tag} -->")
                lines.append("")
        # Detect probes for endpoints not in spec at all
        if merge_probes:
            for probe, _filename in probes:
                any_match = False
                for tag, ops in by_tag.items():
                    if any(_match_probe(probe, path) for path, _m, _op in ops):
                        any_match = True
                        break
                if not any_match and not tags_filter:
                    warnings.append(
                        f"probe {probe.request.method} {probe.request.url} does not match any spec endpoint"
                    )
    else:
        lines.append("_No spec available._")
        lines.append("")

    # Minimal working example
    lines.append("## Minimal working example")
    lines.append("")
    if "example" in narratives:
        lines.append(narratives["example"])
        lines.append("")
        lines.append(
            emit_source(
                "(narrative)",
                retrieved=retrieved,
                raw_file="narrative/example.md",
            )
        )
    else:
        lines.append("_Not documented upstream._")
        lines.append("")
        lines.append("<!-- TODO: provide a minimal working example -->")
    lines.append("")

    # Errors
    lines.append("## Errors")
    lines.append("")
    _emit_narrative_section(lines, narratives, "errors", "errors.md", retrieved)
    lines.append("")

    # Rate limits
    lines.append("## Rate limits, quotas, versioning")
    lines.append("")
    _emit_narrative_section(lines, narratives, "rate-limits", "rate-limits.md", retrieved)
    lines.append("")

    # Gotchas
    lines.append("## Gotchas")
    lines.append("")
    _emit_narrative_section(lines, narratives, "gotchas", "gotchas.md", retrieved)
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_handoff(
    workspace: str,
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    probes: list[tuple[ProbeFixture, str]],
    retrieved: str,
    docs_md_text: str,
    *,
    tag_filter: list[str],
) -> dict[str, Any]:
    info = (spec or {}).get("info", {})
    title = info.get("title", "tool")
    proposed_name = f"{title.lower().replace(' ', '-')}-integration"
    endpoint_count = 0
    tag_count = 0
    if spec:
        paths = spec.get("paths") or {}
        tags_seen: set[str] = set()
        for _path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for m, op in methods.items():
                if m.lower() not in ("get", "post", "put", "delete", "patch", "head", "options"):
                    continue
                endpoint_count += 1
                if isinstance(op, dict):
                    for t in op.get("tags", []):
                        tags_seen.add(t)
        tag_count = len(tags_seen)

    spec_url = (source_map or {}).get("spec_url")
    spec_format = (source_map or {}).get("format")

    provenance_index: dict[str, Any] = {}
    if spec:
        by_tag = _group_ops_by_tag(spec)
        by_tag = _filter_tags(by_tag, tag_filter)
        for tag, ops in by_tag.items():
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
                pointer = _spec_pointer(path, method)
                provenance_index[section_key]["sources"].append(
                    {
                        "type": "spec",
                        "url": spec_url,
                        "pointer": pointer,
                        "raw_file": "raw/spec.json",
                    }
                )
                for probe, filename in probes:
                    if _match_probe(probe, path):
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

    handoff = {
        "version": 1,
        "proposed_name": proposed_name,
        "tool_summary": info.get("description", "")[:1024],
        "user_declared_scope": declared_scope,
        "user_declared_languages": declared_languages,
        "archetype_primary": 4 if spec else None,
        "content_shape_signals": {
            "has_openapi_spec": bool(spec),
            "spec_url": spec_url,
            "spec_format": spec_format,
            "endpoint_count": endpoint_count,
            "tag_count": tag_count,
        },
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
    return handoff


def _derive_coverage_checklist(
    docs_md_text: str, spec_url: str | None
) -> list[dict[str, Any]]:
    """H9: walk docs.md and decide coverage status for each of the 8 canonical
    sections. A section is `covered` if it has a non-empty body (not just
    `_Not documented upstream._`), `partial` if it has a `<!-- TODO -->`
    marker, and `missing` otherwise.
    """
    items_spec = [
        ("Installation", "## Installation"),
        ("Authentication", "## Authentication"),
        ("Core concepts", "## Core concepts"),
        ("API reference", "## API reference"),
        ("Minimal working example", "## Minimal working example"),
        ("Errors", "## Errors"),
        ("Rate limits", "## Rate limits"),
        ("Gotchas", "## Gotchas"),
    ]
    lines = docs_md_text.splitlines()
    out: list[dict[str, Any]] = []
    for name, heading in items_spec:
        status = "missing"
        # Find the heading; capture the body lines until the next H2.
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(heading)), None)
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
        out.append({"name": name, "status": status, "sources": sources})
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


def run(args) -> int:
    workspace = args.workspace or os.getcwd()
    if not os.path.isdir(workspace):
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        return 1

    started = now_iso()
    retrieved = started

    spec, source_map, spec_path = _load_spec(workspace)
    if spec is None:
        print(f"ERROR: no spec at {workspace}/raw/spec.json", file=sys.stderr)
        return 3

    # Sanitize spec descriptions (in-place).
    if args.sanitize:
        spec, detections = sanitize_spec_descriptions(spec)
        if detections and not args.quiet:
            print(
                f"consolidate: sanitized {len(detections)} spec description(s)",
                file=sys.stderr,
            )

    probes = _load_probes(workspace) if args.merge_probes else []
    narratives = _load_narratives(workspace, args.narrative_dir)
    if args.sanitize:
        sanitized_narratives: dict[str, str] = {}
        for k, v in narratives.items():
            result = sanitize_text(v, source_pointer=f"narrative/{k}.md")
            sanitized_narratives[k] = result.text
        narratives = sanitized_narratives

    warnings: list[str] = []
    docs_md = _build_docs_md(
        spec,
        source_map,
        spec_path,
        probes,
        narratives,
        tags_filter=args.tag,
        merge_probes=args.merge_probes,
        retrieved=retrieved,
        warnings=warnings,
    )

    if args.dry_run:
        print("=== docs.md ===")
        print(docs_md)
        if args.emit_handoff:
            print("\n=== handoff.json ===")
            print(json.dumps(
                _build_handoff(
                    workspace, spec, source_map, probes, retrieved, docs_md, tag_filter=args.tag
                ),
                indent=2,
            ))
        for w in warnings:
            print(f"WARN: {w}", file=sys.stderr)
        return 0

    docs_path = os.path.join(workspace, "docs.md")
    with open(docs_path, "w", encoding="utf-8") as f:
        f.write(docs_md)

    outputs = [file_entry(workspace, docs_path)]

    if args.emit_handoff:
        handoff = _build_handoff(
            workspace, spec, source_map, probes, retrieved, docs_md, tag_filter=args.tag
        )
        handoff_path = os.path.join(workspace, "handoff.json")
        with open(handoff_path, "w", encoding="utf-8") as f:
            json.dump(handoff, f, indent=2)
            f.write("\n")
        outputs.append(file_entry(workspace, handoff_path))

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    finished = now_iso()
    record_run(
        workspace,
        subcommand="consolidate",
        args={
            "merge_probes": args.merge_probes,
            "tags": args.tag,
            "sanitize": args.sanitize,
        },
        started_at=started,
        finished_at=finished,
        outputs=outputs,
    )

    if not args.quiet:
        print(f"wrote {docs_path}", file=sys.stderr)
        if args.emit_handoff:
            print(f"wrote {os.path.join(workspace, 'handoff.json')}", file=sys.stderr)
    return 0
