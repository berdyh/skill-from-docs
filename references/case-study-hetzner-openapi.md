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
# → approximately 85
```

That's the proof-of-life. The cascade resolved the spec, `prance` flattened `$ref`s, and the operation count fell out. From here the case study walks the full harvest. The exact count is intentionally fuzzy here; CI regenerates it on every PR so a stale number doesn't quietly haunt the docs.

---

## Why Hetzner is the canonical archetype-4 example

A handful of properties line up that make this case educational beyond Hetzner itself:

- **SPA-rendered docs.** `docs.hetzner.com/cloud/api/` is a Swagger UI shell. A naive `WebFetch` returns React boilerplate, not endpoints. The spec lives elsewhere.
- **Auto-generated spec from internal sources.** ~85 operations across ~25 tags. Field-level documentation is uneven; some descriptions are literally `"string"` placeholders.
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

The intake answers go into `~/.claude/skill-from-docs/api.hetzner.cloud/manifest.json` so every subsequent `openapi-harvest` subcommand sees the same declared scope:

```json
{
  "tool_slug": "api.hetzner.cloud",
  "tool_name": "Hetzner Cloud",
  "entry_url": "https://docs.hetzner.com/cloud/",
  "target_languages": ["python", "language-agnostic"],
  "declared_scope": "read-only: GET /locations, GET /datacenters, GET /server_types",
  "allowed_hosts": ["api.hetzner.cloud", "raw.githubusercontent.com", "github.com", "docs.hetzner.com"]
}
```

The `allowed_hosts` list is what the host-allowlist check enforces on every subsequent outbound call. A poisoned spec that points `/locations` at an attacker-controlled host gets blocked here, not after the token already leaked.

---

## Phase 1, Step 1 — Discovery cascade

Archetype 4 changes the order of operations: the spec is the doc; everything else is supplementary. The cascade lives in `openapi-harvest fetch` so you don't run it by hand, but knowing it matters because each fallback labels its provenance differently.

**Try 1: docs.hetzner.com siblings.**

```
https://docs.hetzner.com/cloud/openapi.json     → 404
https://docs.hetzner.com/cloud/api/openapi.json → 404
https://docs.hetzner.com/cloud/api/spec.json    → 404
https://docs.hetzner.com/api-docs               → 404
```

Hetzner doesn't publish a first-party spec URL anywhere obvious. The Swagger UI on docs.hetzner.com is loaded from internal Hetzner systems and the spec path isn't externally addressable.

**Try 2: renderer-config regex over the docs page HTML.**

`openapi-harvest fetch https://docs.hetzner.com/cloud/api/` view-source contains a Swagger UI bundle, but the `url:` parameter in `SwaggerUIBundle({...})` points at a same-origin path that 401s without a Hetzner session cookie. The regex cascade in `references/discovery.md` ("OpenAPI renderers") finds the bundle but the URL it extracts isn't fetchable from outside.

**Try 3: community mirror.**

Fall through to a known maintained mirror. Search confirms `MaximilianKoestler/hcloud-openapi` on GitHub is the de-facto source — referenced by `hcloud-python`, `hcloud-go`, and several community SDKs. The mirror scrapes Hetzner's internal API definitions and republishes a normalized OpenAPI 3.0 document.

```
https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json
```

This is the spec the harvest uses. Mark it as `mirror: unofficial` in every provenance comment downstream — `skill-creator`'s verifier applies stricter trust to first-party spec sources than to community mirrors, and the label is what drives that decision.

The mirror staleness check runs automatically. `openapi-harvest fetch` recognizes the `raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>` shape, calls the GitHub commits API for the file path on `main`, and warns on stderr if the most recent commit is older than `--staleness-days` (default 90):

```
WARN: mirror staleness: hcloud.json last committed 2026-01-08 (118 days ago).
      Threshold: 90 days. Consider checking upstream for breaking changes.
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
  --allow-host api.github.com
```

What it writes:

- `~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json` — the normalized, `$ref`-resolved OpenAPI document. `prance[osv]` validates it against the OpenAPI 3.0 schema before saving.
- `~/.claude/skill-from-docs/api.hetzner.cloud/raw/source-map.json` — JSON Pointer → original-source map. Because `prance` flattens `$ref`s, the original pointers no longer match the resolved tree; this sidecar lets `consolidate` emit accurate provenance.
- `~/.claude/skill-from-docs/api.hetzner.cloud/manifest.json` — appended run record (subcommand args, started_at, finished_at, input/output hashes).

A glimpse of `source-map.json`:

```json
{
  "spec_url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
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

The emitted `docs.md` uses the canonical H2s from `references/doc-template.md`, with per-tag H3s nested under `## API reference` (the archetype-4 layout note in `doc-template.md` documents this). The header:

```markdown
# Hetzner Cloud

- **version**: v1
- **docs site**: https://docs.hetzner.com/cloud/
- **retrieved**: 2026-05-14
- **language**: en
- **target SDK(s)**: python (via hcloud-python), language-agnostic REST
- **scope**: read-only — GET /locations, GET /datacenters, GET /server_types

## Coverage status
- [x] Installation
- [x] Authentication
- [x] Core concepts
- [x] API reference (read-only scope)
- [x] Minimal working example
- [x] Errors
- [x] Rate limits, quotas, versioning
- [x] Gotchas
```

