# Probing tools

Reference inventory for the `openapi-harvest` console script and its six subcommands. Read this when you're harvesting an archetype-4 (OpenAPI-only) target and need to decide which subcommand to reach for, what its output looks like in `docs.md`, and what the security defaults actually defend against.

The tool installs once (`pip install -e ~/.claude/skills/skill-from-docs/scripts`) and is invoked from any directory as `openapi-harvest <subcommand> [options]`. All subcommands share a single redaction policy, host-allowlist enforcement, run manifest, error contract, User-Agent, and Python ≥3.10 check.

**How each subcommand finds its workspace differs.** There is no universal positional `WORKSPACE`:

| Subcommand | Workspace comes from |
|---|---|
| `fetch` | derived from `SOURCE` via the slug rule, overridable with `--workspace DIR` |
| `auth`, `probe` | the single harvested workspace under `~/.claude/skill-from-docs/`; **never** derived from the target URL. Overridable with `--workspace DIR` |
| `consolidate`, `validate` | an **optional positional** argument; **defaults to the current directory**, not to any slug path |
| `quick-diff` | none — it takes two file paths and writes no workspace artifacts |

A bare `openapi-harvest consolidate` after a `fetch` therefore walks `$PWD`, not the workspace `fetch` just wrote, and exits 3 with `no spec at ./raw/spec.json`; a bare `validate` exits 1 on a missing `docs.md`. Always pass the workspace to those two explicitly. See the composition examples below, which all do.

**`auth` and `probe` adopt a workspace; they never invent one.** They used to derive it from their own endpoint, and in an archetype-4 harvest the spec host and the live API host are different — the Hetzner walkthrough fetches from `raw.githubusercontent.com` and probes `api.hetzner.cloud`. So `fetch` populated one directory, `probe` silently populated another, and `consolidate` exited 3 on a workspace you had just filled. Now, when `--workspace` is absent, both scan `~/.claude/skill-from-docs/` for workspaces containing `raw/spec.json` — the artifact only a `fetch` creates — and:

- exactly one candidate: adopt it (named on stderr unless `--quiet`);
- none: **exit 1**, telling you to run `fetch` first or pass `--workspace DIR`;
- more than one: **exit 1**, listing every candidate.

Both refusals land before any network call, so `auth` in particular never puts a real token on the wire for a run with nowhere to write. A `probe --dry-run` writes nothing and needs no workspace at all.

