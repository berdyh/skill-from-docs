# Probing tools

Reference inventory for the `openapi-harvest` console script and its six subcommands. Read this when you're harvesting an archetype-4 (OpenAPI-only) target and need to decide which subcommand to reach for, what its output looks like in `docs.md`, and what the security defaults actually defend against.

The tool installs once (`pip install -e ~/.claude/skills/skill-from-docs/scripts`) and is invoked from any directory as `openapi-harvest <subcommand> [WORKSPACE] [options]`. Each subcommand accepts an optional positional `WORKSPACE` path (defaults to `~/.claude/skill-from-docs/<inferred-slug>/`). All subcommands share a single config, redaction policy, host allowlist, run manifest, error contract, User-Agent, and Python ≥3.10 check.

For the worked walkthrough that exercises every subcommand against a real API, see `case-study-hetzner-openapi.md`.

---

## `fetch` — discover and parse the spec

**Purpose.** Resolve a spec URL through the renderer cascade, validate it against the OpenAPI 3.0/3.1 schema with `prance[osv]`, flatten `$ref`s, and emit a normalized `spec.json` plus a `source-map.json` sidecar that preserves original JSON Pointers for downstream provenance.

**When to reach for it.** Always, as the first subcommand in any archetype-4 harvest. Also for the magical-moment demo: `openapi-harvest fetch URL --count-endpoints` prints the operation count to stdout and exits 0, no workspace required.

**How output integrates into `docs.md`.** `consolidate` reads `raw/spec.json` and `raw/source-map.json` to emit per-tag H3 sub-sections under `## API reference`. Provenance comment shape:

```html
<!-- source: https://example.com/openapi.json
     spec-pointer: /paths/~1v1~1locations/get
     raw_file: raw/spec.json
     retrieved: 2026-05-14 -->
```

For community-mirror sources, append `mirror: unofficial`. The `spec-pointer` value uses JSON Pointer escaping (`/` → `~1`, `~` → `~0`).

**How it shows up in `handoff.json`.** Populates `content_shape_signals.has_openapi_spec`, `spec_url`, `spec_format`, `endpoint_count`, `tag_count`. Also records the spec sha256 in `manifest.json` so `quick-diff` and `validate` can detect probe-vs-spec revision drift later.

**Security defaults.** `--allow-host HOST` (repeatable) restricts where `fetch` will follow the cascade. Without it the subcommand exits 1. The mirror staleness check derives its API target from the source URL — no hardcoded hosts — so the same `--staleness-days N` flag (default 90) works portably across four built-in mirror hosts:

| Source URL pattern | Staleness API target | Style |
|---|---|---|
| `raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}` | `api.github.com/repos/{o}/{r}/commits` | `github` |
| `gitlab.com/{owner}/{repo}/-/raw/{branch}/{path}` | `gitlab.com/api/v4/projects/{owner%2Frepo}/repository/commits` | `gitlab` |
| `codeberg.org/{owner}/{repo}/raw/branch/{branch}/{path}` | `codeberg.org/api/v1/repos/{o}/{r}/commits` | `gitea` |
| `bitbucket.org/{workspace}/{repo}/raw/{branch}/{path}` | `api.bitbucket.org/2.0/repositories/{w}/{r}/commits` | `bitbucket` |

For self-hosted instances (Gitea, GitLab self-managed, Bitbucket Server, GitHub Enterprise), pass both `--staleness-api-host HOST` and `--staleness-api-style {github,gitlab,gitea,bitbucket}` to enable the check explicitly. Either flag alone exits 1 (half a configuration is more confusing than none). Unknown hosts with no explicit flags get a one-line stderr note naming the flags that would enable the check — the harvest continues, just without the staleness warning.

The derived API host is added to a **function-local** allowlist for the single staleness call only; global `--allow-host` does not widen this scope, so the attack surface stays narrow regardless of which mirror host the spec came from. Header-based staleness detection is *not* used (HTTP `Last-Modified` on raw mirror URLs is CDN-cache noise, not commit date).

---

## `auth` — confirm the working auth pattern

**Purpose.** Walk a header-only cascade against a known-good GET endpoint to identify which auth pattern the API actually accepts. Capture the unauthenticated baseline, a fixed bad-token 401, and the success-response headers. Output is markdown ready to paste under `docs.md`'s `## Authentication` section.