Under `## API reference`, each in-scope tag gets an H3 sub-section. Provenance lives at the H3 boundary (or per-endpoint, when probes attach):

```markdown
## API reference

### Tag: Locations

#### GET /v1/locations

List all locations.

Parameters: `name` (query, optional), `page` (query, optional, default 1),
`per_page` (query, optional, default 25, max 50), `sort` (query, optional).

Response: `{ "locations": [...], "meta": { "pagination": {...} } }`.

<!-- source: https://github.com/MaximilianKoestler/hcloud-openapi/blob/main/openapi/hcloud.json
     spec-pointer: /paths/~1v1~1locations/get
     mirror: unofficial
     raw_file: raw/spec.json
     retrieved: 2026-05-14 -->

<!-- probe: GET https://api.hetzner.cloud/v1/locations
     status: 200
     retrieved: 2026-05-14
     scope: case-study
     fixture: probes/locations-200.json -->

### Tag: Datacenters
...

### Tag: ServerTypes
...
```

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
  --allow-host api.hetzner.cloud
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
    -H "Authorization: Bearer $HCLOUD_TOKEN"
done
```

Each call writes a fixture to `probes/<endpoint>-200.json` with:

- The request method, URL, redacted headers (`Authorization: <redacted>` by default), no body.
- The response status, response headers verbatim (`Link`, `Content-Type`, `X-RateLimit-*`), and the response body. Body keys matching the default redaction list (`token`, `api_key`, `secret`, etc.) are redacted, though they don't appear in these particular responses.
- A `scope: case-study` label that propagates into the provenance comment.
- A manifest stanza recording the tool version, captured_at, and the spec sha256 at capture time. `quick-diff` and `validate` use the spec hash to detect "this probe was captured against an older spec revision."

`--allow-host api.hetzner.cloud` is non-optional. Without it the subcommand exits 1 immediately. The hard rule: a poisoned spec or a typo could point at an attacker-controlled host; the allowlist is what prevents a token from leaking there.

**Surface drift.**

```bash
openapi-harvest quick-diff \
  ~/.claude/skill-from-docs/api.hetzner.cloud/probes/locations-200.json \
  ~/.claude/skill-from-docs/api.hetzner.cloud/raw/spec.json
```

`quick-diff` reports the high-signal, low-effort drift between a captured response and the spec's schema for that endpoint. For `GET /v1/locations`, two things surface for Hetzner specifically:

- **`Link` header present in response, absent in spec.** The spec describes pagination via `meta.pagination` in the response body but omits the `Link: <...>; rel="next"` header that the real API also returns. Both can be used; the header form is friendlier for streaming pagination. `quick-diff` flags this as a header-spec-can't-represent finding and the line lands in `## Gotchas`.

- **`meta.pagination.next_page` is nullable in practice but the spec marks it required-integer.** When the current page is the last page, the live API returns `"next_page": null`; the spec types this as `integer` with no `nullable: true`. `quick-diff` flags this as a type-mismatch finding. Lands in `## Gotchas`.

The `quick-diff` output is markdown, ready to paste under `## Gotchas` with a probe provenance comment. Run it for each captured endpoint; usually one or two findings per endpoint.

**Re-consolidate with probes folded in.**

```bash
openapi-harvest consolidate \
  ~/.claude/skill-from-docs/api.hetzner.cloud/ \
  --merge-probes
```

This rewrites `docs.md` with each captured probe added as a sibling provenance comment under the matching endpoint's H4, and folds the `quick-diff` reports into `## Gotchas`. Sections it previously emitted `_Not documented upstream._` for (rate limits, errors) now pick up captured real-world content.

---

## Phase 1, Step 5 — Handoff packet

`consolidate` already emitted `handoff.json` alongside `docs.md`. The file walks the workspace and surfaces every signal `skill-creator`'s interview wants pre-filled:

```json
{
  "version": 1,
  "proposed_name": "hcloud-integration",
  "tool_summary": "Hetzner Cloud is a public cloud provider with a REST API for managing servers, volumes, networks, load balancers, and related resources. The Cloud API is bearer-authenticated and language-agnostic; an official Python reference library (hcloud-python) wraps it.",
  "user_declared_scope": "read-only: GET /locations, GET /datacenters, GET /server_types",
  "user_declared_languages": ["python", "language-agnostic"],
  "archetype_primary": 4,
  "content_shape_signals": {
    "has_openapi_spec": true,
    "spec_url": "https://raw.githubusercontent.com/MaximilianKoestler/hcloud-openapi/main/openapi/hcloud.json",
    "spec_format": "openapi-3.0",
    "endpoint_count": "<regenerated by CI>",
    "tag_count": "<regenerated by CI>",
    "top_level_h2_count": 8,
    "code_block_languages": ["bash", "json", "python"]
  },
  "coverage_checklist": {
    "Installation": {"status": "covered", "sources": ["narrative/installation.md"]},
    "Authentication": {"status": "covered", "sources": ["narrative/authentication.md", "probes/auth-discovery.json"]},
    "Core concepts": {"status": "covered", "sources": ["narrative/core-concepts.md"]},
    "API reference": {"status": "partial", "note": "in-scope tags fully covered; out-of-scope tags flagged"},
    "Errors": {"status": "covered", "sources": ["raw/spec.json", "narrative/errors.md", "probes/auth-discovery.json"]},
    "Rate limits": {"status": "covered", "sources": ["narrative/rate-limits.md", "probes/locations-200.json"]},
    "Gotchas": {"status": "covered", "sources": ["probes/locations-200.json#quick-diff"]}
  },
  "gap_list": [
    "Out-of-scope tags (Servers, Volumes, Networks, ...) intentionally excluded per declared scope.",
    "GET /images explicitly excluded from probing to prevent private-snapshot leakage."
  ],
  "provenance_index": {
    "API reference > Locations > GET /v1/locations": {
      "sources": [{"type": "spec", "url": "https://github.com/MaximilianKoestler/hcloud-openapi/blob/main/openapi/hcloud.json", "pointer": "/paths/~1v1~1locations/get", "raw_file": "raw/spec.json", "mirror": "unofficial"}],
      "probes": [{"method": "GET", "url": "https://api.hetzner.cloud/v1/locations", "status": 200, "scope": "case-study", "fixture": "probes/locations-200.json"}]
    }
  },
  "image_inventory": [],
  "suggested_test_cases": [
    "list all locations",
    "fetch a specific datacenter by ID",
    "enumerate available server types"
  ],
  "harvest_metadata": {
    "retrieved_date": "2026-05-14",
    "tool_version": "0.1.0",
    "raw_page_count": 7,
    "docs_md_token_count": 8123
  }
}
```

Five OpenAPI signals are populated: `has_openapi_spec`, `spec_url`, `spec_format`, `endpoint_count`, `tag_count`. The two counts are written by `consolidate` from the parsed spec; the case study writes them as fuzzy hints (`approximately 85`) because the actual numbers shift as the upstream mirror updates. `provenance_index` carries `sources` (spec) and `probes` (reality) on separate keys so the downstream verifier can apply different trust levels.

Stop here. The harvest is complete. What `skill-creator` does with this packet — what the resulting integration skill's name is, how its body is structured, which test cases land in the trigger description — is not this skill's call.

---

## Done-checkpoint

Verify the workspace passes local validation:

```bash
openapi-harvest validate ~/.claude/skill-from-docs/api.hetzner.cloud/
```

Expected output:

```
Pass: 10/10, warn: 0, fail: 0
verdict: pass
```

If `validate` exits 1, the message names the failing check (orphan TODO, missing provenance, manifest hash mismatch, etc.) and points at the line in `docs.md` or the file in `raw/` or `probes/` that needs attention. Fix and re-run.

`validate --network` additionally re-fetches every `<!-- source: -->` URL in `docs.md` and verifies the response is still 200 with a matching content type. Run this before handing off to `skill-creator` if any time has passed since the harvest.

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

End state: same `docs.md` and `handoff.json` as the live walkthrough produces, minus the live-captured headers and timing data. Drift findings still appear in `## Gotchas` because the bundled fixtures were captured against a known-good Hetzner response and `quick-diff` re-runs cleanly against the offline spec.

The live walkthrough is for contributors who want to verify their token works against a real API and capture fresh drift data. The offline walkthrough is what unblocks every other contributor.

---

## What this case study demonstrates beyond Hetzner

Three patterns transfer to any archetype-4 harvest:

1. **Discovery cascade for SPA-rendered Swagger UIs.** Direct fetch → common spec paths → renderer-config regex over view-source → community mirror fallback. The mirror path requires explicit `mirror: unofficial` provenance and a staleness check. Linear, Fly.io machines, and most fintech APIs follow exactly this shape.
2. **Spec-plus-narrative merge under canonical H2s.** The spec gets you per-endpoint contract content; sibling narrative pages get you the things specs never carry — auth flow, rate-limit policy, error envelope semantics, gotchas. Both feed `consolidate`, both land under `references/doc-template.md`'s canonical H2s, both carry separate provenance.
3. **Probing-as-evidence, scope-labeled.** A captured live response is not a doc page; it's a scoped artifact with its own provenance shape (`<!-- probe: METHOD URL status: N retrieved: DATE scope: LABEL fixture: PATH -->`). Treating it as a doc page collapses the trust distinction the downstream verifier relies on. Probes are optional and additive; the offline path is the contract.

The doc-template-canonical H2 layout with per-tag H3 sub-sections under `## API reference` is what `openapi-harvest consolidate` always emits for archetype 4. Other archetypes use the same H2s with different H3 organization (per-resource, per-module, per-subcommand) — the H2 contract is shared, the H3 structure adapts to the source shape.
