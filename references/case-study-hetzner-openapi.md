# Case study: building `hcloud-integration` from an OpenAPI spec

A worked walkthrough of this skill running against Hetzner Cloud, the canonical archetype-4 (OpenAPI-only) example. Read this before attempting an OpenAPI harvest — it makes the spec-vs-reality distinction and the `openapi-harvest` tool concrete.

**The setup.** A user asks:

> "I want a skill for integrating Hetzner Cloud into my Python service. Docs are at https://docs.hetzner.com/cloud/. Read-only scope to start — listing locations, datacenters, and server types."

---

## 30-second cold-start

Before anything else, the magical-moment demo. One pip install, one command, an endpoint count back. No jq, no Hetzner account, no workspace setup.

```bash
pip install -e ~/.claude/skills/skill-from-docs/scripts
openapi-harvest fetch \
  https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json \
  --allow-host raw.githubusercontent.com \
  --count-endpoints
# → prints the current endpoint count
```

That's the proof-of-life. The cascade resolved the spec, `prance` flattened `$ref`s, and the operation count fell out. From here the case study walks the full harvest. The exact count intentionally is not written here; it shifts as the upstream mirror updates, and CI rejects hardcoded endpoint counts so stale numbers do not quietly haunt the docs.

---

## Why Hetzner is the canonical archetype-4 example

A handful of properties line up that make this case educational beyond Hetzner itself:

- **SPA-rendered docs.** `docs.hetzner.com/cloud/api/` is a Swagger UI shell. A naive `WebFetch` returns React boilerplate, not endpoints. The spec lives elsewhere.
- **Auto-generated spec from internal sources.** The mirror carries a broad multi-tag API surface. Field-level documentation is uneven; some descriptions are literally `"string"` placeholders.
- **Community mirror, not first-party.** The maintained OpenAPI spec is `https://github.com/MaximilianKoestler/hcloud-openapi` — a third-party project that scrapes Hetzner's internal API definitions and republishes them. The skill must mark this as `mirror: unofficial` in provenance.
- **Plain bearer auth.** `Authorization: Bearer <token>`. Token issued from the Hetzner Cloud Console under Security. A free account is enough; no payment method required for read-only operations.
- **Non-trivial error envelope.** Hetzner returns `{"error": {"code": "...", "message": "...", "details": {...}}}` for 4xx — distinct enough from generic OpenAPI error schemas that you want a captured 401 in the docs.
- **Read-only operations are safe to probe.** `GET /locations`, `GET /datacenters`, `GET /server_types` mutate nothing, return small payloads, and surface real headers including the `Link` pagination header and `RateLimit-*` headers the spec omits.

These properties combine to exercise every part of `openapi-harvest`: the renderer cascade, the unofficial-mirror provenance, the spec parser, the auth probe, the probe capture, and `quick-diff`'s drift report.

---

## Phase 1, Step 0 — Confirm inputs

Walk the four required answers. Three are clear from the prompt; one needs explicit narrowing.

- **Tool name**: Hetzner Cloud ✓
- **Entry URL**: `https://docs.hetzner.com/cloud/` ✓ — but flag: the docs site is a Swagger UI shell. The authoritative API surface lives in the community OpenAPI spec at `https://github.com/MaximilianKoestler/hcloud-openapi`. Plan to harvest both.
- **Target language**: Python (via the official `hcloud-python` reference library) plus language-agnostic REST. The spec is language-neutral so harvest covers any caller; Python is what the user will write against.
- **Scope**: read-only. Specifically `GET /locations`, `GET /datacenters`, `GET /server_types`. Three small read endpoints, no mutation, no private data.

One thing to flag back to the user: `GET /images` was *not* included in scope. Hetzner's `/images` endpoint returns a tenant's private snapshots alongside the public image catalog; capturing it under any probe risks leaking private snapshot IDs and labels into a fixture file. Drop it from the worked example. If the user later wants `/images` coverage, they can extend the workspace themselves and review the captured fixture before committing it.

The intake answers are yours to carry into `docs.md` and `handoff.json`; `manifest.json` is not where they live. What the workspace's `manifest.json` records is one append-only entry per `openapi-harvest` run — the arguments it was given, its start and finish times, and the sha256 of each file it wrote:

