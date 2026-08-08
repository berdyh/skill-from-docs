# `openapi-harvest`

Single-binary CLI that powers archetype 4 (OpenAPI-only) for the
`skill-from-docs` Claude Code skill. Six subcommands share one configuration,
one redaction policy, one host-allowlist enforcement layer, and one
workspace-manifest schema.

## Install

```bash
pip install -e ~/.claude/skills/skill-from-docs/scripts
```

After install, `openapi-harvest <subcommand>` works from any directory. We
recommend `pipx install -e ~/.claude/skills/skill-from-docs/scripts` for
isolation.

Fallback (no install):

```bash
python -m skill_from_docs.openapi_harvest <subcommand> ...
```

Requires Python 3.10+.

## Subcommands at a glance

| Subcommand | What it does | When to reach for it |
|---|---|---|
| `fetch` | discover + parse an OpenAPI spec into `raw/spec.json` + `raw/source-map.json`, with renderer regex fallback for Swagger UI / ReDoc / Stoplight / Scalar / RapiDoc shells | always run first |
| `auth` | probe an endpoint with the seven default header auth patterns (Basic + query auth are opt-in) | when the spec says "bearer" but doesn't pin down the header shape |
| `probe` | capture one HTTP request/response pair as a redacted JSON fixture | spec-vs-reality drift, error envelope capture, real header inspection |
| `quick-diff` | compare a probe fixture against a spec; surface 6 categories of drift | after probing, before consolidating |
| `consolidate` | walk a workspace and emit `docs.md` (canonical H2s, per-tag H3s) and `handoff.json` | last step before handoff to `skill-creator` |
| `validate` | local-by-default completion check (hashes, provenance, archetype-4 signals); supports `--strict` and `--json` for CI | gate the handoff |

## Error contract

| Exit | Meaning | Example |
|------|---------|---------|
| 0 | Success | All subcommands happy path |
| 1 | User error | Missing required arg, bad URL, `--allow-host` violation |
| 2 | Network error | Connection refused, timeout, 5xx after retries |
| 3 | Spec/fixture parse error | malformed JSON, can't resolve `$refs` |
| 4 | Auth error | All auth patterns failed (`auth` only) |
| 5 | Missing dependency | Import failure at startup |

Missing-dep errors name the install command verbatim, e.g.

```
ERROR: openapi-spec-validator is not installed.
Fix: pip install "prance[osv]>=23.6"
Or:  pip install -e ~/.claude/skills/skill-from-docs/scripts
```

## Security model (summary)

These are non-negotiable defaults:

1. **`--allow-host` is required** on `auth`, `probe`, `fetch` (when the source is a
   URL), and `validate --network`. Without it, exit 1. It must name at least one
   non-empty host — `--allow-host ""` from an unset shell var is rejected, not
   silently treated as "allow everything".
2. **Query-string auth is OPT-IN ONLY** via `--include-query-auth`.
3. **Basic auth is OPT-IN ONLY** via `--basic-creds USER:PASS`.
4. **The bad-token probe uses a fixed string** (`aaaaaaaa-bad-token-bbbbbbbb`).
   Never derived from the real token.
5. **Redaction is the default in `probe`.** Headers in the sensitive set,
   sensitive body keys, sensitive URL query keys, `Set-Cookie`, `Location` —
   all redacted in the saved fixture. Opt-out via `--no-redact`.
6. **Redirects are never followed.** The probe captures `302`s with the
   `Location` header redacted. There is no opt-in: following a redirect safely
   needs cross-origin credential stripping *on top of* a per-hop allowlist
   check, and a captured `Location` answers the same question without either.
7. **Prompt-injection guard runs by default** in `consolidate`. Escapes
   `<!--`/`-->`, escapes line-leading `#`, detects and strips agent-instruction
   patterns. Opt-out via `--no-sanitize-descriptions`.
8. **HTTP/1.1 only.** `httpx.Client(http2=False)`. No HPACK surface, no `h2`
   transitive dep.

Full coverage in `references/probing-tools.md`.

## Concurrency

One harvest per workspace at a time. `manifest.json` records every run; running
two harvests concurrently against the same workspace may produce inconsistent
state. Document only — no lock implementation in v1.

## `validate --json` schema

```json
{
  "workspace": "/path/to/workspace",
  "verdict": "pass | warn | fail",
  "checks": [
    {"id": "docs_md_exists", "passed": true, "message": null, "severity": "error"}
  ],
  "warnings": [
    {"id": "...", "passed": false, "message": "...", "severity": "warn"}
  ],
  "summary": "Pass: 16/16, warn: 0, fail: 0",
  "checked_at": "ISO-8601 UTC"
}
```

Stable v1. CI consumers may assert on `verdict` and `summary`.

## Smoke test (offline)

```bash
# 1. Seed an offline workspace with bundled Hetzner fixtures.
mkdir -p ~/.claude/skill-from-docs/api.hetzner.cloud/{raw,probes}
cp scripts/test/fixtures/hcloud-offline/spec.json \
   scripts/test/fixtures/hcloud-offline/source-map.json \
   ~/.claude/skill-from-docs/api.hetzner.cloud/raw/
cp scripts/test/fixtures/hcloud-offline/*-200.json \
   ~/.claude/skill-from-docs/api.hetzner.cloud/probes/

# 2. Consolidate + validate.
openapi-harvest consolidate ~/.claude/skill-from-docs/api.hetzner.cloud --merge-probes
openapi-harvest validate ~/.claude/skill-from-docs/api.hetzner.cloud
# → verdict: pass
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `network error: ...` (exit 2) | host unreachable; CDN blocked | retry; try a local file with `openapi-harvest fetch /path/to/spec.json` |
| `no spec at .../raw/spec.json` | `fetch` was not run first | run `openapi-harvest fetch URL` |
| `failed to parse spec as JSON or YAML` | spec is HTML (SPA) and renderer fallback exhausted | inspect the URL with `curl`; pass the raw spec URL directly |
| `prance` import / circular `$ref` | install missing or known-circular spec | `--no-resolve` to skip flattening |
| `--allow-host violation` | endpoint host differs from allowlist | pass the actual host you intend |

## Tests

```bash
pip install -e ./scripts
pip install pytest ruff
pytest scripts/test/ -v
ruff check scripts/src scripts/test
```
