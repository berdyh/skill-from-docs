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

from . import __version__
from ._http import (
    AllowlistViolation,
    HostAllowlist,
    build_client,
)
from ._manifest import now_iso, record_run, sha256_file
from ._redaction import (
    compile_patterns,
    redact_body,
    redact_headers,
    redact_url,
)
from ._schema import ProbeFixture, ProbeManifest, ProbeRequest, ProbeResponse
from ._slug import default_workspace


VALID_SCOPES = ("case-study", "drift-validation", "auth-discovery", "ad-hoc")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "probe",
        help="capture one live response",
        description="Capture a single HTTP request/response pair as a redacted JSON fixture.",
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
    p.add_argument("--allow-host", action="append", default=[])
    p.add_argument("--max-retries", type=int, default=3)
    # Redirects are blocked by default: a 30x to an attacker host is the
    # canonical token-leak path. --no-follow-redirects is kept as an accepted
    # no-op so existing invocations and docs keep working.
    p.add_argument(
        "--follow-redirects",
        dest="follow_redirects",
        action="store_true",
        default=False,
        help="follow 30x responses (default: off; the Location header is captured, not followed)",
    )
    p.add_argument(
        "--no-follow-redirects",
        dest="follow_redirects",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--workspace")
    p.add_argument("-q", "--quiet", action="store_true")
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


def _looks_form_encoded(text: str) -> bool:
    """Heuristic for application/x-www-form-urlencoded without a Content-Type.

    Deliberately strict: every `&`-separated segment must be a single `k=v`
    pair with a non-empty key that looks like an identifier. A JSON body, an
    XML body, or free text will not match.
    """
    if "=" not in text or "\n" in text:
        return False
    for segment in text.split("&"):
        if segment.count("=") != 1:
            return False
        key, _, _value = segment.partition("=")
        if not key or not re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", key):
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
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    if _looks_form_encoded(text):
        return dict(parse_qsl(text, keep_blank_values=True))
    return text


def _fixture_slug(url: str, method: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "root"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", path).strip("-")
    return f"{method.lower()}-{safe}"


def _retry_with_policy(
    client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: bytes | None,
    allowlist: HostAllowlist,
    max_retries: int,
    sleeper=time.sleep,
):
    """Probe-specific retry: honors Retry-After on 429, backoff on 5xx,
    returns the last response on max retries exceeded.
    """
    if allowlist is not None:
        allowlist.check(url)

    attempts = 0
    last_resp = None
    while True:
        last_resp = client.request(method, url, headers=headers, content=content)
        if last_resp.status_code == 429 and attempts < max_retries:
            ra = last_resp.headers.get("Retry-After")
            try:
                delay = max(0.0, float(ra)) if ra else 2 ** attempts
            except ValueError:
                delay = 1.0
            sleeper(delay)
            attempts += 1
            continue
        if 500 <= last_resp.status_code < 600 and attempts < max_retries:
            sleeper(2 ** attempts)
            attempts += 1
            continue
        return last_resp


def _load_spec_meta(workspace: str) -> tuple[str | None, str | None]:
    """Return (spec_url_at_capture, spec_sha256_at_capture) from raw/source-map.json."""
    path = os.path.join(workspace, "raw", "source-map.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("spec_url"), data.get("spec_sha256")
    except Exception:
        return None, None


def run(args, *, transport=None, sleeper=time.sleep) -> int:
    # Test the constructed allowlist, not the raw arg list: argparse append
    # turns `--allow-host ""` (an unset shell var) into [""], which is truthy
    # but builds an empty allowlist, and an empty allowlist permits every host.
    allowlist = HostAllowlist(args.allow_host)
    if not allowlist:
        print(
            "ERROR: --allow-host HOST is required for probe (and must be non-empty).",
            file=sys.stderr,
        )
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

    workspace = args.workspace or default_workspace(args.url)
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
        timeout=args.timeout,
        follow_redirects=args.follow_redirects,
        transport=transport,
    ) as client:
        try:
            resp = _retry_with_policy(
                client,
                args.method,
                args.url,
                headers=hdrs,
                content=body_bytes,
                allowlist=allowlist,
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
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fixture.to_dict(), f, indent=2)
        f.write("\n")

    finished = now_iso()
    record_run(
        workspace,
        subcommand="probe",
        args={"url": args.url, "method": args.method, "scope": args.scope},
        started_at=started,
        finished_at=finished,
        outputs=[{"path": os.path.relpath(out_path, workspace), "sha256": sha256_file(out_path)}],
    )

    if not args.quiet:
        print(f"wrote {out_path}", file=sys.stderr)
    return 0
