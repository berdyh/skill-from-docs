"""openapi-harvest quick-diff — surface spec-vs-reality drift."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

from ._manifest import now_iso, sha256_file
from ._provenance import emit_probe
from ._redaction import redact_url
from ._schema import ProbeFixture


DRIFT_HEADERS = (
    "link",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "retry-after",
    "x-request-id",
    "sunset",
    "deprecation",
    "warning",
)

PLACEHOLDER_VALUES = ("string", "STRING", 0, [], {})


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "quick-diff",
        help="surface header/missing-field gaps",
        description="Compare a probe fixture against an OpenAPI spec; report drift.",
    )
    p.add_argument("fixture")
    p.add_argument("spec")
    p.add_argument("-o", "--output")
    p.add_argument("--source-map")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=run)


def _find_operation(spec: dict[str, Any], url: str, method: str) -> tuple[str | None, dict[str, Any] | None]:
    """Best-effort match a URL+method to a spec operation. Returns (path, op)."""
    parsed = urlparse(url)
    target_path = parsed.path
    paths = spec.get("paths") or {}
    method = method.lower()

    if target_path in paths and isinstance(paths[target_path], dict):
        op = paths[target_path].get(method)
        if op:
            return target_path, op

    # try stripping a leading /v1 etc. by suffix match
    for spec_path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        if target_path.endswith(spec_path) or spec_path == target_path:
            op = methods.get(method)
            if op:
                return spec_path, op
    return None, None


def _success_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    responses = op.get("responses") or {}
    for code in ("200", "201", 200, 201, "default"):
        r = responses.get(str(code)) or responses.get(code)
        if isinstance(r, dict):
            content = r.get("content") or {}
            for _ct, body in content.items():
                if isinstance(body, dict) and "schema" in body:
                    return body["schema"]
    return None


def _enumerate_fields(schema: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Flatten an object schema into {dotted_path: {type, required}}."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(schema, dict):
        return out
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for name, sub in props.items():
            key = f"{prefix}.{name}" if prefix else name
            stype = sub.get("type") if isinstance(sub, dict) else None
            out[key] = {"type": stype, "required": name in required}
            if isinstance(sub, dict) and (sub.get("type") == "object" or "properties" in sub):
                out.update(_enumerate_fields(sub, key))
    return out


def _enumerate_actual(body: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(body, dict):
        for k, v in body.items():
            key = f"{prefix}.{k}" if prefix else k
            out[key] = type(v).__name__
            if isinstance(v, dict):
                out.update(_enumerate_actual(v, key))
    return out


def _py_type_for_schema(t: str | None) -> set[str]:
    return {
        "string": {"str"},
        "integer": {"int"},
        "number": {"float", "int"},
        "boolean": {"bool"},
        "array": {"list"},
        "object": {"dict"},
    }.get(t or "", set())


def _placeholder_drift(body: Any) -> list[str]:
    findings: list[str] = []

    def _walk(v, path: str) -> None:
        if isinstance(v, dict):
            for k, sub in v.items():
                _walk(sub, f"{path}.{k}" if path else k)
        elif isinstance(v, list):
            if v == []:
                findings.append(f"{path or '<root>'}: empty list (possible placeholder)")
        else:
            if v in ("string", "STRING"):
                findings.append(f"{path or '<root>'}: literal placeholder string '{v}'")

    _walk(body, "")
    return findings


def run(args) -> int:
    if not os.path.exists(args.fixture):
        print(f"ERROR: fixture not found: {args.fixture}", file=sys.stderr)
        return 1
    if not os.path.exists(args.spec):
        print(f"ERROR: spec not found: {args.spec}", file=sys.stderr)
        return 1

    try:
        with open(args.fixture, "r", encoding="utf-8") as f:
            fixture_data = json.load(f)
        fixture = ProbeFixture.from_dict(fixture_data)
    except Exception as e:
        print(f"ERROR: cannot parse fixture: {e}", file=sys.stderr)
        return 3

    try:
        with open(args.spec, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except Exception as e:
        print(f"ERROR: cannot parse spec: {e}", file=sys.stderr)
        return 3

    drift_categories: dict[str, list[str]] = {
        "additive": [],
        "subtractive": [],
        "type_mismatch": [],
        "placeholder": [],
        "headers": [],
        "spec_revision": [],
    }

    spec_path, op = _find_operation(spec, fixture.request.url, fixture.request.method)
    if op is None:
        drift_categories["subtractive"].append(
            f"endpoint {fixture.request.method} {fixture.request.url} not in spec"
        )

    schema = _success_schema(op) if op else None
    if schema:
        spec_fields = _enumerate_fields(schema)
        actual_fields = _enumerate_actual(fixture.response.body)
        for actual_key in actual_fields:
            if actual_key not in spec_fields:
                drift_categories["additive"].append(actual_key)
        for spec_key, info in spec_fields.items():
            if info.get("required") and spec_key not in actual_fields:
                drift_categories["subtractive"].append(spec_key)
            if spec_key in actual_fields:
                want_types = _py_type_for_schema(info.get("type"))
                got = actual_fields[spec_key]
                if want_types and got not in want_types:
                    drift_categories["type_mismatch"].append(
                        f"{spec_key}: spec={info.get('type')} actual={got}"
                    )

    # Placeholder values literally returned.
    drift_categories["placeholder"] = _placeholder_drift(fixture.response.body)

    # Headers spec misses.
    for header in fixture.response.headers:
        if header.lower() in DRIFT_HEADERS:
            drift_categories["headers"].append(header)

    # Spec revision mismatch.
    if fixture.manifest.spec_sha256_at_capture:
        try:
            current_sha = sha256_file(args.spec)
        except OSError:
            current_sha = None
        if current_sha and current_sha != fixture.manifest.spec_sha256_at_capture:
            drift_categories["spec_revision"].append(
                f"captured against {fixture.manifest.spec_sha256_at_capture[:12]}, current is {current_sha[:12]}"
            )

    # Render report.
    lines = ["# quick-diff report", ""]
    # H10: drift-validation provenance comment so downstream tools can index
    # this output. Use the fixture's spec_url_at_capture when present.
    fixture_rel = os.path.basename(args.fixture)
    provenance_url = fixture.manifest.spec_url_at_capture or fixture.request.url
    lines.append(
        emit_probe(
            fixture.request.method,
            redact_url(provenance_url),
            status=fixture.response.status,
            retrieved=fixture.manifest.captured_at or now_iso(),
            scope="drift-validation",
            fixture=fixture_rel,
        )
    )
    lines.append("")
    lines.append(f"- fixture: {args.fixture}")
    lines.append(f"- spec: {args.spec}")
    lines.append(f"- endpoint: {fixture.request.method} {redact_url(fixture.request.url)}")
    lines.append("")
    any_drift = False
    for cat, items in drift_categories.items():
        if not items:
            continue
        any_drift = True
        lines.append(f"## {cat}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    if not any_drift:
        lines.append("No drift detected.")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)

    if any_drift and args.strict:
        return 1
    return 0
