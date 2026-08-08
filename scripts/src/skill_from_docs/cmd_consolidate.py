"""openapi-harvest consolidate — emit docs.md and handoff.json from a workspace.

Three layers live here — load, spec-walk, render — and the fourth, the handoff
packet, lives in `_handoff.py`. The spec is walked exactly once per run, in
`WalkedSpec.walk`, and the resulting value feeds both the renderer and the
handoff builder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from ._handoff import CANONICAL_SECTIONS, build_handoff
from ._manifest import file_entry, now_iso, record_run
from ._provenance import emit_probe, emit_source
from ._sanitize import sanitize_spec_descriptions, sanitize_text, sanitize_text_for_markdown
from ._schema import ProbeFixture
from ._spec import iter_operations, json_pointer

# One source of truth for the canonical H2s, indexed by the name
# `handoff.coverage_checklist` uses. The renderer and the checklist used to
# carry separate copies that disagreed on "Rate limits, quotas, versioning".
_HEADING: dict[str, str] = {s.name: s.heading for s in CANONICAL_SECTIONS}


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


def _probe_url_path(url: str) -> str:
    """The path component of a probe's request URL, or `""` if it cannot be
    parsed. `urlparse` raises on a malformed IPv6 literal (`http://[::1`); a
    fixture carrying one used to take the whole run down with a traceback,
    breaking the numeric exit-code contract. An unparseable URL now simply
    matches no endpoint, which surfaces as the usual orphan-probe warning.
    """
    try:
        return urlparse(url).path
    except ValueError:
        return ""


class ProbeIndex:
    """Probe fixtures, with each request URL's path parsed once at load.

    Matching is by path suffix, and five independent passes over the spec each
    ask the same questions. The old `_match_probe` called `urlparse` per
    (probe, path) pair: 105,842 calls and 0.80 s of a 1.28 s profiled run at
    Stripe scale, for a value with 30 distinct inputs. Parsing at load kills the
    per-call cost; memoising the scan per path kills the pass multiplier.
    """

    __slots__ = ("_entries", "_paths", "_cache")

    def __init__(self, entries: Iterable[tuple[ProbeFixture, str]] = ()) -> None:
        self._entries: list[tuple[ProbeFixture, str]] = list(entries)
        self._paths: list[str] = [_probe_url_path(f.request.url) for f, _n in self._entries]
        self._cache: dict[str, list[int]] = {}

    def __iter__(self) -> Iterator[tuple[ProbeFixture, str]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def _indices_for(self, path: str) -> list[int]:
        hit = self._cache.get(path)
        if hit is None:
            # `pp == path` is the degenerate case of the suffix test.
            hit = [i for i, pp in enumerate(self._paths) if pp.endswith(path)]
            self._cache[path] = hit
        return hit

    def for_path(self, path: str) -> list[tuple[ProbeFixture, str]]:
        """Fixtures whose URL path ends with `path`, in load order."""
        return [self._entries[i] for i in self._indices_for(path)]

    def has_match(self, path: str) -> bool:
        return bool(self._indices_for(path))

    def unmatched(self, paths: Iterable[str]) -> list[tuple[ProbeFixture, str]]:
        """Fixtures matching none of `paths`, in load order."""
        seen: set[int] = set()
        for path in paths:
            seen.update(self._indices_for(path))
        return [e for i, e in enumerate(self._entries) if i not in seen]


def _load_probes(workspace: str) -> ProbeIndex:
    """Index the workspace's probes/ directory."""
    probes_dir = os.path.join(workspace, "probes")
    if not os.path.isdir(probes_dir):
        return ProbeIndex()
    out: list[tuple[ProbeFixture, str]] = []
    for name in sorted(os.listdir(probes_dir)):
        if not name.endswith(".json"):
            continue
        try:
            data = _read_json(os.path.join(probes_dir, name))
            out.append((ProbeFixture.from_dict(data), name))
        except Exception:
            continue
    return ProbeIndex(out)


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