```json
{
  "tool_version": "0.1.0",
  "runs": [
    {
      "subcommand": "fetch",
      "args": {
        "source": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
        "no_resolve": false,
        "allow_host": ["raw.githubusercontent.com"]
      },
      "started_at": "2026-05-14T09:12:03Z",
      "finished_at": "2026-05-14T09:12:07Z",
      "inputs": [],
      "outputs": [
        {"path": "raw/spec.json", "sha256": "…"},
        {"path": "raw/source-map.json", "sha256": "…"}
      ]
    }
  ]
}
```

Every string in `args`, `inputs` and `outputs` that looks like an http(s) URL is `redact_url`'d on the way in, so a credential-bearing source URL never lands here. (That is why the *fetchable* URL lives in `raw/source-map.json` instead — this file is read-modify-written on every run and has no exemption from that walk.) The file is replaced atomically, so an interrupted run leaves the previous complete manifest rather than a truncation `validate` would report as a corrupt workspace.

Each run records the `--allow-host` set it was given. That is an **audit trail**, not a policy input: nothing reads `allow_host` back out of the manifest, and passing `--allow-host` on every subsequent subcommand is required. The manifest lives inside the workspace, so a tampered workspace that could widen its own allowlist would defeat the check it is supposed to document. A poisoned spec pointing `/locations` at an attacker-controlled host is blocked by the allowlist bound to the client issuing the call, and the manifest is how you later prove which hosts each step was permitted to reach.

---

## Phase 1, Step 1 — Discovery cascade

Archetype 4 changes the order of operations: the spec is the doc; everything else is supplementary. The cascade lives in `openapi-harvest fetch` so you don't run it by hand, but knowing it matters because each fallback labels its provenance differently.

**Step 1: direct fetch of the URL you named.**

`openapi-harvest fetch https://docs.hetzner.com/cloud/api/` GETs that URL first. It answers with HTML, not JSON or YAML, so the direct step yields nothing and the cascade moves on to the renderer step *on that same response*.

**Step 2: renderer-config regex over the docs page HTML.**

That HTML contains a Swagger UI bundle, but the `url:` parameter in `SwaggerUIBundle({...})` points at a same-origin path that 401s without a Hetzner session cookie. The regex cascade in `references/discovery.md` ("OpenAPI renderers") finds the bundle; the URL it extracts isn't fetchable from outside, so the GET of it fails and the cascade moves on.

**Step 3: common spec paths against the origin.**

```
https://docs.hetzner.com/openapi.json  → 404
https://docs.hetzner.com/openapi.yaml  → 404
https://docs.hetzner.com/swagger.json  → 404
https://docs.hetzner.com/v3/api-docs   → 404
https://docs.hetzner.com/api-docs      → 404
… and two more
```

Hetzner doesn't publish a first-party spec URL anywhere obvious. The Swagger UI on docs.hetzner.com is loaded from internal Hetzner systems and the spec path isn't externally addressable. All seven guesses miss, so `fetch` exits 1 with `could not discover an OpenAPI spec from https://docs.hetzner.com/cloud/api/`.

**Then: find a community mirror — by hand.**

This is where the tool stops and you start. There is no fourth cascade step; nothing in `openapi-harvest` searches for a mirror. The exit-1 message is the signal to go looking, and what you find you feed back to `fetch` as a new SOURCE URL. Search confirms `MaximilianKoestler/hcloud-openapi` on GitHub is the de-facto source — referenced by `hcloud-python`, `hcloud-go`, and several community SDKs. The mirror scrapes Hetzner's internal API definitions and republishes a normalized OpenAPI 3.0 document.

```
https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json
```

This is the spec the harvest uses, and `consolidate` marks it as `mirror: unofficial` in every spec-source provenance comment **automatically** — `skill-creator`'s verifier applies stricter trust to first-party spec sources than to community mirrors, and the label is what drives that decision.

The rule is mechanical: the spec came from `raw.githubusercontent.com`, while the spec's own `servers` block declares `api.hetzner.cloud`, so the two differ and the label is stamped. It is a statement of fact rather than a verdict on the maintainer — a vendor publishing its own spec to GitHub trips the same rule, because from here the two are indistinguishable. Read it as "verify this source". When either host is unknown (a local spec, or a spec with no absolute `servers` entry) the label is omitted, so its absence is not a claim of first-party provenance either.

The mirror staleness check runs automatically. `openapi-harvest fetch` recognizes the `raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>` shape, calls the GitHub commits API for the file path on `main`, and warns on stderr if the most recent commit is older than `--staleness-days` (default 90):

