"""Schemas and file contracts shared across the CLI subcommands.

Three contracts live here: the probe-fixture dataclasses, the `handoff.json`
linter, and the `raw/source-map.json` accessors.

**`raw/source-map.json` holds a live credential and must not leave the machine.**
That is new as of A8 and is the reason this module writes the file itself
instead of leaving six lines of `open`/`json.dump` in `cmd_fetch`. See the
"source-map.json contract" section below for the display/fetchable split, and
`write_source_map` for the `0o600` guarantee.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ._io import write_json
from ._redaction import REDACTED, redact_url


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
    # auth-discovery probes carry these. Other scopes leave them at defaults.
    auth_method: str | None = None
    security_warnings: list[str] = field(default_factory=list)
    # The auth cascade: which pattern won, what a deliberately bad token
    # returned, and every pattern tried with its status. `cmd_auth` used to
    # hand-build its fixture dict and write these three keys directly, but
    # `from_dict` did not know them and `cmd_consolidate._load_probes` — the
    # only reader — goes through `from_dict`, so the whole record was written
    # to disk and silently dropped on read. They are declared here so the
    # round-trip is closed.
    winner_pattern: str | None = None
    bad_token_status: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


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
                "winner_pattern": self.manifest.winner_pattern,
                "bad_token_status": self.manifest.bad_token_status,
                "attempts": list(self.manifest.attempts),
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
                winner_pattern=man.get("winner_pattern"),
                bad_token_status=man.get("bad_token_status"),
                attempts=list(man.get("attempts") or []),
            ),
        )



# --- raw/source-map.json contract ------------------------------------------
#
# The file records where the spec came from, in two spellings:
#
#   "spec_url"   DISPLAY form — `redact_url`'d at the point the value enters
#                the workspace. This is the only spelling any other artifact
#                is allowed to copy: `docs.md`'s `<!-- source: -->` comments,
#                `handoff.json`'s spec_url / provenance_index /
#                coverage_checklist, and every probe fixture's
#                `spec_url_at_capture` all carry it.
#   "fetch_url"  FETCHABLE form — the URL verbatim, credentials and all.
#                Absent for a local-file harvest, and absent from every
#                workspace harvested before A8.
#
# Why two (DEFERRED.md A8): `key` is in `SENSITIVE_QUERY_KEYS`, so
# `?key=petstore` — a resource name, not a secret — is recorded as
# `?key=<redacted>`. The audit trail then names a URL nobody can re-fetch, and
# `validate --network` GETs it and reports a 404 that is not real. Dropping
# `key` from the sensitive set would trade that for a leaked credential, since
# `?key=<apikey>` is at least as common. Recording both is the fix.
#
# The split only works if the fetchable form stays put. Two rules enforce that:
#
#   1. `read_source_map` — what `consolidate` and `probe` use — strips
#      `fetch_url`. Those subcommands write `docs.md`, `handoff.json` and the
#      probe fixtures; they cannot leak a value they are never handed.
#      `read_fetch_url` is the single reader, and `validate --network` is its
#      single caller.
#   2. `write_source_map` creates the file `0o600`.
#
# Anything that *compares* URLs keeps comparing the display form — the
# provenance index, probe matching and `quick-diff` all read `spec_url`. That
# is deliberate: DEFERRED.md failure mode 5 was two layers hashing "the same"
# artifact and disagreeing about which bytes. `read_fetch_url` is the one place
# the two forms meet, and it normalizes both through `redact_url` before
# treating them as the same URL.

SOURCE_MAP_FILENAME = "source-map.json"
FETCH_URL_KEY = "fetch_url"
SOURCE_MAP_MODE = 0o600


def source_map_path(workspace: str) -> str:
    return os.path.join(workspace, "raw", SOURCE_MAP_FILENAME)


def write_source_map(path: str, data: dict[str, Any]) -> None:
    """Write `raw/source-map.json` with mode `0o600`.

    The permissions are the point, not housekeeping: `fetch_url` can be a live
    credential, and before A8 nothing in the workspace was. `mode` is passed
    explicitly because `_io.write_json` writes through a temp file and renames
    it into place: a fresh temp gets umask permissions, so omitting `mode` here
    would quietly hand this file out at `0o644`. `_io` applies it to the temp
    descriptor before the content is written and before the replace, so the
    credential is never on disk at looser permissions — not even for the window
    between two syscalls, and not even when a world-readable `source-map.json`
    is already sitting there.
    """
    write_json(path, data, mode=SOURCE_MAP_MODE)


def read_source_map(workspace: str) -> dict[str, Any]:
    """The display view of `raw/source-map.json`: `fetch_url` is stripped.

    Every consumer but `validate --network` reads through here, so the
    fetchable URL is not merely "not copied into `docs.md`" — the code that
    writes `docs.md` never holds it. `{}` for a workspace with no source map;
    a malformed one still raises, which is the pre-A8 behaviour of the two
    call sites.
    """
    path = source_map_path(workspace)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != FETCH_URL_KEY}


def read_fetch_url(workspace: str, display_url: str) -> tuple[str | None, str | None]:
    """Resolve the URL to actually GET for `display_url`.

    Returns `(fetchable_url, skip_reason)`; exactly one is non-None, and the
    reason is safe to print — it never quotes either URL.

    Three cases, in order:

    - The workspace records a `fetch_url` that agrees with `display_url`: use
      it. Agreement is decided by normalizing **both** through `redact_url`,
      never by comparing a redacted string to an unredacted one. Two layers
      comparing "the same" URL have to agree on which one (failure mode 5).
    - `display_url` carries no redaction sentinel: it is already fetchable, so
      use it. This is the ordinary case and the pre-A8 behaviour, which is what
      keeps a workspace harvested before A8 working unchanged.
    - Otherwise there is nothing fetchable to GET. Skipping is the honest
      answer; fetching the redacted URL and reporting the 404 is A8 itself.
    """
    path = source_map_path(workspace)
    candidate: Any = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                candidate = data.get(FETCH_URL_KEY)
        except Exception:
            candidate = None

    if isinstance(candidate, str) and candidate:
        if redact_url(candidate) == redact_url(display_url):
            return candidate, None
        return None, (
            f"skipped: the fetchable URL in raw/{SOURCE_MAP_FILENAME} does not describe "
            "the spec URL handoff.json records. Re-run `openapi-harvest fetch` and "
            "`consolidate` so the two agree."
        )

    if REDACTED not in display_url:
        return display_url, None

    return None, (
        f"skipped: handoff.json records a redacted spec URL and raw/{SOURCE_MAP_FILENAME} "
        f"has no {FETCH_URL_KEY} (harvested before it was recorded), so there is no URL "
        "that can be re-fetched. Re-run `openapi-harvest fetch` to record one."
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
