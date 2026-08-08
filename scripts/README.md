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

An **endpoint** is one operation: a path item keyed by one of the eight HTTP
methods OpenAPI defines, `trace` included. `fetch --count-endpoints`,
`raw/source-map.json`, `docs.md` and `handoff.json`'s `endpoint_count` all count
the same set — one workspace, one number. A `handoff.json` produced before this
was unified can disagree with a freshly generated one (`consolidate` used to omit
`trace`); re-run `consolidate` on any workspace whose count you rely on.

`auth` writes its cascade into the fixture manifest at
`probes/auth-<host>-<status>.json` — host with `.` replaced by `-`, and the status
being the **unauthenticated baseline**, which is the response the fixture body
records. It holds `winner_pattern` (the pattern that returned 200),
`bad_token_status` (what a deliberately invalid token returned), and `attempts`
(every pattern tried, with its status). Consumers should read these through
`_schema.ProbeFixture.from_dict` rather than off the raw JSON.

`probe` names its fixture `probes/<method>-<url-path>.json` — e.g.
`probes/get-v1-locations.json`. **No status code**, so a second capture of the same
endpoint overwrites the first; pass `-o PATH` to keep both. `auth-discovery` is a
`--scope` label, never a filename.

Re-running a subcommand over an existing workspace is safe. `manifest.json` is
append-only, and `validate` hash-checks each path against the most recent run
that wrote it; the superseded entries are reported as advisory warnings rather
than failures.

Every JSON artifact and `docs.md` are written atomically — temp file in the target
directory, then `os.replace` — so an interrupted run leaves the previous complete
file rather than a truncation. That matters most for `manifest.json`, which every
subcommand read-modify-writes and whose truncation made `validate` report the
whole workspace corrupt.

## Workspaces

`fetch`, `auth` and `probe` derive a workspace from their target and take
`--workspace DIR` to override it. `consolidate` and `validate` take the workspace
as an **optional positional that defaults to `$PWD`** — there is no universal
positional `WORKSPACE`, and a bare `openapi-harvest consolidate` after a `fetch`
walks the current directory and exits 3.

The derived default is the **bare hostname** of the target URL, nothing more:
`https://raw.githubusercontent.com/o/r/main/openapi.json` derives
`~/.claude/skill-from-docs/raw.githubusercontent.com/`. In an archetype-4 harvest
the spec host and the API host usually differ, so `fetch` and `probe` default into
*different* workspaces. Pass `--workspace` explicitly and use the same value
everywhere.

## Timeouts

`--timeout` governs the URL you actually name. Defaults differ per subcommand —
30s for `fetch` and `probe`, 10s for `auth`, which issues a cascade against one
endpoint. `quick-diff` and `consolidate` are offline and have none; `validate
--network` hardcodes 10s.

A non-positive `--timeout` is rejected with exit 1 on `fetch`, `auth` and `probe`.
It used to be forwarded to httpx, which raises before issuing anything — `fetch`
swallowed that per-candidate and reported "could not discover an OpenAPI spec"
after zero requests, and `auth`/`probe` burned the full backoff and reported a
network error.

When the named URL turns out not to be a spec, `fetch` guesses seven common spec
paths against the origin; those guesses are capped at 5s each, so a host that
blackholes packets fails discovery in about 35s rather than 210s.

## Error contract

| Exit | Meaning | Example |
|------|---------|---------|
| 0 | Success | All subcommands happy path |
| 1 | User error | `--allow-host` missing or violated, `--timeout 0`, file not found, unpaired `--staleness-api-*`, discovery cascade exhausted |
| 2 | Network error | Connection refused, timeout, 429/5xx after retries |
| 3 | Spec/fixture parse error | malformed JSON or YAML, poisoned external `$ref` under the default resolve, `consolidate` with no `raw/spec.json` |
| 4 | Auth error | All auth patterns failed (`auth` only) |
| 5 | Missing dependency | Import failure at startup, or Python < 3.10 |

**Exit 2 is also argparse's.** A missing or unrecognized *argument* — no `--scope` on `probe`, no `--token` on `auth`, an unknown flag — is rejected by argparse, which exits **2**, the same code this table gives to network errors. Nothing in this package can change that without shadowing the parser, so a CI consumer distinguishing "retry me" from "you invoked me wrong" has to read stderr: argparse's message starts with `usage:`, a network failure's with `ERROR: network error:`. This is also why non-positive `--timeout` is checked in `run()` rather than as an argparse `type=` — the argparse form would have reported a configuration mistake as exit 2.

Missing-dep errors are raised by the dispatcher and name the install command verbatim:

```
ERROR: missing dependency: No module named 'httpx'
Fix: pip install -e ~/.claude/skills/skill-from-docs/scripts
```

## Security model (summary)

These are non-negotiable defaults:

1. **`--allow-host` is required** on `auth`, `probe`, `fetch` (when the source is a
   URL), and `validate --network`. Without it, exit 1. It must name at least one
   non-empty host — `--allow-host ""` from an unset shell var is rejected, not
   silently treated as "allow everything". Enforcement is bound to the HTTP client,
   not repeated at call sites: `build_client` returns a `GuardedClient` that
   rejects an off-allowlist host in a request event hook, reinstalled by the
   `event_hooks` setter so it cannot be unhooked. `client.narrowed(...)` tightens
   the policy for a block and is intersection-only — it refuses an empty allowlist
   and raises rather than widening.
