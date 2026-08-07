"""openapi-harvest fetch — discover, fetch, and parse an OpenAPI spec."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.parse import urljoin, urlparse

from ._http import (
    AllowlistViolation,
    DEFAULT_USER_AGENT,
    HostAllowlist,
    build_client,
    request_with_retry,
    require_allowlist,
)
from ._manifest import file_entry, now_iso, record_run, sha256_bytes
from ._redaction import redact_url
from ._slug import default_workspace


COMMON_SPEC_PATHS = (
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/v3/api-docs",
    "/api-docs",
    "/api/v1/openapi.json",
    "/spec.json",
)

# Renderer regex patterns, order matters (specific first).
RENDERER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("scalar", re.compile(r"data-url=[\"']([^\"']+)[\"']", re.IGNORECASE)),
    (
        "stoplight",
        re.compile(
            r"apiDescriptionUrl\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
        ),
    ),
    (
        "redoc",
        re.compile(r"<redoc[^>]*spec-url=[\"']([^\"']+)[\"']", re.IGNORECASE),
    ),
    (
        "rapidoc",
        re.compile(r"<rapi-doc[^>]*spec-url=[\"']([^\"']+)[\"']", re.IGNORECASE),
    ),
    (
        "swagger-ui",
        re.compile(
            r"(?:SwaggerUIBundle|swagger-ui-init|SwaggerUI)[^{]*\{[^}]*?url\s*:\s*[\"']([^\"']+)[\"']",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]

GITHUB_RAW_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)
GITLAB_RAW_RE = re.compile(
    r"^https://gitlab\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/-/raw/(?P<branch>[^/]+)/(?P<path>.+)$"
)
GITEA_RAW_RE = re.compile(
    # Gitea/Forgejo (codeberg) raw URL: /{owner}/{repo}/raw/branch/{branch}/{path}
    # Also matches /raw/commit/{sha}/{path} when contributors pin to a SHA.
    r"^https://(?P<host>codeberg\.org)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/raw/(?:branch|commit)/(?P<branch>[^/]+)/(?P<path>.+)$"
)
BITBUCKET_RAW_RE = re.compile(
    r"^https://bitbucket\.org/(?P<workspace>[^/]+)/(?P<repo>[^/]+)/raw/(?P<branch>[^/]+)/(?P<path>.+)$"
)

# Supported `--staleness-api-style` values. Each style maps to a known
# commits-API shape and date-field path inside `_extract_commit_date`.
_KNOWN_STYLES = ("github", "gitlab", "gitea", "bitbucket")


class StalenessTarget:
    """One staleness-check API target. Derived from a spec URL or supplied
    explicitly via --staleness-api-host + --staleness-api-style."""

    __slots__ = ("api_host", "api_url", "style")

    def __init__(self, api_host: str, api_url: str, style: str):
        self.api_host = api_host
        self.api_url = api_url
        self.style = style


class FetchError(Exception):
    """Internal fetch error mapped to an exit code."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fetch",
        help="discover + parse an OpenAPI spec",
        description="Fetch an OpenAPI spec from a URL, local path, or stdin (@-).",
    )
    p.add_argument("source")
    p.add_argument("-o", "--output-spec")
    p.add_argument("--output-source-map")
    p.add_argument("--no-resolve", action="store_true")
    p.add_argument("--user-agent")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--staleness-days", type=int, default=90)
    p.add_argument(
        "--staleness-api-host",
        help="Self-hosted git instance host (e.g. git.example.com). "
        "Must be paired with --staleness-api-style.",
    )
    p.add_argument(
        "--staleness-api-style",
        choices=_KNOWN_STYLES,
        help="API shape for --staleness-api-host. "
        "github = REST /repos/.../commits; gitlab = /api/v4/projects/.../commits; "
        "gitea = /api/v1/repos/.../commits; bitbucket = /2.0/repositories/.../commits.",
    )
    p.add_argument("--count-endpoints", action="store_true")
    p.add_argument("--allow-host", action="append", default=[])
    p.add_argument("--workspace")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=run)


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _parse_spec_bytes(data: bytes) -> dict[str, Any]:
    """Parse JSON or YAML bytes into a dict."""
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        pass
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(data.decode("utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except Exception as e:
        raise FetchError(f"failed to parse spec as JSON or YAML: {e}", exit_code=3)
    raise FetchError("spec is not a JSON object", exit_code=3)


def _collect_external_refs(node: Any, refs: list[str]) -> None:
    """Walk a spec graph collecting every `$ref` string value. Internal refs
    (`#/...`) are skipped — only external refs that prance would dereference
    by URL/file are returned.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                if not v.startswith("#"):
                    refs.append(v)
            else:
                _collect_external_refs(v, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_external_refs(item, refs)


def _collect_external_ref_violations(
    spec: dict[str, Any],
    *,
    allowlist: HostAllowlist,
    source_host: str | None,
) -> list[str]:
    """B3: find every `$ref` that would cause a fetch of a non-allowed URL or a
    local file. Returns one message per violation; empty means clean.

    - Internal refs (`#/...`) are always safe — never collected.
    - `file://` and any non-http(s) scheme are rejected unconditionally.
    - http/https refs must target a host in `allowlist` OR the same host as
      the source spec.

    Collecting rather than raising lets the caller choose the severity: fatal
    when the refs are about to be dereferenced, advisory under --no-resolve
    where nothing is fetched but the spec still lands on disk.
    """
    violations: list[str] = []
    refs: list[str] = []
    _collect_external_refs(spec, refs)
    for ref in refs:
        # Strip in-document anchor for host parsing.
        ref_url = ref.split("#", 1)[0]
        if not ref_url:
            # Pure fragment after stripping (shouldn't happen since '#/...' is
            # filtered out, but be safe).
            continue
        parsed = urlparse(ref_url)
        scheme = (parsed.scheme or "").lower()
        if not scheme:
            # Relative path ref to a sibling file — resolves against source.
            # If source is remote, this becomes a same-host http(s) fetch and
            # is allowed when the source host is allowed. If source is local
            # (no spec_url), prance reads a sibling file — block it.
            if source_host is None:
                violations.append(
                    f"external $ref to sibling file is not allowed when source is local: {ref!r}"
                )
            continue
        if scheme in ("file",):
            violations.append(f"external $ref uses file:// scheme (not allowed): {ref!r}")
            continue
        if scheme not in ("http", "https"):
            violations.append(f"external $ref uses non-http(s) scheme {scheme!r}: {ref!r}")
            continue
        host = (parsed.hostname or "").lower()
        if not host:
            violations.append(f"external $ref has no host: {ref!r}")
            continue
        allowed_hosts = set()
        if source_host:
            allowed_hosts.add(source_host.lower())
        # allowlist exposes its hosts via __contains__
        if host not in allowed_hosts and host not in allowlist:
            violations.append(
                f"external $ref host {host!r} is not in --allow-host: {ref!r}"
            )
    return violations


def _resolve_refs(spec: dict[str, Any]) -> dict[str, Any]:
    """Use prance to resolve $refs. On failure, return the original spec.

    NOTE: the caller MUST run `_collect_external_ref_violations` first to ensure prance
    is not given a poisoned spec with attacker-controlled `$ref` URLs (B3).
    """
    try:
        from prance import ResolvingParser  # type: ignore
    except ImportError:
        return spec
    try:
        parser = ResolvingParser(spec_string=json.dumps(spec), backend="openapi-spec-validator")
        if parser.specification is not None:
            return parser.specification  # type: ignore[return-value]
    except Exception:
        # If prance can't resolve (circular refs, validator failure), keep original.
        return spec
    return spec


def _jp_escape(s: str) -> str:
    """RFC 6901 JSON Pointer escape: `~` -> `~0`, `/` -> `~1`. (H7)"""
    return s.replace("~", "~0").replace("/", "~1")


def _build_source_map(spec: dict[str, Any], *, spec_url: str | None, sha256: str) -> dict[str, Any]:
    operations: dict[str, Any] = {}
    paths = spec.get("paths") or {}
    if isinstance(paths, dict):
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in (
                    "get", "post", "put", "delete", "patch", "head", "options", "trace"
                ):
                    continue
                if not isinstance(op, dict):
                    continue
                key = f"{path}:{method.lower()}"
                # H7: build the pointer as `/paths/` + RFC-6901-encoded raw
                # path. For `/v1/locations` this yields `/paths/~1v1~1locations/get`
                # (single `~1` for each `/`), not `/paths/~11v1~1locations/get`.
                operations[key] = {
                    "original_pointer": f"/paths/{_jp_escape(path)}/{method.lower()}",
                    "tags": op.get("tags", []),
                }
    return {
        # Redact HERE, not at each reader. This value is copied into
        # source-map.json, every `<!-- source: -->` comment in docs.md,
        # handoff.json's spec_url / provenance_index / coverage_checklist, and
        # every probe fixture's spec_url_at_capture. A spec URL carrying
        # `?api_key=` reached all of them; only cmd_quick_diff happened to
        # redact on read. One redaction at the source closes every path.
        "spec_url": redact_url(spec_url) if spec_url else spec_url,
        "spec_sha256": sha256,
        "fetched_at": now_iso(),
        "format": _detect_format(spec),
        "operations": operations,
    }


def _detect_format(spec: dict[str, Any]) -> str:
    if "openapi" in spec:
        v = str(spec["openapi"])
        return f"openapi-{v.rsplit('.', 1)[0]}" if "." in v else f"openapi-{v}"
    if "swagger" in spec:
        return f"swagger-{spec['swagger']}"
    return "unknown"


def _count_endpoints(spec: dict[str, Any]) -> int:
    n = 0
    for _path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for m in methods:
            if m.lower() in (
                "get", "post", "put", "delete", "patch", "head", "options", "trace"
            ):
                n += 1
    return n


def _try_renderers(html: str, base_url: str) -> str | None:
    """Return the first renderer-discovered spec URL, or None."""
    for _name, pat in RENDERER_PATTERNS:
        m = pat.search(html)
        if m:
            candidate = m.group(1)
            if not candidate.startswith(("http://", "https://")):
                candidate = urljoin(base_url, candidate)
            return candidate
    return None


def _discover(client, base_url: str, allowlist: HostAllowlist) -> tuple[bytes, str]:
    """Discovery cascade. Returns (spec_bytes, final_url) or raises FetchError."""
    # 1. Direct fetch.
    try:
        resp = request_with_retry(client, "GET", base_url, allowlist=allowlist, max_retries=0)
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type", "").lower()
            body = resp.content
            if any(t in ct for t in ("json", "yaml", "yml")) or _looks_like_spec(body):
                return body, base_url
            # treat as HTML — try renderers
            html = body.decode("utf-8", errors="replace")
            renderer_url = _try_renderers(html, base_url)
            if renderer_url:
                # Let AllowlistViolation propagate to the handler below — an
                # off-allowlist renderer URL is a user error worth reporting,
                # not something to fall through into common-path probing.
                allowlist.check(renderer_url)
                r2 = request_with_retry(
                    client, "GET", renderer_url, allowlist=allowlist, max_retries=0
                )
                if r2.status_code == 200:
                    return r2.content, renderer_url
    except AllowlistViolation as exc:
        raise FetchError(str(exc), exit_code=1)
    except Exception:
        # Continue to common-path probing for connection errors.
        pass

    # 2. Common spec paths against the origin.
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in COMMON_SPEC_PATHS:
        candidate = origin + path
        try:
            allowlist.check(candidate)
        except AllowlistViolation:
            continue
        try:
            resp = request_with_retry(
                client, "GET", candidate, allowlist=allowlist, max_retries=0
            )
        except Exception:
            continue
        if resp.status_code == 200 and _looks_like_spec(resp.content):
            return resp.content, candidate

    raise FetchError(
        f"could not discover an OpenAPI spec from {base_url}", exit_code=1
    )


def _looks_like_spec(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    if head.startswith(b"{"):
        return b"openapi" in body[:2048].lower() or b"swagger" in body[:2048].lower()
    if head.startswith(b"openapi") or head.startswith(b"swagger:"):
        return True
    return False


def _derive_staleness_target(source_url: str) -> StalenessTarget | None:
    """Recognize the four built-in mirror-host patterns and build a
    StalenessTarget for each. Returns None for unknown hosts; callers fall
    back to explicit --staleness-api-host + --staleness-api-style flags or
    skip the check with an actionable stderr note.
    """
    m = GITHUB_RAW_RE.match(source_url)
    if m:
        return StalenessTarget(
            api_host="api.github.com",
            api_url=(
                f"https://api.github.com/repos/{m.group('owner')}/{m.group('repo')}"
                f"/commits?path={m.group('path')}&sha={m.group('branch')}&per_page=1"
            ),
            style="github",
        )
    m = GITLAB_RAW_RE.match(source_url)
    if m:
        # GitLab requires URL-encoding the project's owner/repo as `owner%2Frepo`.
        project = f"{m.group('owner')}%2F{m.group('repo')}"
        return StalenessTarget(
            api_host="gitlab.com",
            api_url=(
                f"https://gitlab.com/api/v4/projects/{project}/repository/commits"
                f"?path={m.group('path')}&ref_name={m.group('branch')}&per_page=1"
            ),
            style="gitlab",
        )
    m = GITEA_RAW_RE.match(source_url)
    if m:
        host = m.group("host")
        return StalenessTarget(
            api_host=host,
            api_url=(
                f"https://{host}/api/v1/repos/{m.group('owner')}/{m.group('repo')}"
                f"/commits?path={m.group('path')}&sha={m.group('branch')}&limit=1"
            ),
            style="gitea",
        )
    m = BITBUCKET_RAW_RE.match(source_url)
    if m:
        return StalenessTarget(
            api_host="api.bitbucket.org",
            api_url=(
                f"https://api.bitbucket.org/2.0/repositories/{m.group('workspace')}"
                f"/{m.group('repo')}/commits?include={m.group('branch')}"
                f"&path={m.group('path')}&pagelen=1"
            ),
            style="bitbucket",
        )
    return None


def _build_explicit_target(source_url: str, host: str, style: str) -> StalenessTarget | None:
    """When auto-derivation returns None and the user passed
    --staleness-api-host + --staleness-api-style, build the target for a
    self-hosted instance. Requires the source URL to follow a recognizable
    `/owner/repo/raw/...` shape so we can extract the path components.

    The recognizer tries three common self-hosted layouts:
    - Gitea-style: /{owner}/{repo}/raw/branch/{branch}/{path}
    - Gitea-style (commit-pinned): /{owner}/{repo}/raw/commit/{sha}/{path}
    - Simple: /{owner}/{repo}/raw/{branch}/{path}  (Bitbucket Server / Stash)
    """
    parsed = urlparse(source_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 4 or parts[2] != "raw":
        return None
    owner, repo = parts[0], parts[1]
    if len(parts) >= 6 and parts[3] in ("branch", "commit"):
        branch = parts[4]
        path = "/".join(parts[5:])
    else:
        branch = parts[3]
        path = "/".join(parts[4:])
    if style == "github":
        # GitHub Enterprise; assumes /api/v3 prefix.
        api_url = (
            f"https://{host}/api/v3/repos/{owner}/{repo}/commits"
            f"?path={path}&sha={branch}&per_page=1"
        )
    elif style == "gitlab":
        project = f"{owner}%2F{repo}"
        api_url = (
            f"https://{host}/api/v4/projects/{project}/repository/commits"
            f"?path={path}&ref_name={branch}&per_page=1"
        )
    elif style == "gitea":
        api_url = (
            f"https://{host}/api/v1/repos/{owner}/{repo}/commits"
            f"?path={path}&sha={branch}&limit=1"
        )
    elif style == "bitbucket":
        api_url = (
            f"https://{host}/2.0/repositories/{owner}/{repo}/commits"
            f"?include={branch}&path={path}&pagelen=1"
        )
    else:  # pragma: no cover — argparse choices guard this
        return None
    return StalenessTarget(api_host=host, api_url=api_url, style=style)


def _extract_commit_date(payload: Any, style: str) -> str | None:
    """Walk the style-specific JSON path to the ISO 8601 commit date."""
    try:
        if style == "bitbucket":
            values = payload.get("values") if isinstance(payload, dict) else None
            if not values:
                return None
            return values[0].get("date")
        # github / gitlab / gitea all return a top-level list of commits.
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        if style == "gitlab":
            return first.get("committed_date")
        # github + gitea both nest under commit.committer.date
        return (first.get("commit") or {}).get("committer", {}).get("date")
    except (AttributeError, IndexError, TypeError):
        return None


def _parse_iso_date(date_str: str):
    """Tolerant ISO 8601 parser. Returns a timezone-aware datetime or None."""
    from datetime import datetime, timezone

    if not date_str:
        return None
    # Common shapes: "2026-05-14T10:30:00Z", "2026-05-14T10:30:00+00:00",
    # "2026-05-14T10:30:00.000Z", "2026-05-14T10:30:00.000+00:00".
    normalized = date_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_staleness(
    source_url: str,
    days: int,
    client,
    *,
    log,
    explicit_host: str | None = None,
    explicit_style: str | None = None,
) -> None:
    """Portable staleness check. Auto-derives the API target from the source
    URL's host; falls back to explicit flags for self-hosted instances; skips
    with an actionable note when neither resolves.

    Replaces the original GitHub-only logic. The derived `api_host` becomes a
    function-local allowlist for the single staleness call — global
    `--allow-host` is NOT honored here, preserving the narrowed attack surface
    the codex review imposed.
    """
    if days <= 0:
        return

    target = _derive_staleness_target(source_url)
    if target is None and explicit_host and explicit_style:
        target = _build_explicit_target(source_url, explicit_host, explicit_style)
    if target is None:
        host = urlparse(source_url).hostname or "(unknown)"
        log(
            f"NOTE: staleness check unavailable for host '{host}'. "
            "Pass --staleness-api-host HOST --staleness-api-style "
            "{github|gitlab|gitea|bitbucket} to enable the check against a "
            "self-hosted instance."
        )
        return

    # Function-local allowlist: only the derived/explicit api_host is allowed
    # for this one outbound call. Global --allow-host does not widen this scope.
    local_allowlist = HostAllowlist([target.api_host])
    try:
        local_allowlist.check(target.api_url)
    except AllowlistViolation as exc:
        log(f"staleness check: internal allowlist mismatch ({exc})")
        return

    try:
        resp = client.get(target.api_url)
    except Exception as e:
        log(f"staleness check failed ({target.style}): {e}")
        return
    if resp.status_code != 200:
        log(
            f"staleness check: {target.style} commits API returned "
            f"{resp.status_code}"
        )
        return
    try:
        payload = resp.json()
    except Exception:
        log(f"staleness check: {target.style} response was not JSON")
        return

    date_str = _extract_commit_date(payload, target.style)
    if not date_str:
        log(f"staleness check: no commit date found in {target.style} response")
        return
    commit_dt = _parse_iso_date(date_str)
    if commit_dt is None:
        log(f"staleness check: could not parse commit date {date_str!r}")
        return

    from datetime import datetime, timezone

    age_days = (datetime.now(timezone.utc) - commit_dt).days
    if age_days > days:
        log(
            f"WARNING: mirror is {age_days} days old "
            f"(threshold: {days} days, source: {target.style})"
        )


def run(args, *, log=None, transport=None) -> int:
    """Entry point for the subcommand. `log` is a printer used for stderr
    output; tests can capture it. `transport` is an httpx.MockTransport for
    tests.
    """
    if log is None:
        def log(msg: str) -> None:
            if not args.quiet:
                print(msg, file=sys.stderr)

    # --staleness-api-host and --staleness-api-style must be paired or both
    # absent. Half a configuration is more confusing than none.
    if bool(args.staleness_api_host) != bool(args.staleness_api_style):
        print(
            "ERROR: --staleness-api-host and --staleness-api-style must be "
            "passed together (or both omitted).",
            file=sys.stderr,
        )
        return 1

    workspace = args.workspace or default_workspace(args.source)
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(os.path.join(workspace, "raw"), exist_ok=True)
    allowlist = HostAllowlist(args.allow_host)

    # B2: when source is a remote URL, --allow-host is REQUIRED and must match
    # the source host. Local paths and stdin (@-) are not network calls and
    # skip the allowlist check.
    if _is_url(args.source):
        if not allowlist:
            require_allowlist(
                args.allow_host, subcommand="fetch", context="when the source is a URL"
            )
            return 1

    # 1. Read source bytes.
    started = now_iso()
    spec_url: str | None = None
    if args.source == "@-":
        body = sys.stdin.buffer.read()
    elif _is_url(args.source):
        try:
            allowlist.check(args.source)
        except AllowlistViolation as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        with build_client(
            timeout=args.timeout,
            user_agent=args.user_agent or DEFAULT_USER_AGENT,
            transport=transport,
        ) as client:
            try:
                body, spec_url = _discover(client, args.source, allowlist)
            except FetchError as fe:
                print(f"ERROR: {fe}", file=sys.stderr)
                return fe.exit_code
            except Exception as e:
                print(f"ERROR: network error: {e}", file=sys.stderr)
                return 2
            _check_staleness(
                spec_url,
                args.staleness_days,
                client,
                log=log,
                explicit_host=args.staleness_api_host,
                explicit_style=args.staleness_api_style,
            )
    else:
        if not os.path.exists(args.source):
            print(f"ERROR: file not found: {args.source}", file=sys.stderr)
            return 1
        with open(args.source, "rb") as f:
            body = f.read()
        spec_url = None

    # 2. Parse.
    try:
        spec = _parse_spec_bytes(body)
    except FetchError as fe:
        print(f"ERROR: {fe}", file=sys.stderr)
        return fe.exit_code

    # 3. Resolve $refs (optional).
    # B3: validate every external $ref BEFORE handing the spec to prance — a
    # poisoned `$ref: https://attacker.com/x` or `$ref: file:///etc/passwd`
    # would otherwise be fetched server-side.
    #
    # Validation runs even under --no-resolve. Nothing is dereferenced there, so
    # a violation is not fatal — but the spec still gets written to raw/spec.json
    # with the hostile refs intact, and downstream consumers (consolidate, the
    # user's own tooling) may resolve them later. Warn so the artifact is not
    # silently poisoned.
    source_host = urlparse(spec_url).hostname if spec_url else None
    ref_violations = _collect_external_ref_violations(
        spec, allowlist=allowlist, source_host=source_host
    )
    if not args.no_resolve:
        if ref_violations:
            print(f"ERROR: {ref_violations[0]}", file=sys.stderr)
            return 3
        spec = _resolve_refs(spec)
    elif ref_violations:
        for v in ref_violations:
            print(f"WARNING: {v}", file=sys.stderr)
        print(
            f"WARNING: {len(ref_violations)} unsafe $ref(s) preserved verbatim in the "
            "output spec because --no-resolve was set; nothing was fetched, but do not "
            "dereference this spec downstream without re-validating.",
            file=sys.stderr,
        )

    # 4. --count-endpoints short-circuit.
    if args.count_endpoints:
        print(_count_endpoints(spec))
        return 0

    # 5. Write outputs.
    out_spec = args.output_spec or os.path.join(workspace, "raw", "spec.json")
    out_map = args.output_source_map or os.path.join(workspace, "raw", "source-map.json")
    os.makedirs(os.path.dirname(out_spec) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(out_map) or ".", exist_ok=True)

    # Hash the bytes we WRITE, not the bytes we fetched. `quick-diff` re-hashes
    # raw/spec.json to detect spec drift, and that file is re-serialized here
    # (indent=2, possibly $ref-resolved), so hashing the fetched body instead
    # guaranteed a mismatch and made every spec_revision drift report a false
    # positive.
    spec_text = json.dumps(spec, indent=2) + "\n"
    sha = sha256_bytes(spec_text.encode("utf-8"))
    source_map = _build_source_map(spec, spec_url=spec_url, sha256=sha)

    with open(out_spec, "w", encoding="utf-8") as f:
        f.write(spec_text)
    with open(out_map, "w", encoding="utf-8") as f:
        json.dump(source_map, f, indent=2)
        f.write("\n")

    finished = now_iso()
    record_run(
        workspace,
        subcommand="fetch",
        args={
            "source": args.source,
            "no_resolve": args.no_resolve,
            "allow_host": sorted(args.allow_host or []),
        },
        started_at=started,
        finished_at=finished,
        outputs=[file_entry(workspace, out_spec), file_entry(workspace, out_map)],
    )

    log(f"wrote {out_spec}")
    log(f"wrote {out_map}")
    return 0