@dataclass
class WalkedSpec:
    """Everything both builders need from the spec, derived in one traversal.

    `iter_operations` made the renderer and the handoff builder share one
    definition of "an operation"; it did not stop them walking the spec three
    times per run (twice to group by tag, once for the counts). This is that
    walk, done once in `run()`.

    `by_tag` is already `--tag`-filtered; `endpoint_count` and `tag_count`
    deliberately are not — they describe the spec, not the slice being rendered.
    """

    operations: tuple[tuple[str, str, dict[str, Any]], ...] = ()
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = field(default_factory=dict)
    paths: tuple[str, ...] = ()
    endpoint_count: int = 0
    tag_count: int = 0

    @classmethod
    def walk(cls, spec: dict[str, Any] | None, *, tags_filter: list[str]) -> "WalkedSpec":
        operations: list[tuple[str, str, dict[str, Any]]] = []
        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
        tags_seen: set[str] = set()
        for path, method, op in iter_operations(spec):
            operations.append((path, method, op))
            tags = op.get("tags")
            tags_seen.update(tags or [])
            for tag in tags or ["_untagged"]:
                grouped[tag].append((path, method.upper(), op))
        by_tag = dict(grouped)
        if tags_filter:
            by_tag = {k: v for k, v in by_tag.items() if k in tags_filter}
        paths = list(dict.fromkeys(p for ops in by_tag.values() for p, _m, _op in ops))
        return cls(
            operations=tuple(operations),
            by_tag=by_tag,
            paths=tuple(paths),
            endpoint_count=len(operations),
            tag_count=len(tags_seen),
        )


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

    pointer = json_pointer(path, method)
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


def _emit_section(lines: list[str], heading: str, body: Iterable[str]) -> None:
    """Write one `## <heading>` section: the heading, a blank, the body, and
    exactly one trailing blank line. Nine sections used to repeat this
    scaffolding by hand, ~100 lines of it.

    Trailing blanks already in `body` are collapsed into that one separator, so
    a body that naturally ends in a blank (every endpoint block does) does not
    open a double gap before the next H2.
    """
    lines.append(f"## {heading}")
    lines.append("")
    body = list(body)
    while body and body[-1] == "":
        body.pop()
    lines.extend(body)
    lines.append("")


def _emit_narrative_section(
    lines: list[str],
    heading: str,
    narratives: dict[str, str],
    key: str,
    retrieved: str,
    *,
    missing_todo: str | None = None,
) -> None:
    """H6: write a section body sourced from `narrative/<key>.md`, emitting a
    `<!-- source: narrative file: ... -->` provenance comment so `validate`
    accepts the section. Falls back to `_Not documented upstream._` (plus
    `missing_todo`, if the section has one) when no narrative exists.
    """
    body = narratives.get(key)
    if body:
        section = [
            body,
            "",
            emit_source("(narrative)", retrieved=retrieved, raw_file=f"narrative/{key}.md"),
        ]
    else:
        section = ["_Not documented upstream._"]
        if missing_todo:
            section.extend(["", missing_todo])
    _emit_section(lines, heading, section)


def _authentication_body(
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    narratives: dict[str, str],
    retrieved: str,
) -> list[str]:
    auth_body = narratives.get("authentication")
    spec_url_for_auth = (source_map or {}).get("spec_url")
    if auth_body:
        return [
            auth_body,
            "",
            emit_source(
                spec_url_for_auth or "(narrative)",
                retrieved=retrieved,
                raw_file="narrative/authentication.md",
            ),
        ]
    sec = (spec or {}).get("components", {}).get("securitySchemes", {})
    if not sec:
        return ["_Not documented upstream._"]
    lines: list[str] = []
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
    return lines


