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
            ),
        )


@dataclass
class NormalizedSpec:
    """Wraps a parsed OpenAPI spec with its source-map."""

    spec: dict[str, Any]
    source_map: dict[str, Any]
    sha256: str
    url: str | None = None