**When to reach for it.** When the spec names a security scheme but you don't know whether the live API expects `Authorization: Bearer X`, `Authorization: Token X`, `X-API-Key: X`, or one of the less common variants. Run this once with a real token before any `probe` calls.

**How output integrates into `docs.md`.** Markdown body lands in `## Authentication`. Probe provenance comment carries `scope: auth-discovery`:

```html
<!-- probe: GET https://api.example.com/v1/me
     status: 200
     retrieved: 2026-05-14
     scope: auth-discovery
     fixture: probes/auth-discovery.json -->
```

The captured 401 envelope (from the fixed bad-token call) also feeds `## Errors`.

**How it shows up in `handoff.json`.** Populates the `coverage_checklist.Authentication` entry. The captured fixture lands in `provenance_index` under the relevant H2 with `type: probe` and `scope: auth-discovery`. The winning pattern is classified into one of five `auth_method` values — `bearer`, `auth_token_header`, `api_key_header`, `basic`, `query_string` — and surfaced as `content_shape_signals.auth_method`. Any policy warnings (e.g., "query-string credentials leak into logs") flow into `content_shape_signals.security_warnings`. **skill-creator reads these signals to decide what the generated integration skill must warn users about and how it should load credentials.**

**Auth-method policy.** Four rules govern what the cascade tries and how the result flows downstream:

| Auth method | Cascade behavior | Generated-skill guidance |
|---|---|---|
| **Bearer / API-key header** | Enabled by default, preferred. Bearer is first in the cascade. Short-circuit on first 200. | No special guidance — this is the safe default. |
| **Query-string auth** | Opt-in via `--include-query-auth`. When the spec is provided (`--spec`) and declares any header-based scheme, query patterns are dropped automatically with a stderr note ("prefer-header-automatically"). | If query auth wins, the markdown report emits a `## Security guidance` block and `handoff.json` carries a warning that the generated skill MUST surface to users (logs / proxies / CDN caches / browser history leakage). |
| **Basic auth** | Opt-in via `--basic-creds USER:PASS` (stderr warning recommends switching) or, preferred, `--basic-creds-env VARNAME` (reads `USER:PASS` from the named env var — no shell-history exposure). | If Basic wins, the markdown report and `handoff.json` direct skill-creator to load credentials from environment variables in the generated integration skill, never hardcoded. |
| **Both supported by docs** | Pass `--spec PATH` and the cascade is filtered to declared `securitySchemes` only, with header-based always preferred over query-string. | The fixture manifest records which schemes the spec declared so skill-creator can pick the safer one. |

**Security defaults.** `--allow-host HOST` is required; endpoint host must match. Cascade is header-only by default. Query-string auth patterns (`?api_key=`, `?token=`, etc.) require `--include-query-auth` because the URL reaches logs, proxies, caches, and fixture files. `Authorization: Basic` requires `--basic-creds USER:PASS` (CLI; warns) or `--basic-creds-env VARNAME` (env var; preferred) to opt in. The 401-capture probe uses the literal string `aaaaaaaa-bad-token-bbbbbbbb` rather than deriving from the real token, so the real token cannot leak via the bad-token call. Redirects are blocked by default. Default redaction (auth headers, sensitive body keys, `Set-Cookie`, `Location`, sensitive URL query keys) applies to the saved fixture and to the markdown output (URL query strings carrying `api_key=`/`token=`/etc. are redacted before display).

---

## `probe` — capture one live response

**Purpose.** Make a single HTTP call against a live endpoint, capture the request/response as a fixture file, and label it with an evidence scope. The fixture becomes a citable artifact in `docs.md` and a comparison target for `quick-diff`.

