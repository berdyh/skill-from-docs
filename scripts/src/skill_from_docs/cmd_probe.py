"""openapi-harvest probe — capture one live response as a redacted fixture."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qsl, urlparse

from . import __version__, _cli
from ._http import (
    AllowlistViolation,
    build_client,
    request_with_retry,
    require_allowlist,
    require_positive_timeout,
)
from ._io import write_json
from ._manifest import now_iso, record_run, sha256_file
from ._redaction import (
    compile_patterns,
    redact_body,
    redact_headers,
    redact_url,
)
from ._schema import (
    ProbeFixture,
    ProbeManifest,
    ProbeRequest,
    ProbeResponse,
    read_source_map,
)
from ._slug import resolve_existing_workspace


VALID_SCOPES = ("case-study", "drift-validation", "auth-discovery", "ad-hoc")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "probe",
        help="capture one live response",
        description="Capture a single HTTP request/response pair as a redacted JSON fixture.",
        parents=[
            _cli.allow_host(),
            _cli.no_follow_redirects(),
            _cli.timeout(default=30.0),
            _cli.workspace_flag(),
            _cli.quiet(),
        ],
    )
    p.add_argument("url")
    p.add_argument("-X", "--method", default="GET")
    p.add_argument("-H", "--header", action="append", default=[])
    p.add_argument("-d", "--data")
    p.add_argument("-o", "--output")
    p.add_argument("--scope", required=True, choices=VALID_SCOPES)
    p.add_argument("--no-redact", action="store_true")
    p.add_argument("--redact-body-key", action="append", default=[])
    p.add_argument("--redact-body-pattern", action="append", default=[])
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=run)


def _parse_header(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise ValueError(f"bad header (expected K:V): {raw!r}")
    k, v = raw.split(":", 1)
    return k.strip(), v.strip()


def _read_body(spec: str | None) -> bytes | None:
    if spec is None:
        return None
    if spec.startswith("@"):
        path = spec[1:]
        with open(path, "rb") as f:
            return f.read()
    return spec.encode("utf-8")


def _reject_json_constant(name: str):
    """json.loads accepts Python-only NaN/Infinity; the fixture must stay valid
    JSON for the process that reads it, so treat those bodies as text."""
    raise ValueError(f"non-JSON constant in body: {name}")


def _looks_form_encoded(text: str) -> bool:
    """Heuristic for application/x-www-form-urlencoded without a Content-Type.

    Deliberately strict: every `&`-separated segment must be a single `k=v`
    pair with a non-empty key that looks like an identifier. A JSON body, an
    XML body, or free text will not match.
    """
    if "=" not in text or "\n" in text:
        return False
    segments = text.split("&")
    if len(segments) == 1 and not segments[0].partition("=")[2].strip("="):
        # `c2VjcmV0...=` is base64 padding, not `key=`. Converting it would move
        # the blob into a dict KEY, where redact_body's pattern pass never looks.
        return False
    for segment in segments:
        # partition, not count("=") — a base64-padded value (`secret=abc==`) is
        # the common shape for exactly the credentials this needs to reach.
        key, sep, _value = segment.partition("=")
        if not sep or not key or not re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", key):
            return False
    return True


def _decode_request_body(body_bytes: bytes | None) -> Any:
    """Decode a request body for the fixture, structuring it where possible.

    Structure matters for more than tidiness: `redact_body` only applies
    key-based redaction while walking dicts, so a body left as a string keeps
    `password=hunter2` verbatim in the saved fixture. The response path gets
    this for free via `resp.json()`; the request path has to ask.

    Form-encoded bodies matter as much as JSON here — an OAuth2 token request
    (`grant_type=password&client_secret=...`) is the most credential-dense body
    this tool will ever capture, and capturing it is exactly what
    `--scope auth-discovery` is for.
    """
    if not body_bytes:
        return None
    text = body_bytes.decode("utf-8", errors="replace")
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        pass
    if _looks_form_encoded(text):
        pairs = parse_qsl(text, keep_blank_values=True)
        out: dict[str, Any] = {}
        for key, value in pairs:
            # `scope=read&scope=write` is ordinary; collapsing to the last value
            # would misrepresent the request the fixture claims to record.
            if key in out:
                existing = out[key]
                out[key] = existing + [value] if isinstance(existing, list) else [existing, value]
            else:
                out[key] = value
        return out
    return text


def _fixture_slug(url: str, method: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "root"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", path).strip("-")
    return f"{method.lower()}-{safe}"


def _load_spec_meta(workspace: str) -> tuple[str | None, str | None]:
    """Return (spec_url_at_capture, spec_sha256_at_capture) from raw/source-map.json.

    `read_source_map` strips `fetch_url`, so the unredacted spec URL cannot
    reach a probe fixture's `spec_url_at_capture` even by accident (A8).
    """
    try:
        data = read_source_map(workspace)
    except Exception:
        return None, None
    return data.get("spec_url"), data.get("spec_sha256")


def run(args, *, transport=None, sleeper=time.sleep) -> int:
    allowlist = require_allowlist(args.allow_host, subcommand="probe")
    if allowlist is None:
        return 1

    if not require_positive_timeout(args.timeout, subcommand="probe"):
        return 1

    try:
        allowlist.check(args.url)
    except AllowlistViolation as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.scope not in VALID_SCOPES:
        print(f"ERROR: invalid --scope: {args.scope}", file=sys.stderr)
        return 1

    try:
        hdrs = dict(_parse_header(h) for h in args.header)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        body_bytes = _read_body(args.data)
    except OSError as e:
        print(f"ERROR: can't read --data: {e}", file=sys.stderr)
        return 1

    # `probe` must never derive its workspace from `args.url`. The spec host
    # and the live API host differ in most archetype-4 harvests, so deriving
    # sent `fetch` and `probe` to two different directories and `consolidate`
    # exited 3 on a workspace the user had just populated. Adopt the harvested
    # workspace or refuse; never guess.
    workspace: str | None = args.workspace
    if not workspace:
        if args.dry_run:
            # A dry run writes nothing, so it needs no workspace at all.
            workspace = None
        else:
            workspace, error = resolve_existing_workspace("probe")
            if workspace is None:
                print(error, file=sys.stderr)
                return 1
            if not args.quiet:
                print(f"using workspace {workspace}", file=sys.stderr)

    spec_url: str | None = None
    spec_sha: str | None = None
    if workspace is not None:
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(os.path.join(workspace, "probes"), exist_ok=True)
        spec_url, spec_sha = _load_spec_meta(workspace)

    redact_keys = args.redact_body_key
    redact_patterns = compile_patterns(args.redact_body_pattern)

    def _apply_redaction(headers: dict[str, str], url: str, body: Any) -> tuple[dict[str, str], str, Any]:
        if args.no_redact:
            return headers, url, body
        return (
            redact_headers(headers),
            redact_url(url),
            redact_body(body, extra_keys=redact_keys, patterns=redact_patterns),
        )

    if args.dry_run:
        req_headers, req_url, req_body = _apply_redaction(
            hdrs, args.url, _decode_request_body(body_bytes)
        )
        print(json.dumps(
            {
                "dry_run": True,
                "request": {
                    "method": args.method,
                    "url": req_url,
                    "headers": req_headers,
                    "body": req_body,
                },
            },
            indent=2,
        ))
        return 0

    started = now_iso()
    t0 = time.perf_counter()
    with build_client(
        allowlist=allowlist,
        timeout=args.timeout,
        follow_redirects=False,
        transport=transport,
    ) as client:
        try:
            # B2: this used to be a 38-line local fork of `request_with_retry`
            # with identical 429/5xx handling and one difference — it did not
            # retry transient network errors. `probe` is the subcommand most
            # likely to hit a flaky live API and the only one exposing
            # `--max-retries`, so the fork had quietly dropped exactly the
            # retry its own flag advertises.
            resp = request_with_retry(
                client,
                args.method,
                args.url,
                headers=hdrs,
                content=body_bytes,
                max_retries=args.max_retries,
                sleeper=sleeper,
            )
        except AllowlistViolation as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"ERROR: network error: {e}", file=sys.stderr)
            return 2

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        # max retries exceeded
        print(
            f"ERROR: server returned {resp.status_code} after {args.max_retries} retries",
            file=sys.stderr,
        )
        return 2

    # Build fixture (redaction applied).
    try:
        resp_body: Any = resp.json()
    except Exception:
        resp_body = resp.text

    req_headers, req_url, req_body = _apply_redaction(
        hdrs, args.url, _decode_request_body(body_bytes)
    )
    resp_headers = dict(resp.headers)
    if not args.no_redact:
        resp_headers = redact_headers(resp_headers)
    resp_body_clean = (
        resp_body
        if args.no_redact
        else redact_body(resp_body, extra_keys=redact_keys, patterns=redact_patterns)
    )

    fixture = ProbeFixture(
        scope=args.scope,
        request=ProbeRequest(method=args.method, url=req_url, headers=req_headers, body=req_body),
        response=ProbeResponse(
            status=resp.status_code, headers=resp_headers, body=resp_body_clean, timing_ms=elapsed_ms
        ),
        manifest=ProbeManifest(
            tool_version=__version__,
            captured_at=started,
            spec_url_at_capture=spec_url,
            spec_sha256_at_capture=spec_sha,
        ),
    )

    out_path = args.output or os.path.join(
        workspace, "probes", f"{_fixture_slug(args.url, args.method)}.json"
    )
    write_json(out_path, fixture.to_dict())

    finished = now_iso()
    record_run(
        workspace,
        subcommand="probe",
        args={
            "url": args.url,
            "method": args.method,
            "scope": args.scope,
            "allow_host": sorted(args.allow_host or []),
        },
        started_at=started,
        finished_at=finished,
        outputs=[{"path": os.path.relpath(out_path, workspace), "sha256": sha256_file(out_path)}],
    )

    if not args.quiet:
        print(f"wrote {out_path}", file=sys.stderr)
    return 0