2. **Query-string auth is OPT-IN ONLY** via `--include-query-auth`.
3. **Basic auth is OPT-IN ONLY** via `--basic-creds USER:PASS`, or preferably
   `--basic-creds-env VARNAME`, which reads `USER:PASS` from an env var instead of
   shell history. The two are mutually exclusive.
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
9. **Proxy env vars are ignored.** `trust_env=False`, so `HTTP_PROXY` /
   `HTTPS_PROXY` / `NO_PROXY` cannot route a token-bearing request through a host
   the allowlist never saw.

### The workspace holds one live credential

`raw/source-map.json` records `fetch_url` — the spec URL **verbatim**, credentials
and all — so that `validate --network` has something re-fetchable after redaction
has replaced a benign `?key=petstore` with `?key=<redacted>`. Every other artifact
in the workspace carries only the redacted `spec_url`.

Three properties hold it in place, and a change to any one of them is a security
change, not a refactor:

- `_schema.write_source_map` is the only writer and passes `mode=0o600`.
  `_io.write_text` applies that to the temp descriptor *before* the content is
  written and before `os.replace`, so the file is never briefly world-readable —
  not even when a `0o644` `source-map.json` is already sitting there.
- `_schema.read_source_map` strips `fetch_url`. `consolidate` and `probe` read
  through it, so the code that writes `docs.md`, `handoff.json` and the probe
  fixtures is never handed the value at all.
- `_schema.read_fetch_url` is the single reader, and `cmd_validate._check_network`
  is its single caller. It hands the URL to `client.get` and nowhere else — in
  particular never into an error string, which is why `str(e)` goes through
  `redact_text` on the failure path.

Operationally: a workspace is safe to read and safe to hand to `skill-creator`. It
is **not** safe to copy wholesale into a repo, an archive or a bug report. Exclude
`raw/source-map.json`, or delete it once `validate --network` has run. Nothing in
the code makes that decision for the operator.

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

**The four numbers in that `summary` are an example of the shape, not a count to
match.** `validate` emits one check per section in `docs.md`, so the total scales
with the spec's tag and endpoint count and with how much narrative was merged —
read it off the command, never off a doc. What is pinned is the *format*:
`test_cmd_validate.py::test_summary_string_is_the_shape_the_readme_documents`
parses the string above out of this file, requires it to match
`Pass: N/N, warn: N, fail: N`, and then asserts what each of the four numbers
counts against a live run. That executable check is why this one line is allowed
to carry digits while `references/` may not (see the `docs-guard` CI job) — a
grep can only ban a literal, whereas this test fails if the wording, the
arithmetic, or the README drift apart.

Check `id`s, unlike counts, *are* stable across processes — the per-item suffix is
a SHA-256 prefix, not Python's per-process-salted `hash()` — so matching on an id
is safe where matching on a count is not.

**Verdicts.** `fail` means a check with `severity: "error"` did not pass — the
workspace is not ready to hand off, exit 1. `warn` means the only failing checks
were advisory ones, today just the unreferenced-capture check; exit is still 0 so
a pipeline keeps going. `pass` means neither.

The `warnings` array is a third, softer channel — "recommended optional field
absent", plus **superseded manifest digests**. It is reported but never moves the
non-strict verdict, because `spec_url` is legitimately absent for every local-file
harvest, a second `consolidate` over changed input legitimately supersedes a
`docs.md` digest, and a verdict that says `warn` for the ordinary case is a verdict
nobody reads.

On superseded digests specifically: `validate` verifies only the *newest* recorded
digest per path, which is what stops a re-run from failing. The older entries that
no longer describe the file are surfaced here instead, naming the runs that wrote
them — advisory by default, blocking under `--strict`.

`--strict` promotes everything — advisory checks and the `warnings` array — to
blocking, so the same workspace reports `fail` and exits 1.

**If you gate CI on `validate`, use `--strict`.** A default run exits 0 on an
unreferenced capture, and an unreferenced probe fixture holds a captured live-API
response that nothing in `docs.md` accounts for.

`scripts/test/test_cmd_validate.py` reads this file and asserts the three values
above are exactly the ones the code can emit, so the list cannot drift again.

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
| `ERROR: network error: ...` (exit 2) | host unreachable; CDN blocked | retry; try a local file with `openapi-harvest fetch /path/to/spec.json` |
| `ERROR: no spec at <dir>/raw/spec.json` (exit 3) | `fetch` was not run first — **or** it wrote to a different workspace than the one you passed | check the path in the message; re-run `fetch` with the same `--workspace` you pass to `consolidate` |
| `ERROR: failed to parse spec as JSON or YAML` (exit 3) | spec is HTML (SPA) and the renderer step found nothing usable | inspect the URL with `curl`; pass the raw spec URL directly |
| `ERROR: could not discover an OpenAPI spec from <url>` (exit 1) | all three cascade steps missed | find the spec (or a community mirror) by hand and pass that URL — there is no mirror-lookup step |
| `prance` import / circular `$ref` | install missing or known-circular spec | `--no-resolve` to skip flattening |
| `ERROR: --allow-host HOST is required for <sub> ...` (exit 1) | flag omitted, or expanded to `""` from an unset shell var | pass a non-empty host |
| `ERROR: host '<h>' not in allowlist (have: [...])` (exit 1) | endpoint host differs from the allowlist | pass the actual host you intend |
| `usage: openapi-harvest ...` (exit **2**) | argparse rejected the invocation — this is not a network error despite the shared exit code | read the `usage:` line |

## Tests

```bash
pip install -e ./scripts
pip install pytest ruff
pytest scripts/test/ -v
ruff check scripts/src scripts/test
```