def _api_reference_body(
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    spec_path: str | None,
    probes: ProbeIndex,
    walked: WalkedSpec,
    *,
    tags_filter: list[str],
    merge_probes: bool,
    retrieved: str,
    warnings: list[str],
) -> list[str]:
    if not spec:
        return ["_No spec available._"]

    spec_url = (source_map or {}).get("spec_url")
    spec_raw_rel = (
        os.path.relpath(spec_path, os.path.dirname(os.path.dirname(spec_path)))
        if spec_path
        else "raw/spec.json"
    )
    by_tag = walked.by_tag
    lines: list[str] = []

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
        for path, method, op in ops:
            lines.extend(
                _endpoint_block(
                    path,
                    method,
                    op,
                    spec_url=spec_url,
                    retrieved=retrieved,
                    raw_file=spec_raw_rel,
                    probes_for_endpoint=probes.for_path(path) if merge_probes else [],
                )
            )
        if merge_probes and not any(probes.has_match(p) for p, _m, _op in ops):
            lines.append(f"<!-- TODO: no probe captured for tag {tag} -->")
            lines.append("")

    # One orphan-probe scan. There used to be two, differing only in guard and
    # message, and they were mutually exclusive on `tags_filter`: a probe that
    # matches nothing is either outside the requested slice or outside the spec.
    if tags_filter:
        orphan_msg: str | None = "references endpoint outside --tag filter"
    elif merge_probes:
        orphan_msg = "does not match any spec endpoint"
    else:
        orphan_msg = None
    if orphan_msg:
        for probe, _filename in probes.unmatched(walked.paths):
            warnings.append(
                f"probe {probe.request.method} {probe.request.url} {orphan_msg}"
            )
    return lines


def _build_docs_md(
    spec: dict[str, Any] | None,
    source_map: dict[str, Any] | None,
    spec_path: str | None,
    probes: ProbeIndex,
    narratives: dict[str, str],
    walked: WalkedSpec,
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

    _emit_section(
        lines,
        "Coverage status",
        [
            "- [x] OpenAPI spec parsed" if spec else "- [ ] OpenAPI spec not loaded",
            "- [x] Probes merged" if merge_probes and probes else "- [ ] Probes not merged",
        ],
    )
    _emit_narrative_section(lines, _HEADING["Installation"], narratives, "installation", retrieved)
    _emit_section(
        lines,
        _HEADING["Authentication"],
        _authentication_body(spec, source_map, narratives, retrieved),
    )
    _emit_narrative_section(lines, _HEADING["Core concepts"], narratives, "core-concepts", retrieved)
    _emit_section(
        lines,
        _HEADING["API reference"],
        _api_reference_body(
            spec,
            source_map,
            spec_path,
            probes,
            walked,
            tags_filter=tags_filter,
            merge_probes=merge_probes,
            retrieved=retrieved,
            warnings=warnings,
        ),
    )
    _emit_narrative_section(
        lines,
        _HEADING["Minimal working example"],
        narratives,
        "example",
        retrieved,
        missing_todo="<!-- TODO: provide a minimal working example -->",
    )
    _emit_narrative_section(lines, _HEADING["Errors"], narratives, "errors", retrieved)
    _emit_narrative_section(
        lines, _HEADING["Rate limits"], narratives, "rate-limits", retrieved
    )
    _emit_narrative_section(lines, _HEADING["Gotchas"], narratives, "gotchas", retrieved)

    return "\n".join(lines).rstrip() + "\n"


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

    probes = _load_probes(workspace) if args.merge_probes else ProbeIndex()
    narratives = _load_narratives(workspace, args.narrative_dir)
    if args.sanitize:
        sanitized_narratives: dict[str, str] = {}
        for k, v in narratives.items():
            result = sanitize_text(v, source_pointer=f"narrative/{k}.md")
            sanitized_narratives[k] = result.text
        narratives = sanitized_narratives

    # The one spec traversal. Both builders read this value.
    walked = WalkedSpec.walk(spec, tags_filter=args.tag)

    warnings: list[str] = []
    docs_md = _build_docs_md(
        spec,
        source_map,
        spec_path,
        probes,
        narratives,
        walked,
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
                build_handoff(
                    workspace, spec, source_map, probes, retrieved, docs_md, walked=walked
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
        handoff = build_handoff(
            workspace, spec, source_map, probes, retrieved, docs_md, walked=walked
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
