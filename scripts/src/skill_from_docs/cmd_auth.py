"""openapi-harvest auth — confirm a working authentication pattern."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from . import __version__, _cli
from ._http import (
    AllowlistViolation,
    build_client,
    request_with_retry,
    require_allowlist,
    require_positive_timeout,
)
from ._io import write_json
from ._manifest import file_entry, now_iso, record_run
from ._provenance import emit_probe
from ._redaction import redact_body, redact_headers, redact_text, redact_url
from ._schema import ProbeFixture, ProbeManifest, ProbeRequest, ProbeResponse
from ._slug import resolve_existing_workspace


FIXED_BAD_TOKEN = "aaaaaaaa-bad-token-bbbbbbbb"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "auth",
        help="confirm working auth pattern",
        description="Probe an endpoint with a cascade of auth patterns to find one that returns 200.",
        parents=[
            _cli.allow_host(),
            _cli.no_follow_redirects(),
            _cli.timeout(default=10.0),
            _cli.workspace_flag(),
            _cli.quiet(),
        ],
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
    p.set_defaults(func=run)


# Auth-method classifications. These strings are written to disk as
# `ProbeManifest.auth_method` and lifted into handoff.json by `consolidate`,
# where skill-creator reads them — treat them as an external contract.
AUTH_BEARER = "bearer"
AUTH_TOKEN_HEADER = "auth_token_header"
AUTH_API_KEY_HEADER = "api_key_header"
AUTH_BASIC = "basic"
AUTH_QUERY_STRING = "query_string"


class DeclaredSchemes(NamedTuple):
    """What a spec's `components.securitySchemes` actually declares.

    Parsed once by `_declared_schemes`; every cascade entry's `keep_when`
    predicate is answered against this, so no code re-reads the raw spec.
    """

    bearer: bool = False
    basic: bool = False
    api_key_headers: frozenset[str] = frozenset()
    api_key_query: frozenset[str] = frozenset()

    @property
    def any_header(self) -> bool:
        return self.bearer or self.basic or bool(self.api_key_headers)


# Spec-filter predicates. Each cascade entry carries its own, so adding a
# pattern cannot leave it unreachable: `AuthPattern` has no default for
# `keep_when`, and the filter dispatches on the predicate rather than on the
# display name. Signature is (declared, pattern) because the api-key gates need
# the pattern's `key`.
KeepWhen = Callable[["DeclaredSchemes", "AuthPattern"], bool]


def _keep_if_bearer(declared: DeclaredSchemes, pattern: AuthPattern) -> bool:
    return declared.bearer


def _keep_if_bearer_or_basic(declared: DeclaredSchemes, pattern: AuthPattern) -> bool:
    return declared.bearer or declared.basic


def _keep_if_basic(declared: DeclaredSchemes, pattern: AuthPattern) -> bool:
    return declared.basic


def _keep_if_declared_header(declared: DeclaredSchemes, pattern: AuthPattern) -> bool:
    return pattern.key in declared.api_key_headers


def _keep_if_declared_query(declared: DeclaredSchemes, pattern: AuthPattern) -> bool:
    # Only probe query-string auth when the spec declares it AND offers no
    # header alternative (the prefer-header-automatically rule).
    return pattern.key in declared.api_key_query and not declared.any_header


class AuthPattern(NamedTuple):
    """One entry in the auth cascade, ready to send.

    `name` is a display label only: it reaches the markdown report and the probe
    fixture (`ProbeManifest.winner_pattern`, and each `attempts[].name`), and
    nothing parses it back. Every decision reads `kind`, `key` or `keep_when`.
    Renaming an entry therefore changes recorded artifacts but no behaviour.
    """

    name: str
    kind: str  # one of the AUTH_* constants; becomes `auth_method` if it wins
    key: str  # header name, or query-parameter name when kind is query_string
    headers: dict[str, str]
    url: str
    keep_when: KeepWhen


class HeaderPatternSpec(NamedTuple):
    """Static half of a header-based cascade entry (no token bound yet)."""

    name: str
    kind: str
    header: str
    value: str  # format template, `{token}` substituted at build time
    keep_when: KeepWhen


HEADER_PATTERNS: tuple[HeaderPatternSpec, ...] = (
    HeaderPatternSpec(
        "Bearer header", AUTH_BEARER, "Authorization", "Bearer {token}", _keep_if_bearer
    ),
    # Many APIs accept "Token <X>" as a Bearer alias; keep for tolerance.
    HeaderPatternSpec(
        "Token header", AUTH_TOKEN_HEADER, "Authorization", "Token {token}", _keep_if_bearer
    ),
    HeaderPatternSpec(
        "raw Authorization",
        AUTH_TOKEN_HEADER,
        "Authorization",
        "{token}",
        _keep_if_bearer_or_basic,
    ),
    HeaderPatternSpec(
        "X-API-Key", AUTH_API_KEY_HEADER, "X-API-Key", "{token}", _keep_if_declared_header
    ),
    HeaderPatternSpec(
        "X-Auth-Token", AUTH_API_KEY_HEADER, "X-Auth-Token", "{token}", _keep_if_declared_header
    ),
    HeaderPatternSpec(
        "Api-Key", AUTH_API_KEY_HEADER, "Api-Key", "{token}", _keep_if_declared_header
    ),
    HeaderPatternSpec(
        "Token (custom header)", AUTH_API_KEY_HEADER, "Token", "{token}", _keep_if_declared_header
    ),
)

QUERY_PARAM_KEYS = ("api_key", "token", "access_token", "key")


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


_SECURITY_WARNINGS: dict[str, list[str]] = {
    AUTH_QUERY_STRING: [
        "Query-string credentials leak into logs, proxies, CDN caches, "
        "browser history, and server access logs. The generated integration "
        "skill MUST warn users about this risk and recommend migrating to a "
        "header-based pattern if the API supports it."
    ],
    AUTH_BASIC: [
        "Basic auth credentials. The generated integration skill MUST load "
        "USER:PASS from environment variables (never hardcode in source or "
        "docs). Recommend a credential helper for local development."
    ],
}


def _classify_winner(kind: str | None) -> tuple[str | None, list[str]]:
    """Classify the winning pattern. Returns (auth_method, security_warnings).

    The `kind` comes straight off the winning `AuthPattern` — it is not derived
    from the display name. Downstream skill-creator reads these from
    handoff.json.content_shape_signals to decide what the generated integration
    skill must warn users about.
    """
    if kind is None:
        return (None, [])
    return (kind, list(_SECURITY_WARNINGS.get(kind, [])))


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


def _declared_schemes(schemes: dict[str, Any]) -> DeclaredSchemes:
    """Reduce raw `components.securitySchemes` to the four facts the gates need."""
    has_bearer = False
    has_basic = False
    api_key_headers: set[str] = set()
    api_key_query: set[str] = set()
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
                api_key_headers.add(name_)
            elif in_ == "query":
                api_key_query.add(name_)
    return DeclaredSchemes(
        bearer=has_bearer,
        basic=has_basic,
        api_key_headers=frozenset(api_key_headers),
        api_key_query=frozenset(api_key_query),
    )


def _filter_cascade_by_spec(
    cascade: list[AuthPattern],
    schemes: dict[str, Any],
    include_query_auth: bool,
) -> tuple[list[AuthPattern], list[str]]:
    """Filter the auth cascade to patterns the spec actually declares.

    Each entry answers for itself via its `keep_when` predicate, so a pattern
    added to the cascade is filtered correctly without a second edit here.

    Implements the prefer-header-automatically rule: if the spec declares any
    header-based scheme, query-string patterns are dropped from the cascade even
    when --include-query-auth was set. The user gets a warning so the override
    is legible. Returns (filtered_cascade, warnings).
    """
    warnings: list[str] = []
    if not schemes:
        return cascade, warnings  # No declared schemes: keep brute-force.

    declared = _declared_schemes(schemes)

    if declared.any_header and include_query_auth:
        warnings.append(
            # Do not name an escape-hatch flag here without adding one. This
            # sentence used to end "Pass --no-prefer-header-automatically if you
            # really need both", and that flag has never existed — a documented
            # control that does not exist, emitted straight at the user
            # (DEFERRED.md failure mode 1). The policy is deliberate: probing
            # query-string auth when the spec declares a header scheme puts a
            # credential in a URL that reaches logs, proxies and CDN caches.
            "Spec declares header-based authentication; query-string patterns excluded "
            "from probe cascade despite --include-query-auth (prefer-header-automatically "
            "policy). Drop --spec to probe the full cascade, or name the query parameter "
            "in the spec's securitySchemes if the API really takes one."
        )

    filtered = [pattern for pattern in cascade if pattern.keep_when(declared, pattern)]

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


def _build_cascade(
    endpoint: str,
    token: str,
    *,
    basic_creds: str | None = None,
    include_query_auth: bool = False,
) -> list[AuthPattern]:
    """Build the ordered cascade of patterns to try.

    Order is deliberate and load-bearing: the cascade is sequential and the
    first 200 wins, so reordering changes which pattern a run reports.
    """
    cascade = [
        AuthPattern(
            name=spec.name,
            kind=spec.kind,
            key=spec.header,
            headers={spec.header: spec.value.format(token=token)},
            url=endpoint,
            keep_when=spec.keep_when,
        )
        for spec in HEADER_PATTERNS
    ]
    if basic_creds:
        cascade.append(
            AuthPattern(
                name="Basic auth",
                kind=AUTH_BASIC,
                key="Authorization",
                headers=_basic_header(basic_creds),
                url=endpoint,
                keep_when=_keep_if_basic,
            )
        )
    if include_query_auth:
        for key in QUERY_PARAM_KEYS:
            cascade.append(
                AuthPattern(
                    name=f"query ?{key}=",
                    kind=AUTH_QUERY_STRING,
                    key=key,
                    headers={},
                    url=_query_url(endpoint, key, token),
                    keep_when=_keep_if_declared_query,
                )
            )
    return cascade


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
            emit_probe(
                "GET",
                endpoint_display,
                status=winner_status,
                retrieved=report["captured_at"],
                scope="auth-discovery",
                fixture=fixture_rel,
            )
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

    if not require_positive_timeout(args.timeout, subcommand="auth"):
        return 1

    try:
        allowlist.check(args.endpoint)
    except AllowlistViolation as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    resolved_basic_creds, basic_exit = _resolve_basic_creds(args)
    if basic_exit != 0:
        return basic_exit

    # See `_slug.resolve_existing_workspace`: deriving the workspace from
    # `args.endpoint` put `auth` in a different directory from the `fetch` that
    # harvested the spec, because the API host and the spec host differ.
    workspace = args.workspace
    if not workspace:
        workspace, error = resolve_existing_workspace("auth")
        if workspace is None:
            print(error, file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"using workspace {workspace}", file=sys.stderr)
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
        cascade = _build_cascade(
            args.endpoint,
            args.token,
            basic_creds=resolved_basic_creds,  # from _resolve_basic_creds above
            include_query_auth=args.include_query_auth,
        )

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

        for pattern in cascade:
            try:
                status, resp_headers, _body = _try(client, pattern.url, pattern.headers)
            except AllowlistViolation as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            except Exception as e:
                # The message can quote the URL that failed, and a
                # `--include-query-auth` URL carries the token in its query.
                attempts.append(
                    {"name": pattern.name, "status": -1, "error": redact_text(str(e))}
                )
                continue
            attempts.append({"name": pattern.name, "status": status})
            if status == 200 and winner is None:
                winner = {
                    "name": pattern.name,
                    "kind": pattern.kind,
                    "headers": redact_headers(pattern.headers),
                    "url": redact_url(pattern.url),
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
    auth_method, security_warnings = _classify_winner((winner or {}).get("kind"))
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
    write_json(fixture_path, fixture_payload)
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
