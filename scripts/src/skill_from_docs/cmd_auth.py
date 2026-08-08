"""openapi-harvest auth — confirm a working authentication pattern."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from . import __version__
from ._http import (
    AllowlistViolation,
    build_client,
    request_with_retry,
    require_allowlist,
)
from ._manifest import file_entry, now_iso, record_run
from ._redaction import redact_body, redact_headers, redact_text, redact_url
from ._schema import ProbeFixture, ProbeManifest, ProbeRequest, ProbeResponse
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
    p.add_argument("--basic-creds", help="USER:PASS on the command line (logs to shell history).")
    p.add_argument(
        "--basic-creds-env",
        help="Name of env var containing USER:PASS (preferred over --basic-creds).",
    )
    p.add_argument(
        "--spec",
        help="Optional OpenAPI spec path. When provided, the cascade is filtered to "
        "the spec's declared securitySchemes (header-based preferred automatically).",
    )
    p.add_argument("--bad-token-pattern", default=FIXED_BAD_TOKEN)
    p.add_argument("--allow-host", action="append", default=[])
    # Redirects are never followed; see cmd_probe for the reasoning. Accepted
    # for compatibility, states the guarantee rather than toggling it.
    p.add_argument(
        "--no-follow-redirects",
        dest="follow_redirects",
        action="store_false",
        default=False,
        help="accepted for compatibility; redirects are never followed",
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


def _resolve_basic_creds(args) -> tuple[str | None, int]:
    """Resolve Basic credentials from --basic-creds OR --basic-creds-env.

    Returns (creds_or_None, exit_code). exit_code is 0 on success (creds may be
    None if Basic was not requested), or 1 on user error. Env-var path is
    preferred because CLI creds leak via shell history / ps output / process
    listings; --basic-creds prints a stderr warning so contributors notice.
    """
    if args.basic_creds and args.basic_creds_env:
        print(
            "ERROR: --basic-creds and --basic-creds-env are mutually exclusive.",
            file=sys.stderr,
        )
        return (None, 1)
    if args.basic_creds_env:
        creds = os.environ.get(args.basic_creds_env)
        if not creds:
            print(
                f"ERROR: env var {args.basic_creds_env!r} is not set or is empty.",
                file=sys.stderr,
            )
            return (None, 1)
        return (creds, 0)
    if args.basic_creds:
        print(
            "WARNING: --basic-creds passes credentials on the command line, which "
            "leaks via shell history and process listings. Use --basic-creds-env "
            "VARNAME instead for production use.",
            file=sys.stderr,
        )
        return (args.basic_creds, 0)
    return (None, 0)


def _classify_winner(name: str | None) -> tuple[str | None, list[str]]:
    """Classify the winning pattern. Returns (auth_method, security_warnings).

    Downstream skill-creator reads these from handoff.json.content_shape_signals
    to decide what the generated integration skill must warn users about.
    """
    if name is None:
        return (None, [])
    if name.startswith("query "):
        return (
            "query_string",
            [
                "Query-string credentials leak into logs, proxies, CDN caches, "
                "browser history, and server access logs. The generated integration "
                "skill MUST warn users about this risk and recommend migrating to a "
                "header-based pattern if the API supports it."
            ],
        )
    if name == "Basic auth":
        return (
            "basic",
            [
                "Basic auth credentials. The generated integration skill MUST load "
                "USER:PASS from environment variables (never hardcode in source or "
                "docs). Recommend a credential helper for local development."
            ],
        )
    if name == "Bearer header":
        return ("bearer", [])
    if name in ("Token header", "raw Authorization"):
        return ("auth_token_header", [])
    return ("api_key_header", [])


def _load_spec_schemes(spec_path: str) -> dict[str, Any]:
    """Load components.securitySchemes from a local OpenAPI spec file.

    Tries JSON first; falls back to YAML if pyyaml is available. Returns an
    empty dict if the spec can't be parsed or doesn't declare schemes.
    """
    try:
        with open(spec_path, "rb") as f:
            text = f.read()
    except OSError:
        return {}
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            spec = yaml.safe_load(text)
        except Exception:
            return {}
    if not isinstance(spec, dict):
        return {}
    components = spec.get("components") or {}
    schemes = components.get("securitySchemes") or {}
    return schemes if isinstance(schemes, dict) else {}


def _filter_cascade_by_spec(
    cascade: list[tuple[str, dict[str, str], str]],
    schemes: dict[str, Any],
    include_query_auth: bool,
) -> tuple[list[tuple[str, dict[str, str], str]], list[str]]:
    """Filter the auth cascade to patterns the spec actually declares.

    Implements the prefer-header-automatically rule: if the spec declares any
    header-based scheme, query-string patterns are dropped from the cascade even
    when --include-query-auth was set. The user gets a warning so the override
    is legible. Returns (filtered_cascade, warnings).
    """
    warnings: list[str] = []
    if not schemes:
        return cascade, warnings  # No declared schemes: keep brute-force.

    has_bearer = False
    has_basic = False
    declared_api_key_headers: set[str] = set()
    declared_api_key_query: set[str] = set()
    for scheme in schemes.values():
        if not isinstance(scheme, dict):
            continue
        stype = scheme.get("type")
        if stype == "http":
            scheme_kind = (scheme.get("scheme") or "").lower()
            if scheme_kind == "bearer":
                has_bearer = True
            elif scheme_kind == "basic":
                has_basic = True
        elif stype == "apiKey":
            in_ = scheme.get("in")
            name_ = scheme.get("name")
            if not name_:
                continue
            if in_ == "header":
                declared_api_key_headers.add(name_)
            elif in_ == "query":
                declared_api_key_query.add(name_)

    has_any_header_pattern = has_bearer or has_basic or bool(declared_api_key_headers)

    if has_any_header_pattern and include_query_auth:
        warnings.append(
            "Spec declares header-based authentication; query-string patterns excluded "
            "from probe cascade despite --include-query-auth (prefer-header-automatically "
            "policy). Pass --no-prefer-header-automatically if you really need both."
        )

    filtered: list[tuple[str, dict[str, str], str]] = []
    for entry in cascade:
        pattern_name = entry[0]
        keep = False
        if pattern_name == "Bearer header" and has_bearer:
            keep = True
        elif pattern_name == "Token header" and has_bearer:
            # Many APIs accept "Token <X>" as a Bearer alias; keep for tolerance.
            keep = True
        elif pattern_name == "raw Authorization" and (has_bearer or has_basic):
            keep = True
        elif pattern_name == "Basic auth" and has_basic:
            keep = True
        elif pattern_name == "X-API-Key" and "X-API-Key" in declared_api_key_headers:
            keep = True
        elif pattern_name == "X-Auth-Token" and "X-Auth-Token" in declared_api_key_headers:
            keep = True
        elif pattern_name == "Api-Key" and "Api-Key" in declared_api_key_headers:
            keep = True
        elif pattern_name == "Token (custom header)" and "Token" in declared_api_key_headers:
            keep = True
        elif pattern_name.startswith("query "):
            qkey = pattern_name.replace("query ?", "").rstrip("=")
            # Only keep query patterns if spec declares them AND no header alternative.
            if qkey in declared_api_key_query and not has_any_header_pattern:
                keep = True
        if keep:
            filtered.append(entry)

    if not filtered:
        # Spec declares only schemes we don't probe (e.g., oauth2). Fall through.
        warnings.append(
            "Spec declares only schemes openapi-harvest doesn't probe (e.g., oauth2, "
            "openIdConnect). Falling back to the full brute-force cascade."
        )
        return cascade, warnings

    return filtered, warnings


def _query_url(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    pairs.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _try(client, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
    # No allowlist argument: `client` is a GuardedClient bound to one, so the
    # check happens inside `client.request` and raises AllowlistViolation from
    # there. (D1)
    resp = request_with_retry(client, "GET", url, max_retries=0, headers=headers)
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
        lines.append("")

    # Security guidance — propagates the auth-method policy to docs.md and
    # ultimately to handoff.json. skill-creator reads this to decide what
    # warnings to emit in the generated integration skill.
    auth_method = report.get("auth_method")
    warnings = report.get("security_warnings") or []
    if warnings:
        lines.append(f"## Security guidance (auth_method: `{auth_method}`)")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

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
    allowlist = require_allowlist(args.allow_host, subcommand="auth")
    if allowlist is None:
        return 1

    try:
        allowlist.check(args.endpoint)
    except AllowlistViolation as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    resolved_basic_creds, basic_exit = _resolve_basic_creds(args)
    if basic_exit != 0:
        return basic_exit

    workspace = args.workspace or default_workspace(args.endpoint)
    os.makedirs(workspace, exist_ok=True)
    started = now_iso()

    attempts: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    success_response_headers: dict[str, str] = {}

    with build_client(
        allowlist=allowlist,
        timeout=args.timeout,
        follow_redirects=args.follow_redirects,
        transport=transport,
    ) as client:
        # Unauthenticated baseline.
        try:
            base_status, base_headers, base_body = _try(client, args.endpoint, {})
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
            )
        except Exception as e:
            print(f"ERROR: network error: {e}", file=sys.stderr)
            return 2
        bad_token = {"status": bt_status, "body": redact_body(bt_body)}

        # Cascade.
        cascade: list[tuple[str, dict[str, str], str]] = []
        for name, fn in HEADER_PATTERNS:
            cascade.append((name, fn(args.token), args.endpoint))
        basic_creds = resolved_basic_creds  # from _resolve_basic_creds above
        if basic_creds:
            cascade.append(("Basic auth", _basic_header(basic_creds), args.endpoint))
        if args.include_query_auth:
            for k in ("api_key", "token", "access_token", "key"):
                cascade.append((f"query ?{k}=", {}, _query_url(args.endpoint, k, args.token)))

        # Spec-aware filtering. Prefer-header-automatically rule lives here:
        # if the spec declares a header-based scheme, query patterns drop out
        # of the cascade even when --include-query-auth was set.
        spec_filter_warnings: list[str] = []
        if args.spec:
            schemes = _load_spec_schemes(args.spec)
            cascade, spec_filter_warnings = _filter_cascade_by_spec(
                cascade, schemes, args.include_query_auth
            )
            for warn_line in spec_filter_warnings:
                print(f"NOTE: {warn_line}", file=sys.stderr)

        for name, headers, url in cascade:
            try:
                status, resp_headers, _body = _try(client, url, headers)
            except AllowlistViolation as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            except Exception as e:
                # The message can quote the URL that failed, and a
                # `--include-query-auth` URL carries the token in its query.
                attempts.append({"name": name, "status": -1, "error": redact_text(str(e))})
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

    # Classify the winning pattern so the markdown report + fixture manifest +
    # downstream handoff.json all carry the same signal. skill-creator reads
    # this from handoff.content_shape_signals.auth_method to decide what
    # warnings the generated integration skill must surface.
    winner_name = (winner or {}).get("name")
    auth_method, security_warnings = _classify_winner(winner_name)
    if spec_filter_warnings:
        security_warnings = security_warnings + spec_filter_warnings

    # H5: write a probe fixture for auth discovery so `validate` can index it.
    # Use the unauth baseline as the captured response — it's the most useful
    # signal (WWW-Authenticate header + 401 body). All fields URL-redacted.
    probes_dir = os.path.join(workspace, "probes")
    os.makedirs(probes_dir, exist_ok=True)
    parsed_endpoint = urlparse(args.endpoint)
    host_slug = (parsed_endpoint.hostname or "unknown").replace(".", "-")
    fixture_filename = f"auth-{host_slug}-{baseline['status']}.json"
    fixture_path = os.path.join(probes_dir, fixture_filename)
    # Build through ProbeFixture rather than hand-rolling the dict: the reader
    # (`cmd_consolidate._load_probes`) goes through `ProbeFixture.from_dict`,
    # so a key this type does not declare is a key nothing can read.
    fixture_payload = ProbeFixture(
        scope="auth-discovery",
        request=ProbeRequest(method="GET", url=redact_url(args.endpoint)),
        response=ProbeResponse(
            status=baseline["status"],
            headers=redact_headers(
                {
                    "WWW-Authenticate": baseline.get("www_authenticate") or "",
                    **rate_headers,
                }
            ),
            body=baseline.get("body"),
        ),
        manifest=ProbeManifest(
            tool_version=__version__,
            captured_at=started,
            auth_method=auth_method,
            security_warnings=security_warnings,
            winner_pattern=(winner or {}).get("name"),
            bad_token_status=bad_token["status"],
            attempts=attempts,
        ),
    ).to_dict()
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
        "auth_method": auth_method,
        "security_warnings": security_warnings,
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
        args={
            "endpoint": args.endpoint,
            "patterns_tried": len(attempts),
            "allow_host": sorted(args.allow_host or []),
        },
        started_at=started,
        finished_at=finished,
        outputs=[file_entry(workspace, fixture_rel)],
    )

    if winner is None:
        return 4
    return 0