**When to reach for it.** When you want a real response shape, real headers (especially headers the spec body schema can't represent — `Link`, `RateLimit-*`, `Retry-After`), or evidence that a documented endpoint actually behaves as advertised. Run it once per endpoint you care about; three is usually enough to surface the dominant drift patterns.

**How output integrates into `docs.md`.** Each captured fixture appears as a sibling provenance comment alongside the matching endpoint's spec source:

```html
<!-- probe: GET https://api.example.com/v1/locations
     status: 200
     retrieved: 2026-05-14
     scope: case-study
     fixture: probes/locations-200.json -->
```

Two distinct provenance shapes — spec-source and probe-source — sit side by side under the same endpoint H4. The downstream `skill-creator` verifier reads both but applies different trust levels.

**How it shows up in `handoff.json`.** Populates `provenance_index.<section>.probes[]`. Each probe entry carries `method`, `url`, `status`, `scope`, `fixture`. Scope labels: `case-study`, `drift-validation`, `auth-discovery`, `ad-hoc`.

**Security defaults.** `--allow-host HOST` required. Default redaction covers request headers (`Authorization`, `X-API-Key`, `X-Auth-Token`, `Api-Key`, `Token`, `Cookie`, `X-CSRF-Token`), URL query strings on credential-suggestive keys, response headers (same list plus `Set-Cookie`, `Location`), and structured body keys on both the request and the response (JSON and form-encoded) (`token`, `api_key`, `apiKey`, `secret`, `password`, `private_key`, `access_token`, `refresh_token`, `session`). Redacted content is what gets saved to disk and what `--dry-run` prints — real values never touch the filesystem under default policy. Request bodies are structured before redaction — JSON is parsed, and `application/x-www-form-urlencoded` is split into key/value pairs so an OAuth2 token request (`grant_type=password&client_secret=...`) gets the same key-based redaction as JSON. A body that is neither is stored as text, where only pattern-redaction (`--redact-body-pattern`) reaches it. `--no-redact` is opt-out and only documented for non-sensitive shared examples. Redirects are never followed; the `Location` header is captured (redacted) instead. `--no-follow-redirects` is accepted for compatibility and states that guarantee rather than toggling it. On 429 with `Retry-After`, retry up to `--max-retries` times (default 3). On 5xx, exponential backoff (1s, 2s, 4s).

---

## `quick-diff` — surface spec-vs-reality drift

**Purpose.** Compare a probe fixture against the spec's schema for that endpoint. Report high-signal drift — fields present in the response but absent in the spec, required spec fields missing from the response, type mismatches, placeholder values, response headers the spec body schema can't represent, and spec-revision mismatches.

**When to reach for it.** After capturing a probe, before re-consolidating. The diff output is what populates `docs.md`'s `## Gotchas` section.

**How output integrates into `docs.md`.** Markdown report lands in `## Gotchas`. Each finding cites both the spec pointer and the probe fixture:

```html
<!-- source: https://example.com/openapi.json
     spec-pointer: /paths/~1v1~1locations/get/responses/200
     raw_file: raw/spec.json
     retrieved: 2026-05-14 -->
<!-- probe: GET https://api.example.com/v1/locations
     status: 200
     scope: case-study
     fixture: probes/locations-200.json -->
```

**How it shows up in `handoff.json`.** Findings land in `coverage_checklist.Gotchas` with both `sources` (spec pointer) and `probes` (fixture path) entries. Any spec-vs-probe revision mismatch (probe captured against an older spec sha) is surfaced as a warning in `validate`'s output.

**Security defaults.** No outbound network calls; operates against local files. `--strict` promotes findings to exit 1 (for CI); default is report-only, exit 0. Explicit non-goals (linked here so consumers don't expect them): content negotiation, `allOf`/`oneOf`/`anyOf` resolution, `nullable` semantics across the full schema graph, `additionalProperties`, full path templating, status-code families. For systematic schema validation use **Schemathesis** (not bundled).

---

## `consolidate` — emit `docs.md` and `handoff.json`

**Purpose.** Walk the workspace (`raw/`, `probes/`, `narrative/`) and merge everything into a single `docs.md` and `handoff.json`. The docs file uses `doc-template.md`'s canonical H2s with per-tag H3 sub-sections under `## API reference` for archetype 4. Both provenance shapes (spec and probe) appear inline at the section boundary where their evidence applies.

**When to reach for it.** Twice, usually. Once after `fetch` + narrative collection (before any probes), to confirm the spec-only harvest is complete. Then again after `auth` + `probe` + `quick-diff`, with `--merge-probes`, to fold the captured evidence in.

**How output integrates into `docs.md`.** It *is* `docs.md`. Section ordering is fixed by `doc-template.md`. Merge precedence is deterministic: probes win for response shape (reality), spec wins for parameter docs (contract), narrative wins for prose, spec wins for endpoint signatures. The `--tag` filter restricts which spec tags are included; probes outside the filter emit a warning and are excluded. Partial-coverage tags get `<!-- TODO: no probe captured for tag X -->` markers and corresponding `gap_list` entries.

**How it shows up in `handoff.json`.** Emits the file. Populates every field: `proposed_name`, `tool_summary`, `archetype_primary: 4`, all five `content_shape_signals`, `coverage_checklist` per H2, `provenance_index` per H2 + H3 with `sources` and `probes` on separate keys, `gap_list`, `image_inventory` (empty for archetype 4), `harvest_metadata`.

**Security defaults.** Prompt-injection guard runs by default on every spec-derived `description` and every community-mirror narrative source. Escapes `<!--` and `-->` (prevents fake provenance / TODO injection). Escapes leading `#` at line start (prevents fake-heading injection). Detects agent-instruction patterns (`(?i)you are (an? )?(assistant|ai|model|claude)`, `ignore (previous|prior) instructions`, `<system>`, `</?role>`) and strips them with a stderr warning naming the source pointer. `--no-sanitize-descriptions` disables the guard; documented as a security risk for untrusted specs.

---

## `validate` — local-by-default completion check

**Purpose.** Verify the workspace is internally consistent and ready for handoff. Local-syntax checks always run; network checks (re-fetching every `<!-- source: -->` URL) are opt-in.

**When to reach for it.** Before invoking `skill-creator`. Also useful in CI: `--strict --json` emits a machine-readable result.

**How output integrates into `docs.md`.** It doesn't; `validate` is read-only. But every failure points at a specific line in `docs.md` or a specific file in `raw/` or `probes/`, so the fix path is mechanical.

**How it shows up in `handoff.json`.** `validate` reads `handoff.json` and verifies it against the workspace. Failures it catches: every H2 + H3 section has a `<!-- source: -->` or `<!-- probe: -->` comment or carries `_Not documented upstream._`; every `<!-- TODO -->` marker has a matching `gap_list` entry; every file in `raw/` and `probes/` is referenced by some provenance comment (orphan-capture detection); every provenance comment's local file path resolves; every recorded manifest hash matches the file's current sha256; for archetype 4, `has_openapi_spec` is true, `spec_url` non-empty, `endpoint_count` ≥ 1.

**Security defaults.** Local-only by default — no network calls. `--network` re-fetches every source URL and verifies HTTP 200 with matching content-type; it **requires `--allow-host HOST`** (repeatable, non-empty), because the URL it fetches is read from `handoff.json`, which `validate` did not produce. `--strict` promotes warnings to errors (for CI consumers).

---

## Composition examples

Four typical sequences, each end-to-end.

**1. Auth-first, then spec-and-narrative-only** (the first run, when a token is available):

```bash
openapi-harvest fetch SPEC_URL --allow-host raw.githubusercontent.com
openapi-harvest auth ENDPOINT_URL --token $TOKEN --allow-host api.example.com
# (manually fetch narrative siblings into narrative/)
openapi-harvest consolidate
openapi-harvest validate
```

Use when you want the auth section right and the rest spec-derived. Skip probes if the spec is trusted.

**2. Drift validation before completeness check** (the rigorous run):

```bash
openapi-harvest fetch SPEC_URL --allow-host raw.githubusercontent.com
openapi-harvest consolidate                                    # spec only
openapi-harvest probe ENDPOINT_URL --scope drift-validation --allow-host api.example.com -H "Authorization: Bearer $TOKEN"
# (repeat probe for each endpoint of interest)
openapi-harvest quick-diff probes/EP-200.json raw/spec.json
openapi-harvest consolidate --merge-probes                     # fold captures in
openapi-harvest validate
```

Use when you suspect spec drift and want the `## Gotchas` section populated with evidence rather than speculation.

**3. Quick orientation only** (the 30-second demo):

```bash
openapi-harvest fetch SPEC_URL --allow-host SPEC_HOST --count-endpoints
```

Prints the operation count. Exits 0. No workspace required, no token, no narrative. Use when deciding whether an archetype-4 harvest is worth the effort.

**4. Offline walkthrough** (no third-party account):

```bash
# Seed workspace from bundled fixtures (see case-study-hetzner-openapi.md).
cp scripts/test/fixtures/hcloud-offline/* ~/.claude/skill-from-docs/<slug>/
openapi-harvest consolidate --merge-probes
openapi-harvest validate
```

Use this as the default contributor path. CI exercises this sequence on every PR.

---

## Security model

The defaults are conservative because a poisoned spec can specify an attacker-controlled endpoint URL, and a careless probe can leak a real token into a fixture file checked into a public repo. The eight defenses:

- **Host allowlist.** Every outbound network call validates the target host against `--allow-host` (repeatable) plus the workspace's `manifest.json` `allowed_hosts` array. A poisoned spec pointing `GET /locations` at `attacker.example.com` is blocked at the call site, not after the token already leaked.
- **External `$ref` validation.** `fetch` walks every `$ref` before handing the spec to `prance`, rejecting `file://`, non-http(s) schemes, and hosts outside the allowlist — this is what stops `$ref: file:///etc/passwd` from being read server-side. Under `--no-resolve` nothing is dereferenced, so violations are downgraded to a stderr **warning** rather than exit 3; the refs are still written to `raw/spec.json` verbatim, so treat a warned spec as untrusted input for anything downstream that *does* resolve.
- **Centralised redaction.** One implementation, applied uniformly: auth-suggestive request and response headers, `Set-Cookie`, `Location`, sensitive URL query keys, and sensitive JSON body keys on **both** the request and the response. Opt-out is per-flag and per-call; defaults stay on.
- **Opt-in query-string auth and Basic.** `--include-query-auth` enables `?api_key=`, `?token=`, `?access_token=`, `?key=` in the auth cascade. `--basic-creds USER:PASS` enables `Authorization: Basic`. Both are off by default because query auth leaks tokens into logs/proxies/caches/fixtures and Basic without explicit creds shouldn't run.
- **Fixed bad-token.** The 401-envelope capture probe uses the literal string `aaaaaaaa-bad-token-bbbbbbbb`. Never derived from the real token. A bad-token probe that "looked like" the real token would let a server log correlate the two.
- **Redirect blocking.** Default ON. The `Location` header is captured but not followed automatically. A 30x → attacker host token leak is the canonical OAuth-redirect-handling failure mode.
- **Prompt-injection guard.** `consolidate` sanitizes every spec-derived `description` and every community-mirror narrative source: escape `<!--` and `-->`, escape leading `#`, detect agent-instruction patterns and strip them with a stderr warning. Disable only for trusted internal specs (`--no-sanitize-descriptions`).
- **Scope labels on probes.** Every probe fixture carries a `scope` field — `case-study`, `drift-validation`, `auth-discovery`, `ad-hoc`. The downstream `skill-creator` verifier applies stricter trust to spec-source than to probe-source, and within probe-source it can apply different trust per scope.

The seven layers compose. A probe against a poisoned spec endpoint is blocked by the host allowlist before the redaction layer even sees the request. A real token in a header survives `--no-redact` because the host allowlist already vetted the destination. The point is not that any single layer is bulletproof; the point is that defeating the system requires defeating multiple layers, and the defaults defeat themselves zero of the time.

---

## What these tools do not do

- **No OAuth flows.** Bearer / API key / Basic only. The auth cascade is header-only by default with optional query auth. OAuth requires a different model (callback URL, state parameter, redirect handling); out of scope.
- **No fuzzing.** `quick-diff` reports drift between *one* probe response and the spec. Systematic schema-conformance testing across the full operation surface needs a fuzzer. Use **Schemathesis** (https://schemathesis.readthedocs.io/); link to it from `docs.md` `## Gotchas` if relevant.
- **No mutation testing.** Probes are read-only by convention. The skill never advises running write probes; mutation-safety on a real account is the contributor's responsibility, on a sandbox account.
- **No JS / DOM rendering for spec discovery.** Regex over view-source covers the five renderers in the wild (Swagger UI, ReDoc, Stoplight, Scalar, RapiDoc). When that fails, the cascade falls through to a community mirror or asks the user; it does not spin up a headless browser. Headless browser is reserved for non-OpenAPI SPA docs (see archetype 5).