```
WARNING: mirror is 118 days old (threshold: 90 days, source: github)
```

For Hetzner, the mirror is usually fresh enough to ignore the warning; surface it to the user if it fires.

The same staleness check works portably for GitLab (`gitlab.com/<o>/<r>/-/raw/...`), Gitea/codeberg (`codeberg.org/<o>/<r>/raw/branch/...`), and Bitbucket (`bitbucket.org/<w>/<r>/raw/...`) mirrors with no extra flags. Self-hosted instances opt in with `--staleness-api-host HOST` + `--staleness-api-style {github,gitlab,gitea,bitbucket}`. The recognizer lives in `scripts/src/skill_from_docs/cmd_fetch.py`.

---

## Phase 1, Step 2 — Parse spec, enumerate narrative siblings

The spec parse is one command:

```bash
openapi-harvest fetch \
  https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json \
  --allow-host raw.githubusercontent.com \
  --workspace ~/.claude/skill-from-docs/api.hetzner.cloud
```

`--workspace` is not optional bookkeeping here. Left off, `fetch` derives the workspace from the *source* host and writes to `~/.claude/skill-from-docs/raw.githubusercontent.com/`, while every later `probe` against `api.hetzner.cloud` derives `~/.claude/skill-from-docs/api.hetzner.cloud/` — two workspaces, and a `consolidate` on either one missing half the harvest. Pin it once and pass the same value everywhere.

`--allow-host api.github.com` is *not* needed and would not help: the staleness check runs on its own client bound to the commits-API host, and the fetch allowlist neither widens nor restricts it.

What it writes:

- `~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json` — the normalized, `$ref`-resolved OpenAPI document. `prance[osv]` validates it against the OpenAPI 3.0 schema before saving.
- `~/.claude/skill-from-docs/api.hetzner.cloud/raw/source-map.json` — JSON Pointer → original-source map. Because `prance` flattens `$ref`s, the original pointers no longer match the resolved tree; this sidecar lets `consolidate` emit accurate provenance. Written `0o600`: it is the one workspace file that can hold a live credential, in `fetch_url`. Do not hand it on.
- `~/.claude/skill-from-docs/api.hetzner.cloud/manifest.json` — appended run record (subcommand args, started_at, finished_at, input/output hashes).

A glimpse of `source-map.json`:

```json
{
  "spec_url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
  "fetch_url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
  "spec_sha256": "ab12cd34ef56...",
  "fetched_at": "2026-05-14T09:30:00Z",
  "format": "openapi-3.0",
  "operations": {
    "/v1/locations:get": {
      "original_pointer": "/paths/~1v1~1locations/get",
      "tags": ["Locations"]
    },
    "/v1/datacenters:get": {
      "original_pointer": "/paths/~1v1~1datacenters/get",
      "tags": ["Datacenters"]
    },
    "/v1/server_types:get": {
      "original_pointer": "/paths/~1v1~1server_types/get",
      "tags": ["ServerTypes"]
    }
  }
}
```

Note the JSON Pointer escaping: `/` in a path becomes `~1` (and a literal `~` would be `~0`). The `consolidate` step renders these pointers verbatim in provenance comments; getting the escapes right is what makes the pointers actually resolvable against the original raw file.

`spec_url` and `fetch_url` are identical here because this URL carries no query string. They diverge whenever redaction has something to do — `?key=petstore` becomes `?key=<redacted>` in `spec_url`, and only `fetch_url` still names a URL you can GET. Everything downstream reads `spec_url`; `fetch_url` never leaves `raw/`, because the reader every other subcommand goes through strips it before handing the file over.

`spec_sha256` is the digest of `raw/spec.json` **as written** — `indent=2`, trailing newline, `$ref`s resolved — not of the bytes that came back from the mirror. `quick-diff` re-hashes that same file, so the two agree by construction; hashing the download instead is what used to make every `spec_revision` finding a false positive. Two consequences: the same download yields a different `spec_sha256` under `--no-resolve`, and a workspace fetched by an older version of the tool carries a body-hash that will never match, so its probes report drift that is not real until you re-run `fetch`.

The spec covers endpoints but not narrative context — what the tool is for, how to get a token, what the rate-limit policy is. Enumerate sibling narrative pages on docs.hetzner.com:

```
https://docs.hetzner.com/cloud/api/getting-started/overview/
https://docs.hetzner.com/cloud/api/getting-started/using-api/
https://docs.hetzner.com/cloud/api/getting-started/authentication/
https://docs.hetzner.com/cloud/api/getting-started/pagination/
https://docs.hetzner.com/cloud/api/getting-started/label-selector/
https://docs.hetzner.com/cloud/api/getting-started/errors/
```

These are server-rendered (not part of the Swagger UI SPA) and `WebFetch`-able. Each saves to `~/.claude/skill-from-docs/api.hetzner.cloud/raw/narrative-<slug>.md`. Their content fills the canonical H2s `consolidate` doesn't get from the spec alone: `## Installation`, `## Authentication`, `## Core concepts`, `## Rate limits, quotas, versioning`, `## Errors`.

Stage them for `consolidate` by also copying to `narrative/` (the directory `consolidate --narrative-dir` reads):

```
~/.claude/skill-from-docs/api.hetzner.cloud/narrative/
├── authentication.md
├── core-concepts.md
├── rate-limits.md
└── errors.md
```

---

## Phase 1, Step 2.5 — Image extraction (skipped)

Step 2.5 doesn't run for archetype 4. Hetzner's API docs are diagram-free; what looks like an icon is decorative. The SKILL.md preflight already routes archetype 4 around this step.

---

## Phase 1, Step 3 — Consolidate

One command merges spec, narrative, and (later) probes into `docs.md` and emits the handoff packet:

```bash
openapi-harvest consolidate ~/.claude/skill-from-docs/api.hetzner.cloud/
```

Pass the workspace. `consolidate` and `validate` take it as an **optional positional that defaults to `$PWD`**, not to the slug directory `fetch` wrote — a bare `openapi-harvest consolidate` here exits 3 with `no spec at ./raw/spec.json`.

The emitted `docs.md` uses the canonical H2s from `references/doc-template.md`, with per-tag H3s nested under `## API reference` (the archetype-4 layout note in `doc-template.md` documents this). What `consolidate` writes at the top is deliberately thin — it only knows what the spec and source map told it:

```markdown
# Hetzner Cloud

- version: 1.0.0
- retrieved: 2026-05-14T09:31:00Z
- spec_url: https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json

## Coverage status

- [x] OpenAPI spec parsed
- [x] Probes merged
```

The richer header and full coverage checklist in `references/doc-template.md` — `docs site`, `language`, `target SDK(s)`, `scope`, one line per canonical H2 — are what the *agent* is responsible for filling in on top of what `consolidate` emits, using the Phase 0 answers. `consolidate` never had those answers, so it does not invent them.

Under `## API reference`, each in-scope tag gets an H3 sub-section. Provenance lives at the H3 boundary and again per-endpoint, one comment per line:

```markdown
## API reference

### Tag: Locations

<!-- source: https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json retrieved: 2026-05-14T09:31:00Z raw_file: raw/spec.json spec-pointer: /tags/Locations -->

#### `GET /v1/locations`

**List all locations**

**Parameters:**
- `name` (query) — Filter by name.
- `page` (query) — Page number.

**Responses:**
- `200` — OK

<!-- source: https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json retrieved: 2026-05-14T09:31:00Z raw_file: raw/spec.json spec-pointer: /paths/~1v1~1locations/get -->
<!-- probe: GET https://api.hetzner.cloud/v1/locations status: 200 retrieved: 2026-05-14T09:40:00Z scope: case-study fixture: probes/get-v1-locations.json -->

### Tag: Datacenters
...

### Tag: ServerTypes
...
```

Two details worth copying exactly. The endpoint heading is an H4 with the method and path in backticks. And `mirror: unofficial` is a field the provenance emitter supports but `consolidate` does not currently set from a spec harvest — if the mirror label matters for a workspace, it has to be added deliberately, not assumed.

Two distinct provenance shapes, side by side. That separation matters: `skill-creator`'s downstream verifier applies different trust levels to spec-source claims (contract-shaped, audit-trail goes back to a JSON Pointer in the raw file) versus probe-source claims (reality-shaped, audit-trail goes back to a saved fixture with a captured-at timestamp). Mixing them, or stripping one, breaks the verifier.