**The slug identifies the project, not just the host.** `_slug.slug_from_url` is `<host>` plus up to three identifying path segments, lowercased: userinfo, port, query and fragment are dropped (a credential must never reach a directory name), as are VCS ref/view segments (`main`, `master`, `trunk`, `blob`, `raw`, `tree`, `refs`, `heads`, `tags`, GitLab's `-`) and a trailing generic spec filename (`openapi.json`, `swagger.yaml`, `index.html`, `README.md`, ...). So `https://raw.githubusercontent.com/o/r/main/openapi.json` derives `~/.claude/skill-from-docs/raw.githubusercontent.com-o-r/`, and `https://api.example.com/v1/locations` derives `~/.claude/skill-from-docs/api.example.com-v1-locations/`. `SKILL.md` Phase 0.5 spells out the same derivation step by step; there is one rule, in one place.

Workspaces harvested before that rule existed sit under the bare hostname and the new lookup will not find them. `fetch` prints a notice naming both paths when it sees one; nothing is migrated automatically.

Pass `--workspace` explicitly on every `fetch` / `auth` / `probe` in a multi-host harvest anyway, and use the same value for `consolidate` and `validate` — it is the clearest thing to read six months later. Every multi-step example in this file does; the one that does not is `fetch --count-endpoints`, which writes nothing worth finding again.

For the worked walkthrough that exercises every subcommand against a real API, see `case-study-hetzner-openapi.md`.

---

## `fetch` — discover and parse the spec

**Purpose.** Resolve a spec URL through the discovery cascade, validate it against the OpenAPI 3.0/3.1 schema with `prance[osv]`, flatten `$ref`s, and emit a normalized `spec.json` plus a `source-map.json` sidecar that preserves original JSON Pointers for downstream provenance.

**The cascade is three steps, in this order: direct → renderer → common paths.**

1. **Direct.** GET the URL you named. If the response is JSON/YAML by content-type or by sniffing, that is the spec.
2. **Renderer.** If it came back as HTML, regex the view-source for the five renderer config attributes (Scalar `data-url`, Stoplight `apiDescriptionUrl`, ReDoc `spec-url`, RapiDoc `spec-url`, Swagger UI `url:`), resolve the match against the base URL, and GET that. An off-allowlist renderer URL raises rather than falling through — that is a user error worth reporting.
3. **Common paths.** Only if both of the above came up empty: probe `/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/v3/api-docs`, `/api-docs`, `/api/v1/openapi.json`, `/spec.json` against the origin, one at a time, each capped at 5s and restricted to the origin host by a narrowed client.

If all three fail, `fetch` exits 1. There is no fourth step — no community-mirror lookup, no headless browser.

**`source-map.json` records the spec URL twice, and holds a credential.** `spec_url` is the display form — redacted, and the only spelling copied into `docs.md`, `handoff.json` or a probe fixture. `fetch_url` is the same URL verbatim, so the audit trail still names something re-fetchable after redaction has replaced a benign `?key=petstore` with `?key=<redacted>`; it is absent for a local-file harvest. Because `fetch_url` can carry a live credential, `fetch` writes the file `0o600` and **`source-map.json` must not leave the machine** — it is the one workspace artifact that is not safe to hand on. `validate --network` is its only reader.

**When to reach for it.** Always, as the first subcommand in any archetype-4 harvest. Also for the magical-moment demo: `openapi-harvest fetch URL --count-endpoints` prints the operation count to stdout and exits 0 without writing a spec, a source map or a manifest entry. (It does still create the empty workspace directory before short-circuiting; pass `--workspace` if that matters.)

**How output integrates into `docs.md`.** `consolidate` reads `raw/spec.json` and `raw/source-map.json` to emit per-tag H3 sub-sections under `## API reference`. Provenance comment shape:

```html
<!-- source: https://example.com/openapi.json
     spec-pointer: /paths/~1v1~1locations/get
     raw_file: raw/spec.json
     retrieved: 2026-05-14 -->
```

For community-mirror sources, append `mirror: unofficial`. The `spec-pointer` value uses JSON Pointer escaping (`/` → `~1`, `~` → `~0`).

**How it shows up in `handoff.json`.** Populates `content_shape_signals.has_openapi_spec`, `spec_url`, `spec_format`, `endpoint_count`, `tag_count`.

**`spec_sha256` is the hash of the bytes `fetch` wrote, not of the bytes it downloaded.** `source-map.json`'s `spec_sha256` covers `raw/spec.json` exactly as serialized (`indent=2`, trailing newline, `$ref`s resolved unless `--no-resolve`). It is not a checksum of the upstream response. Three consequences worth knowing:

- `quick-diff` re-hashes `raw/spec.json` and compares. Hashing the fetched body instead is what used to make every `spec_revision` finding a false positive.
- The same download produces a **different** `spec_sha256` under `--no-resolve` than under the default, because the two write different files. Comparing digests across those two modes is meaningless.
- A workspace fetched by an older version carries a hash of the *response body*, which will never match the file on disk. Any probe captured there records that body hash as `spec_sha256_at_capture`, so `quick-diff` reports a `spec_revision` finding that is not real until the workspace is re-fetched. There is no in-place migration: re-run `openapi-harvest fetch`. (`manifest.json` is unaffected — its digests have always been taken over the written file.)

**Security defaults.** `--allow-host HOST` (repeatable) restricts where `fetch` will follow the cascade, and it is required whenever the source is a URL — without it the subcommand exits 1. A local path or `@-` (stdin) issues no requests and skips the check. `--timeout` must be positive; `--timeout 0` exits 1 rather than silently skipping every discovery probe.

The mirror staleness check derives its API target from the source URL — no hardcoded hosts — so the same `--staleness-days N` flag (default 90) works portably across four built-in mirror hosts:

| Source URL pattern | Staleness API target | Style |
|---|---|---|
| `raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}` | `api.github.com/repos/{o}/{r}/commits` | `github` |
| `gitlab.com/{owner}/{repo}/-/raw/{branch}/{path}` | `gitlab.com/api/v4/projects/{owner%2Frepo}/repository/commits` | `gitlab` |
| `codeberg.org/{owner}/{repo}/raw/branch/{branch}/{path}` | `codeberg.org/api/v1/repos/{o}/{r}/commits` | `gitea` |
| `bitbucket.org/{workspace}/{repo}/raw/{branch}/{path}` | `api.bitbucket.org/2.0/repositories/{w}/{r}/commits` | `bitbucket` |

For self-hosted instances (Gitea, GitLab self-managed, Bitbucket Server, GitHub Enterprise), pass both `--staleness-api-host HOST` and `--staleness-api-style {github,gitlab,gitea,bitbucket}` to enable the check explicitly. Either flag alone exits 1 (half a configuration is more confusing than none). Unknown hosts with no explicit flags get a one-line stderr note naming the flags that would enable the check — the harvest continues, just without the staleness warning.

The staleness call runs on **its own client, bound to the derived API host alone** — not on the client that fetched the spec, whose allowlist does not (and must not) list `api.github.com`. Global `--allow-host` cannot widen it either: the allowlist is bound to the client and `GuardedClient.narrowed()` only ever restricts, so narrowing the fetch client from `{raw.githubusercontent.com}` down to `{api.github.com}` would correctly permit nothing. A second client states the same one-host policy without giving `narrowed` a widening mode. Header-based staleness detection is *not* used (HTTP `Last-Modified` on raw mirror URLs is CDN-cache noise, not commit date).

When the check fires, the stderr line is:

```
WARNING: mirror is 118 days old (threshold: 90 days, source: github)
```

Everything else the check can say is a `NOTE:`/`staleness check:` line that does not block the harvest — unknown host, non-200 from the commits API, unparseable response or commit date.

---

## `auth` — confirm the working auth pattern

**Purpose.** Walk a header-only cascade against a known-good GET endpoint to identify which auth pattern the API actually accepts. Capture the unauthenticated baseline, a fixed bad-token 401, and the success-response headers. Output is markdown ready to paste under `docs.md`'s `## Authentication` section.

**When to reach for it.** When the spec names a security scheme but you don't know whether the live API expects `Authorization: Bearer X`, `Authorization: Token X`, `X-API-Key: X`, or one of the less common variants. Run this once with a real token before any `probe` calls.

**How output integrates into `docs.md`.** Markdown body lands in `## Authentication`. Probe provenance comment carries `scope: auth-discovery`:

```html
<!-- probe: GET https://api.example.com/v1/me status: 200 retrieved: 2026-05-14 scope: auth-discovery fixture: probes/auth-api-example-com-401.json -->
```

The fixture name is `probes/auth-<host-with-dots-as-dashes>-<unauthenticated-baseline-status>.json` — the status in it is the **unauthenticated baseline**, not the winning pattern's, because that is the response the fixture body records. `auth-discovery` is the `scope` label, never a filename.

The captured 401 envelope (from the fixed bad-token call) also feeds `## Errors`.

**How it shows up in `handoff.json`.** Populates the `coverage_checklist.Authentication` entry. The captured fixture lands in `provenance_index` under the relevant H2 with `type: probe` and `scope: auth-discovery`. The winning pattern is classified into one of five `auth_method` values — `bearer`, `auth_token_header`, `api_key_header`, `basic`, `query_string` — and surfaced as `content_shape_signals.auth_method`. Any policy warnings (e.g., "query-string credentials leak into logs") flow into `content_shape_signals.security_warnings`. **skill-creator reads these signals to decide what the generated integration skill must warn users about and how it should load credentials.**

**Auth-method policy.** Four rules govern what the cascade tries and how the result flows downstream:

| Auth method | Cascade behavior | Generated-skill guidance |
|---|---|---|
| **Bearer / API-key header** | Enabled by default, preferred. Bearer is first in the cascade. Short-circuit on first 200. | No special guidance — this is the safe default. |
| **Query-string auth** | Opt-in via `--include-query-auth`. When the spec is provided (`--spec`) and declares any header-based scheme, query patterns are dropped automatically and a `NOTE:` naming the `prefer-header-automatically` policy goes to stderr *and* into `security_warnings`. The note offers a `--no-prefer-header-automatically` escape hatch **that does not exist**; the policy cannot currently be overridden. | If query auth wins, the markdown report emits a `## Security guidance` block and `handoff.json` carries a warning that the generated skill MUST surface to users (logs / proxies / CDN caches / browser history leakage). |
| **Basic auth** | Opt-in via `--basic-creds USER:PASS` (stderr warning recommends switching) or, preferred, `--basic-creds-env VARNAME` (reads `USER:PASS` from the named env var — no shell-history exposure). | If Basic wins, the markdown report and `handoff.json` direct skill-creator to load credentials from environment variables in the generated integration skill, never hardcoded. |
| **Both supported by docs** | Pass `--spec PATH` and the cascade is filtered to declared `securitySchemes` only, with header-based always preferred over query-string. | The fixture manifest records which schemes the spec declared so skill-creator can pick the safer one. |

**Security defaults.** `--allow-host HOST` is required; endpoint host must match. `--timeout` (default 10s here, not 30s) must be positive or `auth` exits 1. Cascade is header-only by default. Query-string auth patterns (`?api_key=`, `?token=`, etc.) require `--include-query-auth` because the URL reaches logs, proxies, caches, and fixture files. `Authorization: Basic` requires `--basic-creds USER:PASS` (CLI; warns) or `--basic-creds-env VARNAME` (env var; preferred) to opt in. The 401-capture probe uses the literal string `aaaaaaaa-bad-token-bbbbbbbb` rather than deriving from the real token, so the real token cannot leak via the bad-token call. Redirects are blocked by default. Default redaction (auth headers, sensitive body keys, `Set-Cookie`, `Location`, sensitive URL query keys) applies to the saved fixture and to the markdown output (URL query strings carrying `api_key=`/`token=`/etc. are redacted before display).

---

## `probe` — capture one live response

**Purpose.** Make a single HTTP call against a live endpoint, capture the request/response as a fixture file, and label it with an evidence scope. The fixture becomes a citable artifact in `docs.md` and a comparison target for `quick-diff`.

**When to reach for it.** When you want a real response shape, real headers (especially headers the spec body schema can't represent — `Link`, `RateLimit-*`, `Retry-After`), or evidence that a documented endpoint actually behaves as advertised. Run it once per endpoint you care about; three is usually enough to surface the dominant drift patterns.

**How output integrates into `docs.md`.** Each captured fixture appears as a sibling provenance comment alongside the matching endpoint's spec source:

```html
<!-- probe: GET https://api.example.com/v1/locations status: 200 retrieved: 2026-05-14 scope: case-study fixture: probes/get-v1-locations.json -->
```

The fixture name is derived from the **method and URL path** — `<method>-<path with non-alphanumerics collapsed to dashes>.json` — and carries **no status code**, so a second capture of the same endpoint overwrites the first. Pass `-o PATH` when you want two captures of one endpoint side by side. (`consolidate` emits provenance comments on one line, as shown; the parser normalizes whitespace, so a hand-written multi-line form round-trips too.)

Two distinct provenance shapes — spec-source and probe-source — sit side by side under the same endpoint H4. The downstream `skill-creator` verifier reads both but applies different trust levels.

**How it shows up in `handoff.json`.** Populates `provenance_index.<section>.probes[]`. Each probe entry carries `method`, `url`, `status`, `scope`, `fixture`. Scope labels: `case-study`, `drift-validation`, `auth-discovery`, `ad-hoc`.

**Security defaults.** `--allow-host HOST` required; `--timeout` must be positive or `probe` exits 1. Default redaction covers request headers (`Authorization`, `Proxy-Authorization`, `X-API-Key`, `X-Auth-Token`, `Api-Key`, `Token`, `Cookie`, `X-CSRF-Token`, `Set-Cookie`), URL query strings on credential-suggestive keys, response headers (same list plus `Location`), and structured body keys on both the request and the response (JSON and form-encoded): `token`, `client_secret`, `client_assertion`, `api_key`, `apiKey`, `secret`, `password`, `private_key`, `access_token`, `refresh_token`, `session`. Extend either axis per call with `--redact-body-key KEY` (repeatable) and `--redact-body-pattern REGEX` (repeatable; matched against string values *and* dict keys). Redacted content is what gets saved to disk and what `--dry-run` prints — real values never touch the filesystem under default policy. Request bodies are structured before redaction — JSON is parsed, and `application/x-www-form-urlencoded` is split into key/value pairs so an OAuth2 token request (`grant_type=password&client_secret=...`) gets the same key-based redaction as JSON. A body that is neither is stored as text, where only pattern-redaction (`--redact-body-pattern`) reaches it. `--no-redact` is opt-out and only documented for non-sensitive shared examples. Redirects are never followed; the `Location` header is captured (redacted) instead. `--no-follow-redirects` is accepted for compatibility and states that guarantee rather than toggling it. On 429 with `Retry-After`, retry up to `--max-retries` times (default 3); on 5xx, exponential backoff (1s, 2s, 4s); **transient network errors are retried on the same budget** — `probe` used to carry a local fork of the retry loop that handled 429/5xx and silently dropped the network-error case its own `--max-retries` flag advertises. Exhausting the budget against a 429/5xx exits 2.

---

## `quick-diff` — surface spec-vs-reality drift

**Purpose.** Compare a probe fixture against the spec's schema for that endpoint. Report high-signal drift — fields present in the response but absent in the spec, required spec fields missing from the response, type mismatches, placeholder values, response headers the spec body schema can't represent, and spec-revision mismatches.

**When to reach for it.** After capturing a probe, before re-consolidating. The diff output is what populates `docs.md`'s `## Gotchas` section — **but not automatically.** `consolidate` has no knowledge of `quick-diff`; `## Gotchas` is rendered from `narrative/gotchas.md` and nothing else, so a `quick-diff` report only reaches `docs.md` if you write it there:

```bash
openapi-harvest quick-diff "$WS/probes/get-v1-locations.json" "$WS/raw/spec.json" \
  >> "$WS/narrative/gotchas.md"
openapi-harvest consolidate "$WS" --merge-probes
```

The same holds for the other narrative-fed sections: `## Installation`, `## Core concepts`, `## Minimal working example`, `## Errors` and `## Rate limits, quotas, versioning` each render from `narrative/<slug>.md` — `installation`, `core-concepts`, `example`, `errors`, `rate-limits`, `gotchas` — or emit `_Not documented upstream._` when the file is absent. `## Authentication` is the exception: it falls back to the spec's `components.securitySchemes`.

**How output integrates into `docs.md`.** Markdown report is shaped to drop into `## Gotchas`. Each finding cites both the spec pointer and the probe fixture:

```html
<!-- source: https://example.com/openapi.json spec-pointer: /paths/~1v1~1locations/get/responses/200 raw_file: raw/spec.json retrieved: 2026-05-14 -->
<!-- probe: GET https://api.example.com/v1/locations status: 200 scope: case-study fixture: probes/get-v1-locations.json -->
```

**How it shows up in `handoff.json`.** Findings land in `coverage_checklist.Gotchas` with both `sources` (spec pointer) and `probes` (fixture path) entries. A spec-vs-probe revision mismatch — the probe's `spec_sha256_at_capture` differing from the current `raw/spec.json` — is reported by `quick-diff` itself, under a `## spec_revision` heading in its markdown report. `validate` does not check it.

**Security defaults.** No outbound network calls; operates against local files, so it takes no `--allow-host` and no `--timeout`. `--strict` promotes findings to exit 1 (for CI); default is report-only, exit 0. A missing fixture or spec exits 1; an unparseable one exits 3. `--source-map` is accepted by the parser but **the current implementation never reads it** — passing it changes nothing. Explicit non-goals (linked here so consumers don't expect them): content negotiation, `allOf`/`oneOf`/`anyOf` resolution, `nullable` semantics across the full schema graph, `additionalProperties`, full path templating, status-code families. For systematic schema validation use **Schemathesis** (not bundled).

---

## `consolidate` — emit `docs.md` and `handoff.json`

**Purpose.** Walk the workspace (`raw/`, `probes/`, `narrative/`) and merge everything into a single `docs.md` and `handoff.json`. The docs file uses `doc-template.md`'s canonical H2s with per-tag H3 sub-sections under `## API reference` for archetype 4. Both provenance shapes (spec and probe) appear inline at the section boundary where their evidence applies.

**When to reach for it.** Twice, usually. Once after `fetch` + narrative collection (before any probes), to confirm the spec-only harvest is complete. Then again after `auth` + `probe` + `quick-diff`, with `--merge-probes`, to fold the captured evidence in.

**How output integrates into `docs.md`.** It *is* `docs.md`. Section ordering is fixed by the canonical H2 list, which lives in `_handoff.CANONICAL_SECTIONS` and mirrors `doc-template.md`. Each source has exactly one job, and they do not compete:

- **The spec** renders `## API reference` end to end — H3 per tag, H4 per endpoint, summary, parameters, responses — and is the fallback body for `## Authentication` (`components.securitySchemes`).
- **`narrative/<slug>.md`** renders `## Installation`, `## Core concepts`, `## Minimal working example`, `## Errors`, `## Rate limits, quotas, versioning` and `## Gotchas`, plus `## Authentication` when `narrative/authentication.md` exists. Absent file ⇒ `_Not documented upstream._`.
- **Probes** add a `<!-- probe: ... -->` provenance comment beside the matching endpoint's spec comment, and populate `provenance_index[...].probes`. They do **not** rewrite the response shape the spec describes, and they do not feed any prose section.

The `--tag` filter restricts which spec tags are included; probes matching nothing in the rendered slice produce a `WARN:` line on stderr and are excluded from `docs.md`. A tag with no matching probe gets a `<!-- TODO: no probe captured for tag X -->` marker, and every `<!-- TODO` line in the finished `docs.md` becomes a `{line, text}` entry in `gap_list`.

Merge is not incremental: `consolidate` regenerates `docs.md` from the workspace every time, so anything hand-edited into it is lost on the next run. Put durable prose in `narrative/`.

**How it shows up in `handoff.json`.** Emits the file, and the emitted packet is checked against `_schema.lint_handoff` before it is written — a shape error raises here rather than surfacing downstream as a confusing interview. Shapes worth knowing, because several are not what the field names suggest:

| Field | Shape | Derived from |
|---|---|---|
| `proposed_name` | `<slugified info.title>-integration` | the spec title, punctuation and all |
| `tool_summary` | string, truncated to 1024 chars | `info.description` |
| `archetype_primary` | `4`, or `null` when no spec loaded | — |
| `content_shape_signals` | 5 keys (`has_openapi_spec`, `spec_url`, `spec_format`, `endpoint_count`, `tag_count`), plus `auth_method` + `security_warnings` when an auth-discovery fixture was merged | spec + source map |
| `coverage_checklist` | **array** of `{name, status, sources}`, one per canonical H2, `status` ∈ `covered`/`partial`/`missing` | re-parsed from the `docs.md` it just rendered — deliberately, so a renderer bug shows up as `missing` |
| `provenance_index` | object keyed `API reference > Tag: <tag>` and `API reference > Tag: <tag> > <METHOD> <path>` — **API-reference sections only**, not every H2 | spec walk + probe index |
| `gap_list` | array of `{line, text}` | `<!-- TODO` lines in `docs.md` |
| `user_declared_scope` | the first non-`ad-hoc` `--scope` in `manifest.json` — a probe-scope label, not the user's integration scope | earlier `probe` runs |
| `user_declared_languages` | `[]` unless the spec carries a non-standard `info.x-language` | spec |
| `image_inventory` | always `[]` | — |
| `harvest_metadata.docs_md_token_count` | whitespace-split word count, not a tokenizer's count | `docs.md` |

The Phase 0 answers (`user_declared_scope` in its intended sense, `user_declared_languages`) are the harvesting agent's to fill in afterwards; `consolidate` cannot know them and leaves them empty rather than guessing.

**Security defaults.** No network calls, so no `--allow-host`. The prompt-injection guard runs by default over every spec `description`, `summary` and `title`, over every narrative file, and over any spec string emitted into a heading (tag names, paths). It escapes `<!--` and `-->` (prevents fake provenance / TODO injection), escapes leading `#` at line start (prevents fake-heading injection), and detects agent-instruction patterns — `ignore|disregard (previous|prior|all) instructions`, `(?i)you are (an? )?(assistant|ai|model|claude|gpt|chatbot)`, `<system>`, `<role>`, `<instructions>` — replacing each with `[stripped]` and printing a stderr line naming the source pointer. `--no-sanitize-descriptions` disables the guard; documented as a security risk for untrusted specs.

`docs.md` and `handoff.json` are written atomically (temp file in the same directory, then `os.replace`), so an interrupted `consolidate` leaves the previous complete pair on disk rather than a truncated `docs.md` that `validate` then reports as a hash mismatch. The same applies to `manifest.json`, which every subcommand read-modify-writes.

---

## `validate` — local-by-default completion check

**Purpose.** Verify the workspace is internally consistent and ready for handoff. Local-syntax checks always run; the network check (one re-fetch of the spec URL) is opt-in.

**When to reach for it.** Before invoking `skill-creator`. Also useful in CI: `--strict --json` emits a machine-readable result.

**How output integrates into `docs.md`.** It doesn't; `validate` is read-only. But every failure points at a specific line in `docs.md` or a specific file in `raw/` or `probes/`, so the fix path is mechanical.

**How it shows up in `handoff.json`.** `validate` reads `handoff.json` and verifies it against the workspace, and sorts what it finds into three tiers.

*Blocking* (verdict `fail`, exit 1): every H2 + H3 section has a `<!-- source: -->` or `<!-- probe: -->` comment or carries `_Not documented upstream._`; every `<!-- TODO -->` marker has a matching `gap_list` entry; every provenance comment's local file path resolves; the newest recorded manifest hash for each path matches the file's current sha256; for archetype 4, `has_openapi_spec` is true and `endpoint_count` ≥ 1.

*Advisory* (verdict `warn`, exit 0): every file in `raw/` and `probes/` is referenced by some provenance comment (orphan-capture detection).

*Reported but not verdict-moving* (the `warnings` array): recommended-but-optional archetype-4 signals — `spec_url`, `spec_format`, `tag_count` — plus `provenance_index` coverage of each H3 section, plus **superseded manifest digests**. `spec_url` is legitimately absent for a local-file harvest, which is why these never fail a default run.

There is a fourth advisory check, `coverage_checklist_unknown_source`, that **cannot fire on a workspace this tool produced**: it looks for a singular `source` key on each checklist entry, and `consolidate` writes `sources` (a list). It only triggers on a hand-written `handoff.json` using the singular spelling. Do not rely on it to catch a coverage claim nothing backs.

Superseded digests deserve their own note. `validate` verifies only the *newest* recorded digest per path, so re-running `consolidate` over changed input leaves an older `docs.md` digest in `manifest.json` that no longer describes the file. That is the expected outcome of a legitimate re-run, so it is advisory: it names the earlier runs and says so in the message. Under `--strict` it blocks, which is the trade — a CI gate that wants tamper-evidence gets it, and an ordinary second `consolidate` does not turn the verdict yellow.

`--strict` promotes both lower tiers to blocking. Use it when `validate` is a CI gate. The `id` of every check is derived from a SHA-256 prefix, not Python's per-process-salted `hash()`, so ids are stable across runs and a consumer can match on them.

**Security defaults.** Local-only by default — no network calls. `--network` re-fetches one URL — the spec URL — and checks it returns HTTP 200. It **requires `--allow-host HOST`** (repeatable, non-empty), because the URL is read from workspace files `validate` did not produce. `--strict` promotes warnings to errors (for CI consumers).

Which URL it fetches is deliberate: `handoff.json`'s redacted `spec_url` supplies every id and message that `validate` prints, while the GET itself uses `raw/source-map.json`'s `fetch_url`. A redacted URL cannot be fetched, so using it would report a failure that is not real; printing the fetchable one would leak the credential the redaction exists to hide. httpx quotes the request URL in its own exception text, so even the error path runs `str(e)` through `redact_text` before printing it.

When a workspace records no usable `fetch_url` — one harvested before the field existed, or one whose two recorded URLs disagree — the check is **skipped and says so**, as a passing check that moves no verdict in either `--strict` mode. That is the point: a skip that flipped the verdict would be the same false failure in a new place.

`validate --network` re-fetches exactly **one** URL, the spec URL, and asserts only that it returns HTTP 200. It does not walk `docs.md`'s `<!-- source: -->` comments, does not re-fetch narrative pages, and does not check content types. Its timeout is a hardcoded 10s; there is no `--timeout` on this subcommand. `--network` is also the only mode that appends a run to `manifest.json`, and only when a manifest already exists — `validate` reports on a workspace, it never creates one.

---

## Composition examples

Four typical sequences, each end-to-end. Every one of them names the workspace explicitly, because `consolidate` and `validate` default to `$PWD` rather than to the slug directory `fetch` wrote — see the workspace table at the top of this file.

**1. Auth-first, then spec-and-narrative-only** (the first run, when a token is available):

```bash
WS=~/.claude/skill-from-docs/api.example.com
openapi-harvest fetch SPEC_URL --allow-host raw.githubusercontent.com --workspace "$WS"
openapi-harvest auth ENDPOINT_URL --token $TOKEN --allow-host api.example.com --workspace "$WS"
# (manually fetch narrative siblings into "$WS/narrative/")
openapi-harvest consolidate "$WS" --merge-probes
openapi-harvest validate "$WS"
```

Use when you want the auth section right and the rest spec-derived. Skip probes if the spec is trusted. `--merge-probes` is what folds `auth`'s fixture in; without it the auth-discovery capture is written but not read, and `validate` reports it as an orphan capture.

**2. Drift validation before completeness check** (the rigorous run):

```bash
WS=~/.claude/skill-from-docs/api.example.com
openapi-harvest fetch SPEC_URL --allow-host raw.githubusercontent.com --workspace "$WS"
openapi-harvest consolidate "$WS"                              # spec only
openapi-harvest probe https://api.example.com/v1/locations --scope drift-validation \
  --allow-host api.example.com -H "Authorization: Bearer $TOKEN" --workspace "$WS"
# (repeat probe for each endpoint of interest)
openapi-harvest quick-diff "$WS/probes/get-v1-locations.json" "$WS/raw/spec.json"
openapi-harvest consolidate "$WS" --merge-probes                # fold captures in
openapi-harvest validate "$WS"
```

Use when you suspect spec drift and want the `## Gotchas` section populated with evidence rather than speculation. Note the fixture path: `probe` names its output after the method and URL path, never after the status code.

**3. Quick orientation only** (the 30-second demo):

```bash
openapi-harvest fetch SPEC_URL --allow-host SPEC_HOST --count-endpoints
```

Prints the operation count. Exits 0. No token, no narrative, and nothing written but an empty workspace directory. Use when deciding whether an archetype-4 harvest is worth the effort.

**4. Offline walkthrough** (no third-party account):

```bash
# Seed workspace from bundled fixtures (see case-study-hetzner-openapi.md).
# The raw/ and probes/ split matters — consolidate reads raw/spec.json, so a
# flat copy of the fixture directory exits 3.
WS=~/.claude/skill-from-docs/api.hetzner.cloud
mkdir -p "$WS"/{raw,probes}
cp scripts/test/fixtures/hcloud-offline/{spec,source-map}.json "$WS/raw/"
cp scripts/test/fixtures/hcloud-offline/*-200.json "$WS/probes/"

# Both subcommands take the workspace positionally; without it they default to
# the current directory, not the slug path.
openapi-harvest consolidate "$WS" --merge-probes
openapi-harvest validate "$WS"
# → verdict: pass
```

Use this as the default contributor path. CI exercises this sequence on every PR
via `scripts/test/test_documented_offline_smoke.py`, so it cannot rot silently.

---

## Complete flag reference

Every flag every subcommand accepts, as of this writing. `openapi-harvest <sub> --help` is the authority; this table exists so the prose above does not have to cover each flag to make it discoverable. Positional arguments are listed first per subcommand.

### `fetch`

| Flag | Default | What it does |
|---|---|---|
| `SOURCE` (positional, required) | — | URL, local path, or `@-` for stdin. |
| `--allow-host HOST` (repeatable) | none | Required when `SOURCE` is a URL. Must name a non-empty host. |
| `--timeout SECONDS` | `30.0` | Per-request timeout for the URL you named. Must be positive; the seven speculative common-path probes are separately capped at 5s each. |
| `--workspace DIR` | slug from `SOURCE` | Where `raw/`, `manifest.json` etc. go. |
| `-q`, `--quiet` | off | Suppress progress on stderr; errors still print. |
| `-o`, `--output-spec PATH` | `<ws>/raw/spec.json` | Write the normalized spec somewhere else. |
| `--output-source-map PATH` | `<ws>/raw/source-map.json` | Write the source map somewhere else. **Still written `0o600`; still holds `fetch_url`.** |
| `--no-resolve` | off | Skip `prance` `$ref` flattening. External-`$ref` violations become warnings instead of exit 3, and the hostile refs are preserved verbatim in the output. |
| `--user-agent STRING` | `skill-from-docs/<version> (…)` | Override the User-Agent. |
| `--staleness-days N` | `90` | Warn if the mirror's newest commit is older. `0` or negative disables the check. |
| `--staleness-api-host HOST` | none | Self-hosted git instance. Must be paired with `--staleness-api-style` or `fetch` exits 1. |
| `--staleness-api-style {github,gitlab,gitea,bitbucket}` | none | Commits-API shape for the host above. |
| `--count-endpoints` | off | Print the operation count to stdout and exit 0. No spec, source map or manifest entry is written — but the empty workspace directory and its `raw/` are still created, before the short-circuit. Pass `--workspace` if you care where. |

### `auth`

| Flag | Default | What it does |
|---|---|---|
| `ENDPOINT` (positional, required) | — | A known-good GET endpoint to probe. |
| `--token TOKEN` (**required**) | — | The real token. Never written to a fixture. |
| `--allow-host HOST` (repeatable) | none | **Required.** |
| `--timeout SECONDS` | `10.0` | Note: shorter than `fetch`/`probe`, because this is a cascade against one endpoint. Must be positive. |
| `--workspace DIR` | slug from `ENDPOINT` | |
| `-q`, `--quiet` | off | |
| `-o`, `--output PATH` | stdout | Where the markdown report goes. |
| `--short-circuit` / `--no-short-circuit` | **short-circuit on** | Stop at the first pattern that returns 200, or keep going to record every pattern's status. |
| `--include-query-auth` | off | Add `?api_key=`, `?token=`, `?access_token=`, `?key=` to the cascade. Overridden by the prefer-header rule when `--spec` declares a header scheme. |
| `--basic-creds USER:PASS` | none | Enables Basic. Warns — it lands in shell history. |
| `--basic-creds-env VARNAME` | none | Preferred form. Mutually exclusive with `--basic-creds`; both set exits 1, and an unset/empty var exits 1. |
| `--spec PATH` | none | Filter the cascade to the spec's declared `securitySchemes`. If the spec declares only schemes this tool does not probe (oauth2, openIdConnect), it falls back to the full cascade with a note. |
| `--bad-token-pattern STRING` | `aaaaaaaa-bad-token-bbbbbbbb` | The literal used for the 401-envelope capture. Overriding it with anything resembling the real token defeats the point of the fixed string. |
| `--no-follow-redirects` | — | Accepted for compatibility; redirects are never followed either way. |

### `probe`

| Flag | Default | What it does |
|---|---|---|
| `URL` (positional, required) | — | |
| `--scope {case-study,drift-validation,auth-discovery,ad-hoc}` (**required**) | — | Evidence label; propagates into the provenance comment. |
| `--allow-host HOST` (repeatable) | none | **Required.** |
| `--timeout SECONDS` | `30.0` | Must be positive. |
| `--workspace DIR` | slug from `URL` | |
| `-q`, `--quiet` | off | |
| `-X`, `--method METHOD` | `GET` | |
| `-H`, `--header K:V` (repeatable) | none | Malformed (no `:`) exits 1. |
| `-d`, `--data BODY` | none | `@path` reads from a file. |
| `-o`, `--output PATH` | `<ws>/probes/<method>-<path>.json` | The only way to keep two captures of one endpoint. |
| `--no-redact` | off | Writes the fixture verbatim, credentials included. |
| `--redact-body-key KEY` (repeatable) | none | Extra body keys to redact, on top of the defaults. |
| `--redact-body-pattern REGEX` (repeatable) | none | Applied to string values **and** dict keys. |
| `--max-retries N` | `3` | Budget shared by 429, 5xx and transient network errors. |
| `--dry-run` | off | Print the redacted request as JSON and exit 0 without issuing it. |
| `--no-follow-redirects` | — | Accepted for compatibility; redirects are never followed either way. |

### `quick-diff`

| Flag | Default | What it does |
|---|---|---|
| `FIXTURE` `SPEC` (positionals, both required) | — | Paths, not a workspace. |
| `-o`, `--output PATH` | stdout | |
| `--strict` | off | Exit 1 if any drift was found. |
| `--source-map PATH` | none | **Accepted but unused** by the current implementation. |

### `consolidate`

| Flag | Default | What it does |
|---|---|---|
| `WORKSPACE` (positional, optional) | **`$PWD`** | Not the slug path. Pass it. |
| `-q`, `--quiet` | off | |
| `--merge-probes` | off | Without this, `probes/` is not read at all and every fixture shows up as an orphan capture in `validate`. |
| `--tag TAG` (repeatable) | all tags | Restrict `## API reference` to these spec tags. Probes outside the filter warn and are excluded. |
| `--narrative-dir DIR` | `<ws>/narrative/` | |
| `--emit-handoff` / `--no-emit-handoff` | **emit on** | |
| `--no-sanitize-descriptions` | guard on | Disables the prompt-injection guard. |
| `--dry-run` | off | Print `docs.md` (and `handoff.json`) to stdout; write nothing. |

### `validate`

| Flag | Default | What it does |
|---|---|---|
| `WORKSPACE` (positional, optional) | **`$PWD`** | Not the slug path. Pass it. |
| `--strict` | off | Promotes advisory checks and the `warnings` array to blocking. |
| `--network` | off | Re-fetch the one spec URL. Requires `--allow-host`. Timeout is a hardcoded 10s. |
| `--allow-host HOST` (repeatable) | none | Required by `--network` only. |
| `--json` | off | Emit the machine-readable result documented in `scripts/README.md`. |

---

## Security model

The defaults are conservative because a poisoned spec can specify an attacker-controlled endpoint URL, and a careless probe can leak a real token into a fixture file checked into a public repo. The ten defenses:

- **Host allowlist, bound to the HTTP client.** Every outbound network call validates the target host against `--allow-host` (repeatable), which is required by `fetch` (URL source), `auth`, `probe`, and `validate --network`. Enforcement is not a check each call site remembers to make: `build_client` returns a `GuardedClient` whose request event hook rejects an off-allowlist host, so it sits under `get`, `request`, `send` and every hop a redirect follower would issue. Reassigning `client.event_hooks` cannot unhook it. `client.narrowed(...)` tightens the policy for a block — the speculative common-path probes run same-origin-only that way — and is **intersection-only**: it takes a constructed `HostAllowlist`, refuses an empty one (empty means "permit everything"), and raises rather than widening to a host the enclosing policy would reject. `--allow-host` is also the *only* input: `manifest.json` records the allowlist each run was given, but nothing reads it back, because a workspace file that could widen its own allowlist would defeat the check. A poisoned spec pointing `GET /locations` at `attacker.example.com` is blocked before the request leaves the process, not after the token already leaked.
- **Proxy environment ignored.** Clients are built with `trust_env=False`, so `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` cannot route a token-bearing request through a host the allowlist never saw.
- **The one credential on disk is mode `0o600`.** `raw/source-map.json` records `fetch_url`, the spec URL verbatim; every other artifact carries only the redacted `spec_url`. See the note under `fetch` above — the file is written `0o600` (applied to the temp descriptor *before* the atomic rename, so it is never briefly world-readable), `read_source_map` strips `fetch_url` so `consolidate` and `probe` are never handed it, and `validate --network` is its only reader.
- **External `$ref` validation.** `fetch` walks every `$ref` before handing the spec to `prance`, rejecting `file://`, non-http(s) schemes, and hosts outside the allowlist — this is what stops `$ref: file:///etc/passwd` from being read server-side. Under `--no-resolve` nothing is dereferenced, so violations are downgraded to a stderr **warning** rather than exit 3; the refs are still written to `raw/spec.json` verbatim, so treat a warned spec as untrusted input for anything downstream that *does* resolve.
- **Centralised redaction.** One implementation, applied uniformly: auth-suggestive request and response headers, `Set-Cookie`, `Location`, sensitive URL query keys, and sensitive JSON body keys on **both** the request and the response. Opt-out is per-flag and per-call; defaults stay on.
- **Opt-in query-string auth and Basic.** `--include-query-auth` enables `?api_key=`, `?token=`, `?access_token=`, `?key=` in the auth cascade. `--basic-creds USER:PASS` enables `Authorization: Basic`. Both are off by default because query auth leaks tokens into logs/proxies/caches/fixtures and Basic without explicit creds shouldn't run.
- **Fixed bad-token.** The 401-envelope capture probe uses the literal string `aaaaaaaa-bad-token-bbbbbbbb`. Never derived from the real token. A bad-token probe that "looked like" the real token would let a server log correlate the two.
- **Redirect blocking.** Default ON. The `Location` header is captured but not followed automatically. A 30x → attacker host token leak is the canonical OAuth-redirect-handling failure mode.
- **Prompt-injection guard.** `consolidate` sanitizes every spec-derived `description` and every community-mirror narrative source: escape `<!--` and `-->`, escape leading `#`, detect agent-instruction patterns and strip them with a stderr warning. Disable only for trusted internal specs (`--no-sanitize-descriptions`).
- **Scope labels on probes.** Every probe fixture carries a `scope` field — `case-study`, `drift-validation`, `auth-discovery`, `ad-hoc`. The downstream `skill-creator` verifier applies stricter trust to spec-source than to probe-source, and within probe-source it can apply different trust per scope.

The ten layers compose. A probe against a poisoned spec endpoint is blocked by the host allowlist before the redaction layer even sees the request. A real token in a header survives `--no-redact` because the host allowlist already vetted the destination. The point is not that any single layer is bulletproof; the point is that defeating the system requires defeating multiple layers, and the defaults defeat themselves zero of the time.

**The one thing the layers do not cover: `raw/source-map.json` is a live credential at rest.** If the spec URL carried a credential in its query string, harvesting it put that credential on the filesystem in plaintext. Nothing rotates or expires it. A workspace is safe to hand to `skill-creator` and safe to read; it is **not** safe to copy wholesale into a repo, an archive, or a bug report. Excluding `raw/source-map.json` — or deleting it once `validate --network` has run — is the operator's call, and there is no code path that makes it for them.

---

## What these tools do not do

- **No OAuth flows.** Bearer / API key / Basic only. The auth cascade is header-only by default with optional query auth. OAuth requires a different model (callback URL, state parameter, redirect handling); out of scope.
- **No fuzzing.** `quick-diff` reports drift between *one* probe response and the spec. Systematic schema-conformance testing across the full operation surface needs a fuzzer. Use **Schemathesis** (https://schemathesis.readthedocs.io/); link to it from `docs.md` `## Gotchas` if relevant.
- **No mutation testing.** Probes are read-only by convention. The skill never advises running write probes; mutation-safety on a real account is the contributor's responsibility, on a sandbox account.
- **No JS / DOM rendering for spec discovery.** Regex over view-source covers the five renderers in the wild (Swagger UI, ReDoc, Stoplight, Scalar, RapiDoc). It does not spin up a headless browser; headless browser is reserved for non-OpenAPI SPA docs (see archetype 5).
- **No community-mirror lookup.** The cascade is three steps and stops. When all three fail, `fetch` exits 1 with `could not discover an OpenAPI spec from <url>` and finding a mirror is a judgement call the agent or the user makes, then re-runs `fetch` against the mirror URL. Nothing in the tool searches for one.
