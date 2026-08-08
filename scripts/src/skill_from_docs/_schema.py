"""Dataclass schemas used across the CLI subcommands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbeRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass
class ProbeResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    timing_ms: int | None = None


@dataclass
class ProbeManifest:
    tool_version: str
    captured_at: str
    spec_url_at_capture: str | None = None
    spec_sha256_at_capture: str | None = None
    # auth-discovery probes carry these. Other scopes leave them None.
    auth_method: str | None = None
    security_warnings: list[str] = field(default_factory=list)


@dataclass
class ProbeFixture:
    scope: str
    request: ProbeRequest
    response: ProbeResponse
    manifest: ProbeManifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "request": {
                "method": self.request.method,
                "url": self.request.url,
                "headers": self.request.headers,
                "body": self.request.body,
            },
            "response": {
                "status": self.response.status,
                "headers": self.response.headers,
                "body": self.response.body,
                "timing_ms": self.response.timing_ms,
            },
            "manifest": {
                "tool_version": self.manifest.tool_version,
                "captured_at": self.manifest.captured_at,
                "spec_url_at_capture": self.manifest.spec_url_at_capture,
                "spec_sha256_at_capture": self.manifest.spec_sha256_at_capture,
                "auth_method": self.manifest.auth_method,
                "security_warnings": list(self.manifest.security_warnings),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProbeFixture":
        req = data.get("request", {})
        resp = data.get("response", {})
        man = data.get("manifest", {})
        return cls(
            scope=data.get("scope", "ad-hoc"),
            request=ProbeRequest(
                method=req.get("method", "GET"),
                url=req.get("url", ""),
                headers=req.get("headers", {}) or {},
                body=req.get("body"),
            ),
            response=ProbeResponse(
                status=int(resp.get("status", 0)),
                headers=resp.get("headers", {}) or {},
                body=resp.get("body"),
                timing_ms=resp.get("timing_ms"),
            ),
            manifest=ProbeManifest(
                tool_version=man.get("tool_version", ""),
                captured_at=man.get("captured_at", ""),
                spec_url_at_capture=man.get("spec_url_at_capture"),
                spec_sha256_at_capture=man.get("spec_sha256_at_capture"),
                auth_method=man.get("auth_method"),
                security_warnings=list(man.get("security_warnings") or []),
            ),
        )




# --- handoff.json contract -------------------------------------------------
#
# handoff.json is the packet skill-creator consumes; it is a cross-process
# contract, so a shape error here surfaces as a confusing interview rather than
# a crash. These constants and `lint_handoff` are the machine-checkable form of
# the shape that SKILL.md describes in prose.
#
# Deliberately a linter over a plain dict rather than a dataclass: the packet
# has conditionally-present keys (auth_method / security_warnings appear only
# for auth-discovery harvests) and gap_list is populated after the dict is
# built, both of which a fixed-field dataclass would flatten.

HANDOFF_VERSION = 1

HANDOFF_REQUIRED_KEYS: tuple[str, ...] = (
    "version",
    "proposed_name",
    "tool_summary",
    "user_declared_scope",
    "user_declared_languages",
    "archetype_primary",
    "content_shape_signals",
    "coverage_checklist",
    "gap_list",
    "provenance_index",
    "image_inventory",
    "suggested_test_cases",
    "harvest_metadata",
)

# key -> (type, human-readable name) for keys whose type matters downstream.
_HANDOFF_TYPES: dict[str, tuple[type | tuple[type, ...], str]] = {
    "version": (int, "int"),
    "proposed_name": (str, "string"),
    "tool_summary": (str, "string"),
    "user_declared_languages": (list, "array"),
    "content_shape_signals": (dict, "object"),
    "coverage_checklist": (list, "array"),
    "gap_list": (list, "array"),
    "provenance_index": (dict, "object"),
    "image_inventory": (list, "array"),
    "suggested_test_cases": (list, "array"),
    "harvest_metadata": (dict, "object"),
}

HARVEST_METADATA_KEYS: tuple[str, ...] = (
    "retrieved_date",
    "tool_version",
    "raw_page_count",
    "docs_md_token_count",
)


def lint_handoff(data: Any) -> list[str]:
    """Return one message per handoff.json shape problem; empty means valid."""
    if not isinstance(data, dict):
        return [f"handoff.json must be a JSON object, got {type(data).__name__}"]

    problems: list[str] = []
    for key in HANDOFF_REQUIRED_KEYS:
        if key not in data:
            problems.append(f"missing required key: {key}")

    for key, (expected, label) in _HANDOFF_TYPES.items():
        if key in data and not isinstance(data[key], expected):
            problems.append(
                f"{key} must be {label}, got {type(data[key]).__name__}"
            )

    version = data.get("version")
    if isinstance(version, int) and version != HANDOFF_VERSION:
        problems.append(
            f"unsupported handoff version {version} (this tool emits {HANDOFF_VERSION})"
        )

    meta = data.get("harvest_metadata")
    if isinstance(meta, dict):
        for key in HARVEST_METADATA_KEYS:
            if key not in meta:
                problems.append(f"harvest_metadata missing key: {key}")

    return problems