Sections `consolidate` does not get from the spec — installation, narrative auth flow, rate-limit numbers, error semantics — come from the `narrative/` directory. Where narrative is missing and the spec doesn't carry equivalent content, `consolidate` writes `_Not documented upstream._` rather than inventing.

---

## Phase 1, Step 4 — Completeness check

Walk the coverage checklist against the `docs.md` `consolidate` just produced:

- **Installation** ✓ — narrative/installation.md or PyPI page for `hcloud-python` (`pip install hcloud`).
- **Authentication** ✓ — narrative covers token issuance from Hetzner Cloud Console → Security → API tokens → Generate. Token type is bearer. Header: `Authorization: Bearer <token>`.
- **Core concepts** ✓ — narrative covers projects, labels, label-selectors, pagination contract.
- **API reference** ✓ for in-scope tags (Locations, Datacenters, ServerTypes). Out-of-scope tags get `<!-- TODO: out of declared scope -->` markers and a corresponding entry in `handoff.json` `gap_list`.
- **Minimal working example** — partial. The narrative pages have curl snippets; we'd like a Python example using `hcloud-python`. Pull from the `hcloud-python` GitHub README under `examples/` and tag as community-source.
- **Errors** ✓ — error schema from spec + narrative/errors.md. The 401 envelope shape gets captured live in Step 4.5 if the user has a token.
- **Rate limits** — partial. Hetzner publishes a per-token request-rate-limit policy in narrative but doesn't document the exact header names or values. The spec doesn't model rate-limit headers at all. Step 4.5 captures real headers if a token is available; otherwise flag `<!-- TODO: rate-limit headers not documented; capture via probe when token available -->`.
- **Gotchas** — empty until Step 4.5 runs `quick-diff`. The spec-vs-reality drift report is what populates this section.

Three items are partial (minimal example, rate limits, gotchas). All three improve if the user provides a token; none block handoff if they don't.

---

## Phase 1, Step 4.5 — Optional live probing

**Skipped if no token; the offline path works against bundled fixtures (see below).** Everything in this step is additive: a captured live response upgrades a section from "spec-only, partial" to "spec + probe, validated."

If the user has a Hetzner free account, they generate a read-only token (Hetzner Cloud Console → Security → API tokens → Generate, scope = read-only) and export it:

```bash
export HCLOUD_TOKEN="..."
```

Three commands, in order.

**Confirm the auth pattern.**

```bash
openapi-harvest auth \
  https://api.hetzner.cloud/v1/locations \
  --token "$HCLOUD_TOKEN" \
  --allow-host api.hetzner.cloud \
  --workspace ~/.claude/skill-from-docs/api.hetzner.cloud
```

`auth` walks the header-only cascade — `Authorization: Bearer`, `Authorization: Token`, bare `Authorization`, `X-API-Key`, `X-Auth-Token`, `Api-Key`, `Token` header — and short-circuits on the first 200. For Hetzner, `Authorization: Bearer <token>` is the first hit. The subcommand also captures, in order:

- Unauthenticated baseline: `GET /v1/locations` with no auth header. Records status (401), body shape, `WWW-Authenticate` header verbatim.
- Fixed bad-token 401: `GET /v1/locations` with the literal string `aaaaaaaa-bad-token-bbbbbbbb`. Records the real Hetzner error envelope.
- Success-response header inspection: any of `X-RateLimit-*`, `RateLimit-*`, `Retry-After`, `X-Request-ID`, `Sunset`, `Deprecation` present in the 200 response.

The output is markdown shaped to drop into `docs.md`'s `## Authentication` section, carrying a probe provenance comment with `scope: auth-discovery`. Real token never touches the saved fixture; only the fixed bad-token does. Redaction is on by default.

**Capture three read-only endpoints.**

```bash
for ep in locations datacenters server_types; do
  openapi-harvest probe \
    https://api.hetzner.cloud/v1/$ep \
    --scope case-study \
    --allow-host api.hetzner.cloud \
    -H "Authorization: Bearer $HCLOUD_TOKEN" \
    --workspace ~/.claude/skill-from-docs/api.hetzner.cloud
done
```

Each call writes a fixture to `probes/<method>-<url-path>.json` — `probes/get-v1-locations.json`, `probes/get-v1-datacenters.json`, `probes/get-v1-server_types.json`. **The status code is not part of the name**, so a second capture of the same endpoint overwrites the first; pass `-o PATH` if you want both. Each fixture holds:

- The request method, URL, redacted headers (`Authorization: <redacted>` by default), no body.
- The response status, response headers verbatim (`Link`, `Content-Type`, `X-RateLimit-*`), and the response body. Body keys matching the default redaction list (`token`, `api_key`, `secret`, etc.) are redacted, though they don't appear in these particular responses.
- A `scope: case-study` label that propagates into the provenance comment.
- A manifest stanza recording the tool version, `captured_at`, and `spec_sha256_at_capture` — the `spec_sha256` that was in `raw/source-map.json` at the time, i.e. the digest of `raw/spec.json` as written. `quick-diff` compares it against a fresh hash of the spec file to detect "this probe was captured against an older spec revision". `validate` does not perform this check.

`--allow-host api.hetzner.cloud` is non-optional. Without it the subcommand exits 1 immediately. The hard rule: a poisoned spec or a typo could point at an attacker-controlled host; the allowlist is what prevents a token from leaking there.

**Surface drift.**

```bash
openapi-harvest quick-diff \
  ~/.claude/skill-from-docs/api.hetzner.cloud/probes/get-v1-locations.json \
  ~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json
```

`quick-diff` reports the high-signal, low-effort drift between a captured response and the spec's schema for that endpoint. For `GET /v1/locations`, two things surface for Hetzner specifically:

- **`Link` header present in response, absent in spec.** The spec describes pagination via `meta.pagination` in the response body but omits the `Link: <...>; rel="next"` header that the real API also returns. Both can be used; the header form is friendlier for streaming pagination. `quick-diff` flags this as a header-spec-can't-represent finding and the line lands in `## Gotchas`.

- **`meta.pagination.next_page` is nullable in practice but the spec marks it required-integer.** When the current page is the last page, the live API returns `"next_page": null`; the spec types this as `integer` with no `nullable: true`. `quick-diff` flags this as a type-mismatch finding. Lands in `## Gotchas`.

The `quick-diff` output is markdown, ready to paste under `## Gotchas` with a probe provenance comment. Run it for each captured endpoint; usually one or two findings per endpoint. **Pasting is the mechanism** — `consolidate` knows nothing about `quick-diff`. `## Gotchas` renders from `narrative/gotchas.md` and nothing else, so append each report there before re-consolidating:

```bash
for ep in locations datacenters server_types; do
  openapi-harvest quick-diff \
    ~/.claude/skill-from-docs/api.hetzner.cloud/probes/get-v1-$ep.json \
    ~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json \
    >> ~/.claude/skill-from-docs/api.hetzner.cloud/narrative/gotchas.md
done
```

**Re-consolidate with probes folded in.**

```bash
openapi-harvest consolidate \
  ~/.claude/skill-from-docs/api.hetzner.cloud/ \
  --merge-probes
```

This rewrites `docs.md` with each captured probe added as a sibling provenance comment under the matching endpoint's H4, and picks up whatever you appended to `narrative/gotchas.md` above. Sections that previously emitted `_Not documented upstream._` (rate limits, errors) pick up captured real-world content **only once the corresponding `narrative/rate-limits.md` and `narrative/errors.md` exist** — `consolidate` renders those H2s from the narrative directory, never from a probe fixture. The probes attach to endpoints under `## API reference`; they do not populate the prose sections.

---

## Phase 1, Step 5 — Handoff packet

`consolidate` already emitted `handoff.json` alongside `docs.md`. The file walks the workspace and surfaces every signal `skill-creator`'s interview wants pre-filled. What `consolidate` writes, in the shapes it actually writes them:

```json
{
  "version": 1,
  "proposed_name": "hetzner-cloud-integration",
  "tool_summary": "Hetzner Cloud is a public cloud provider with a REST API for managing servers, volumes, networks, load balancers, and related resources.",
  "user_declared_scope": "case-study",
  "user_declared_languages": [],
  "archetype_primary": 4,
  "content_shape_signals": {
    "has_openapi_spec": true,
    "spec_url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
    "spec_format": "openapi-3.0",
    "endpoint_count": "<count from this workspace's spec>",
    "tag_count": "<count from this workspace's spec>"
  },
  "coverage_checklist": [
    {"name": "Installation", "status": "missing", "sources": ["https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json"]},
    {"name": "Authentication", "status": "covered", "sources": ["https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json"]},
    {"name": "API reference", "status": "covered", "sources": ["https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json"]},
    {"name": "Minimal working example", "status": "partial", "sources": ["https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json"]}
  ],
  "gap_list": [
    {"line": 72, "text": "<!-- TODO: provide a minimal working example -->"}
  ],
  "provenance_index": {
    "API reference > Tag: Locations > GET /v1/locations": {
      "sources": [{"type": "spec", "url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json", "pointer": "/paths/~1v1~1locations/get", "raw_file": "raw/spec.json"}],
      "probes": [{"method": "GET", "url": "https://api.hetzner.cloud/v1/locations", "status": 200, "scope": "case-study", "fixture": "probes/get-v1-locations.json"}]
    }
  },
  "image_inventory": [],
  "suggested_test_cases": [
    {"trigger_phrase": "List all locations via Hetzner Cloud", "endpoint": "GET /v1/locations", "status": "suggestion"},
    {"trigger_phrase": "use Hetzner Cloud", "endpoint": null, "status": "suggestion"},
    {"trigger_phrase": "integrate with Hetzner Cloud", "endpoint": null, "status": "suggestion"}
  ],
  "harvest_metadata": {
    "retrieved_date": "2026-05-14",
    "tool_version": "0.1.0",
    "raw_page_count": 7,
    "docs_md_token_count": 8123
  }
}
```

Five OpenAPI signals are populated: `has_openapi_spec`, `spec_url`, `spec_format`, `endpoint_count`, `tag_count`. The two counts are written by `consolidate` from the parsed spec; the case study avoids hardcoding them because the actual numbers shift as the upstream mirror updates. `provenance_index` carries `sources` (spec) and `probes` (reality) on separate keys so the downstream verifier can apply different trust levels — but `probes` stays empty unless `consolidate` was passed `--merge-probes`.

Four fields are worth knowing the derivation of, because `consolidate` fills them mechanically rather than from your Phase 0 answers:

- `proposed_name` is `<slugified info.title>-integration`. Punctuation in the spec title survives the slug, so check it.
- `tool_summary` is the spec's `info.description`, truncated to 1024 characters. Nothing is written if the spec has none.
- `user_declared_scope` is lifted from the first non-`ad-hoc` `--scope` recorded in `manifest.json` by an earlier `probe` run — so it holds a probe-scope label like `case-study`, **not** the integration scope the user described in Phase 0. `user_declared_languages` comes only from a non-standard `info.x-language` and is otherwise `[]`.
- `gap_list` is derived from `<!-- TODO` markers in `docs.md`, one `{line, text}` entry each.

The Phase 0 answers are the harvesting agent's to write into this file. `consolidate` cannot know them, and where it cannot, it leaves the field empty rather than inventing one.

Stop here. The harvest is complete. What `skill-creator` does with this packet — what the resulting integration skill's name is, how its body is structured, which test cases land in the trigger description — is not this skill's call.

---

## Done-checkpoint

Verify the workspace passes local validation:

```bash
openapi-harvest validate ~/.claude/skill-from-docs/api.hetzner.cloud/
```

Expected output — `verdict: pass`, followed by a `Pass: N/N, warn: 0, fail: 0` summary and one line per check:

```
workspace: /home/you/.claude/skill-from-docs/api.hetzner.cloud
verdict:   pass
Pass: <n>/<n>, warn: 0, fail: 0
  OK   docs_md_exists
  OK   handoff_json_valid
  ...
```

The check count is not a fixed number and no doc should claim one: `validate` emits one check per `docs.md` section, so it grows with the spec's tag and endpoint count and with how much narrative got merged. Read the count off the command; do not compare it to a number written down somewhere.

If `validate` exits 1, the message names the failing check (orphan TODO, missing provenance, manifest hash mismatch, etc.) and points at the line in `docs.md` or the file in `raw/` or `probes/` that needs attention. Fix and re-run. `verdict: warn` exits 0 — the canonical cause is an unreferenced fixture in `probes/`, which is what `consolidate` without `--merge-probes` leaves behind.

`validate --network` additionally re-fetches **one** URL — the spec URL out of `handoff.json` — and checks only that it returns HTTP 200. It does not walk `docs.md`'s `<!-- source: -->` comments, does not re-fetch the narrative pages, and does not check content types. It requires `--allow-host`, since the URL it GETs comes out of a workspace file rather than the command line:

