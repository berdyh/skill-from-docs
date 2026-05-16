"""openapi-harvest auth — confirm a working authentication pattern."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from ._http import (
    AllowlistViolation,
    HostAllowlist,
    build_client,
    request_with_retry,
)
from ._manifest import file_entry, now_iso, record_run
from ._redaction import redact_body, redact_headers, redact_url
from ._slug import default_workspace


FIXED_BAD_TOKEN = "aaaaaaaa-bad-token-bbbbbbbb"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "auth",
        help="confirm working auth pattern",
        description="Probe an endpoint with a cascade of auth patterns to find one that returns 200.",
    )
    p.add_argument("endpoint")
    p.add_argument("--token", required=True)
    p.add_argument("-o", "--output")
    p.add_argument("--short-circuit", dest="short_circuit", action="store_true", default=True)
    p.add_argument("--no-short-circuit", dest="short_circuit", action="store_false")
    p.add_argument("--include-query-auth", action="store_true", default=False)
    p.add_argument("--basic-creds")
    p.add_argument("--bad-token-pattern", default=FIXED_BAD_TOKEN)
    p.add_argument("--allow-host", action="append", default=[])
    p.add_argument(
        "--no-follow-redirects", dest="follow_redirects", action="store_false", default=False
    )
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--workspace")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=run)


HEADER_PATTERNS = [
    ("Bearer header", lambda token: {"Authorization": f"Bearer {token}"}),
    ("Token header", lambda token: {"Authorization": f"Token {token}"}),
    ("raw Authorization", lambda token: {"Authorization": token}),
    ("X-API-Key", lambda token: {"X-API-Key": token}),
    ("X-Auth-Token", lambda token: {"X-Auth-Token": token}),
    ("Api-Key", lambda token: {"Api-Key": token}),
    ("Token (custom header)", lambda token: {"Token": token}),
]


def _basic_header(creds: str) -> dict[str, str]:
    encoded = base64.b64encode(creds.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def _query_url(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    pairs.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _try(
    client,
    url: str,
    headers: dict[str, str],
    *,
    allowlist: HostAllowlist,
    timeout: float,
) -> tuple[int, dict[str, str], Any]:
    resp = request_with_retry(
        client, "GET", url, allowlist=allowlist, max_retries=0, headers=headers
    )
    body: Any
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:1024]
    return resp.status_code, dict(resp.headers), body


def _format_markdown(report: dict[str, Any]) -> str:
    # B1: always URL-redact the endpoint shown to the user. Sensitive query
    # params (api_key, token, etc.) must never survive into captured markdown.
    endpoint_display = redact_url(report["endpoint"])
    lines: list[str] = ["# Authentication probe report", ""]
    lines.append(f"- endpoint: `{endpoint_display}`")
    lines.append(f"- captured_at: {report['captured_at']}")
    # H5: probe provenance comment so `validate` can index this as a source.
    fixture_rel = report.get("fixture_relpath")
    if fixture_rel:
        host = urlparse(report["endpoint"]).hostname or "unknown"  # noqa: F841
        winner_status = (
            report["winner"]["status"]
            if report.get("winner") and "status" in report["winner"]
            else report["unauthenticated"]["status"]
        )
        lines.append(
            f"<!-- probe: GET {endpoint_display} status: {winner_status} "
            f"retrieved: {report['captured_at']} scope: auth-discovery "
            f"fixture: {fixture_rel} -->"
        )
    lines.append("")

    if report.get("winner"):
        w = report["winner"]
        lines.append(f"**Working pattern:** {w['name']}")
        lines.append("")

    lines.append("## Unauthenticated baseline")
    base = report["unauthenticated"]
    lines.append(f"- status: {base['status']}")
    lines.append(f"- WWW-Authenticate: `{base.get('www_authenticate') or '(absent)'}`")
    lines.append("")

    lines.append("## Bad-token capture")
    bt = report["bad_token"]
    lines.append(f"- status: {bt['status']}")
    lines.append("- body sample:")
    lines.append("```json")
    lines.append(json.dumps(bt["body"], indent=2)[:2000])
    lines.append("```")
    lines.append("")

    lines.append("## Tried patterns")
    for attempt in report["attempts"]:
        marker = "OK" if attempt["status"] == 200 else "FAIL"
        lines.append(f"- {marker} {attempt['name']} -> status {attempt['status']}")
    lines.append("")

    if report.get("rate_limit_headers"):
        lines.append("## Rate-limit headers (from success response)")
        for k, v in report["rate_limit_headers"].items():
            lines.append(f"- `{k}: {v}`")

    return "\n".join(lines).rstrip() + "\n"


_RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "retry-after",
    "x-request-id",
    "x-trace-id",
    "sunset",
    "deprecation",
    "warning",
)


def run(args, *, transport=None) -> int:
    if not args.allow_host:
        print("ERROR: --allow-host is required for auth.", file=sys.stderr)
        return 1

    allowlist = HostAllowlist(args.allow_host)
    try:
        allowlist.check(args.endpoint)
    except AllowlistViolation as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    workspace = args.workspace or default_workspace(args.endpoint)
    os.makedirs(workspace, exist_ok=True)
    started = now_iso()

    attempts: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    success_response_headers: dict[str, str] = {}

    with build_client(
        timeout=args.timeout,
        follow_redirects=args.follow_redirects,
        transport=transport,
    ) as client:
        # Unauthenticated baseline.
        try:
            base_status, base_headers, base_body = _try(
                client, args.endpoint, {}, allowlist=allowlist, timeout=args.timeout
            )
        except AllowlistViolation as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"ERROR: network error: {e}", file=sys.stderr)
            return 2
        baseline = {
            "status": base_status,
            "www_authenticate": base_headers.get("WWW-Authenticate")
            or base_headers.get("www-authenticate"),
            "body": redact_body(base_body),
        }

        # Bad-token capture (uses the fixed bad token, never the real one).
        try:
            bt_status, _, bt_body = _try(
                client,
                args.endpoint,
                {"Authorization": f"Bearer {args.bad_token_pattern}"},
                allowlist=allowlist,
                timeout=args.timeout,
            )
        except Exception as e:
            print(f"ERROR: network error: {e}", file=sys.stderr)
            return 2
        bad_token = {"status": bt_status, "body": redact_body(bt_body)}

        # Cascade.
        cascade: list[tuple[str, dict[str, str], str]] = []
        for name, fn in HEADER_PATTERNS:
            cascade.append((name, fn(args.token), args.endpoint))
        if args.basic_creds:
            cascade.append(("Basic auth", _basic_header(args.basic_creds), args.endpoint))
        if args.include_query_auth:
            for k in ("api_key", "token", "access_token", "key"):
                cascade.append((f"query ?{k}=", {}, _query_url(args.endpoint, k, args.token)))

        for name, headers, url in cascade:
            try:
                status, resp_headers, _body = _try(
                    client, url, headers, allowlist=allowlist, timeout=args.timeout
                )
            except AllowlistViolation as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            except Exception as e:
                attempts.append({"name": name, "status": -1, "error": str(e)})
                continue
            attempts.append({"name": name, "status": status})
            if status == 200 and winner is None:
                winner = {
                    "name": name,
                    "headers": redact_headers(headers),
                    "url": redact_url(url),
                }
                success_response_headers = resp_headers
                if args.short_circuit:
                    break

    rate_headers = {
        k: v for k, v in success_response_headers.items() if k.lower() in _RATE_LIMIT_HEADERS
    }

    # H5: write a probe fixture for auth discovery so `validate` can index it.
    # Use the unauth baseline as the captured response — it's the most useful
    # signal (WWW-Authenticate header + 401 body). All fields URL-redacted.
    probes_dir = os.path.join(workspace, "probes")
    os.makedirs(probes_dir, exist_ok=True)
    parsed_endpoint = urlparse(args.endpoint)
    host_slug = (parsed_endpoint.hostname or "unknown").replace(".", "-")
    fixture_filename = f"auth-{host_slug}-{baseline['status']}.json"
    fixture_path = os.path.join(probes_dir, fixture_filename)
    fixture_payload = {
        "scope": "auth-discovery",
        "request": {
            "method": "GET",
            "url": redact_url(args.endpoint),
            "headers": {},
            "body": None,
        },
        "response": {
            "status": baseline["status"],
            "headers": redact_headers(
                {
                    "WWW-Authenticate": baseline.get("www_authenticate") or "",
                    **{k: v for k, v in rate_headers.items()},
                }
            ),
            "body": baseline.get("body"),
            "timing_ms": None,
        },
        "manifest": {
            "tool_version": __import__("skill_from_docs").__version__,
            "captured_at": started,
            "spec_url_at_capture": None,
            "spec_sha256_at_capture": None,
            "winner_pattern": (winner or {}).get("name"),
            "bad_token_status": bad_token["status"],
            "attempts": attempts,
        },
    }
    with open(fixture_path, "w", encoding="utf-8") as f:
        json.dump(fixture_payload, f, indent=2)
        f.write("\n")
    fixture_rel = os.path.relpath(fixture_path, workspace)

    report = {
        "endpoint": args.endpoint,
        "captured_at": started,
        "unauthenticated": baseline,
        "bad_token": bad_token,
        "attempts": attempts,
        "winner": winner,
        "rate_limit_headers": rate_headers,
        "fixture_relpath": fixture_rel,
    }

    markdown = _format_markdown(report)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(markdown)
    else:
        sys.stdout.write(markdown)

    finished = now_iso()
    record_run(
        workspace,
        subcommand="auth",
        args={"endpoint": args.endpoint, "patterns_tried": len(attempts)},
        started_at=started,
        finished_at=finished,
        outputs=[file_entry(workspace, fixture_rel)],
    )

    if winner is None:
        return 4
    return 0