```bash
openapi-harvest validate ~/.claude/skill-from-docs/api.hetzner.cloud/ \
  --network --allow-host raw.githubusercontent.com
```

The URL it prints is the redacted `spec_url`; the URL it GETs is `raw/source-map.json`'s `fetch_url`, which is never printed. A workspace harvested before `fetch_url` existed records no fetchable URL, and the check then **skips with an explanation, as a passing check** — that is deliberate, because a skip that failed the verdict would be a false failure in a new place.

---

## Offline mode walkthrough

The case study works without a Hetzner account. Bundled fixtures at `scripts/test/fixtures/hcloud-offline/` carry pre-captured probe responses, a snapshot of the spec, and the matching source-map. This is the default contributor path — anyone can complete the walkthrough end-to-end without third-party signups, and is what CI exercises on every PR.

```bash
pip install -e ~/.claude/skills/skill-from-docs/scripts

# Seed the workspace from bundled fixtures.
mkdir -p ~/.claude/skill-from-docs/api.hetzner.cloud/raw/
mkdir -p ~/.claude/skill-from-docs/api.hetzner.cloud/probes/
mkdir -p ~/.claude/skill-from-docs/api.hetzner.cloud/narrative/
cp ~/.claude/skills/skill-from-docs/scripts/test/fixtures/hcloud-offline/spec.json \
   ~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json
cp ~/.claude/skills/skill-from-docs/scripts/test/fixtures/hcloud-offline/source-map.json \
   ~/.claude/skill-from-docs/api.hetzner.cloud/raw/source-map.json
cp ~/.claude/skills/skill-from-docs/scripts/test/fixtures/hcloud-offline/*-200.json \
   ~/.claude/skill-from-docs/api.hetzner.cloud/probes/

# Optional: prove the magical-moment demo still works against the live mirror.
openapi-harvest fetch \
  https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json \
  --allow-host raw.githubusercontent.com \
  --count-endpoints

# Consolidate and validate.
openapi-harvest consolidate ~/.claude/skill-from-docs/api.hetzner.cloud/ --merge-probes
openapi-harvest validate ~/.claude/skill-from-docs/api.hetzner.cloud/
# → exit 0, verdict: pass
```

End state: the same `docs.md` and `handoff.json` shape the live walkthrough produces, minus the live-captured headers and timing data. Note what the seed above does *not* create: there are no files in `narrative/`, so `## Installation`, `## Core concepts`, `## Minimal working example`, `## Errors`, `## Rate limits, quotas, versioning` and `## Gotchas` all render as `_Not documented upstream._`, and `handoff.json` marks them `missing`. That is the correct output for this input — the offline fixtures are a spec plus three probes, not a docs harvest. Run `quick-diff` and append its report to `narrative/gotchas.md` if you want that section populated.

The live walkthrough is for contributors who want to verify their token works against a real API and capture fresh drift data. The offline walkthrough is what unblocks every other contributor.

---

## What this case study demonstrates beyond Hetzner

Three patterns transfer to any archetype-4 harvest:

1. **Discovery cascade for SPA-rendered Swagger UIs.** Direct fetch → renderer-config regex over view-source → common spec paths against the origin. Three steps; when they all miss, `fetch` exits 1 and finding a mirror is a human decision followed by a second `fetch`. The mirror path wants explicit `mirror: unofficial` provenance (which you add) and gets an automatic staleness check. Linear, Fly.io machines, and most fintech APIs follow exactly this shape.
2. **Spec-plus-narrative merge under canonical H2s.** The spec gets you per-endpoint contract content; sibling narrative pages get you the things specs never carry — auth flow, rate-limit policy, error envelope semantics, gotchas. Both feed `consolidate`, both land under `references/doc-template.md`'s canonical H2s, both carry separate provenance.
3. **Probing-as-evidence, scope-labeled.** A captured live response is not a doc page; it's a scoped artifact with its own provenance shape (`<!-- probe: METHOD URL status: N retrieved: DATE scope: LABEL fixture: PATH -->`). Treating it as a doc page collapses the trust distinction the downstream verifier relies on. Probes are optional and additive; the offline path is the contract.

The doc-template-canonical H2 layout with per-tag H3 sub-sections under `## API reference` is what `openapi-harvest consolidate` always emits for archetype 4. Other archetypes use the same H2s with different H3 organization (per-resource, per-module, per-subcommand) — the H2 contract is shared, the H3 structure adapts to the source shape.
