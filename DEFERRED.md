# Deferred work — simplification opportunities and known drift

Not a task list. This is the backlog of things found during the PR #3 / PR #4 review
sweeps that were **deliberately not fixed**, recorded so the decision to defer is
visible rather than forgotten. Each entry says what it costs to leave alone.

Sources: the PR #3 review rounds (fable plan review, security / testing /
maintainability specialists, adversarial pass) and a four-angle `/simplify` sweep
(reuse, simplification, efficiency, altitude) run against the package at `2e67091`.

Effort tags are the reviewers' estimates: **S** ≈ under an hour, **M** ≈ half a day,
**L** ≈ more.

---

## A. Defects — these are bugs, not cleanups

Listed first because they change behaviour a user can observe. None is fixed. All were
confirmed by reading code; the two marked *(measured)* were reproduced by execution.

### A1. `trace` operations produce two contradictory endpoint counts — S

`cmd_fetch._build_source_map:265` and `cmd_fetch._count_endpoints:303` include `trace`
in the HTTP-method whitelist. `cmd_consolidate._group_ops_by_tag:110` and
`_build_handoff:503` omit it.

For a spec containing a TRACE operation, `openapi-harvest fetch --count-endpoints`
prints N while `handoff.json → content_shape_signals.endpoint_count` reports N−1, the
operation appears in `raw/source-map.json` but gets no section in `docs.md`. One
workspace, two answers. Any future method addition has to be made in four places.

Fix travels with **B1** (a shared `iter_operations`), which is why it is deferred rather
than patched in place.

### A2. `validate`'s `warn` verdict is unreachable, and it is a documented contract — S

`cmd_validate._add_check` defaults `severity="error"` and **no call site overrides it**
(17 sites checked). So in the non-strict branch every entry in `failed` has
`severity == "error"`, making `not any(...)` always false: the `warn` verdict at
`cmd_validate.py:317` cannot be produced. `if verdict == "warn" and args.strict` is
doubly dead — `warn` requires *not* strict.

`scripts/README.md` documents `"verdict": "pass | warn | fail"` as a **stable v1 schema
that CI consumers may assert on**. Consumers are coding against a state the tool cannot
emit.

Pick one: delete `severity` and the warn branch (verdict becomes binary, update the
README), or make the genuinely non-fatal checks pass `severity="warn"` — the
orphan-capture check and the archetype-4 optional-signal checks are the candidates.
Today it is half of each.

### A3. `cmd_auth` writes three fixture fields that nothing can read — S/M

`cmd_auth.py:505-535` hand-builds the fixture dict instead of using
`ProbeFixture.to_dict()`, and adds `winner_pattern`, `bad_token_status`, `attempts`.
`ProbeFixture.from_dict` does not know those keys, and `cmd_consolidate._load_probes` —
the only reader — goes through `from_dict`. **The entire auth-cascade record is written
to disk and silently dropped on read.**

That is genuinely useful signal for the generated skill (which patterns were tried, what
each returned). Decide: promote the three into `ProbeManifest`, or give auth its own
`AuthFixture` type in `_schema`, or drop them deliberately. The status quo — written,
unreadable — is the one option that is not defensible.

### A4. Re-running `consolidate` makes `validate` fail — S *(measured)*

`_manifest.record_run` appends a new run with a fresh hash for the same path each time.
`verify_hashes` walks **every** recorded run, so after a second `consolidate` the first
run's now-superseded `docs.md` hash mismatches and `validate` reports
`hash mismatch: docs.md` — once per superseded run.

Re-running consolidate is a normal thing to do. Verify against the newest recorded hash
per path, not every historical one.

### A5. `fetch` has a 210-second worst case on an unresponsive host — S

`cmd_fetch.py:354-367` tries seven candidate spec paths sequentially, each with the full
`--timeout` (default 30 s). Against a host that blackholes rather than refuses, discovery
takes 7 × 30 s before reporting failure. Speculative probes should not inherit the
spec-download timeout: `min(args.timeout, 5.0)` bounds the tail at 35 s.

Concurrency was considered and **rejected** — it fires seven requests at a stranger's
origin to save under a second on a command whose next act is downloading a multi-MB spec.

### A6. `HostAllowlist` has opposite empty-set semantics on two methods — S

`check()` on an empty allowlist permits everything; `__contains__` rejects everything
(`_http.py:44-54`). `_collect_external_ref_violations` uses `__contains__`, so
`fetch ./local-spec.json` with an `$ref: https://example.com/x` is a violation even
though no allowlist was requested. That is fail-closed and probably intended — but two
opposite readings of "empty" on one class is the shape of a future bug. At minimum a
comment; better, name the methods so the asymmetry is visible.

---

## B. Simplification opportunities

### B1. The spec-operation walk is duplicated four times and has already drifted — M

`cmd_fetch.py:265`, `cmd_fetch.py:303`, `cmd_consolidate.py:110`, `cmd_consolidate.py:503`
are the same nested `paths → {path: {method: op}}` walk with the same
skip-non-dict / filter-by-method-tuple logic. The tuple has already forked (**A1**).

Add `_spec.py` with `iter_operations(spec) -> Iterator[tuple[str, str, dict]]` owning the
method whitelist; rewrite all four on it. Decide `trace` once.

### B2. `cmd_probe._retry_with_policy` is a strictly worse fork of `_http.request_with_retry` — S

38 duplicated lines (`cmd_probe.py:161-198` vs `_http.py:93-135`). Diffed line by line:
identical allowlist precheck, identical 429/`Retry-After` handling, identical 5xx
backoff, identical return contract, and a signature the existing call site already
matches. It even re-inlines `_parse_retry_after` verbatim.

**One real difference:** the shared version retries transient network errors; the fork
does not. So `probe` — the subcommand most likely to hit a flaky live API, and the only
one exposing `--max-retries` — is the one that lost network-error retry, by forking.

Delete the fork; call `request_with_retry`. Note this is a small behaviour change
(probe starts retrying network errors), which is what `--max-retries` already advertises.
Existing 429 and 5xx tests pass under the swap.

### B3. `cmd_consolidate.py` splits cleanly — but only pays for itself alongside the walk fix — M

The module is four layers (load / spec-walk / render / handoff) and the handoff layer
touches the render layer through exactly one value: `docs_md_text: str`. It already
treats docs.md as opaque — `_derive_coverage_checklist` re-parses the markdown it was
just handed. A real boundary.

But moving 280 of 840 lines to `_handoff.py` is just moving lines. What makes it pay is
what the split forces you to notice: the spec is walked three times per run
(`:338`, `:516`, and a third hand-rolled walk at `:496-509`). Build a `WalkedSpec` value
once in `run()`, pass it to both builders; then `_handoff.py` imports one thing and
`cmd_consolidate` drops to ~450 lines with a single traversal.

**Do the split only as part of that.**

### B4. `cmd_validate.run()` — keep it linear, do not build a check registry — S

Considered and **rejected**. The ten numbered blocks share seven locals
(`docs_text`, `lines`, `sections`, `handoff`, `handoff_ok`, `workspace`, `args`); a
registry needs a context object passed to every check, most of which ignore most fields,
and the reader loses execution order. The numbered comments already do the registry's job.

Worth doing instead: merge checks 5 and 6 (both call `find_all_provenance(docs_text)` and
both loop the same two field names — one pass does both), and extract blocks 8/9/10 into
three small functions to collapse their three identical `if handoff_ok:` guards.

### B5. `cmd_fetch._discover` control flow — S

One `try` spans two network calls and nests five levels deep. A renderer 404 and an
unreachable origin land in the same `except Exception: pass`, indistinguishably. Two
`allowlist.check()` pre-checks (`:339`, `:357`) are **no-ops** — `request_with_retry`
checks the same URL on the very next line and the violation is caught by the same handler
either way.

Split into `_fetch_direct(...) -> tuple[bytes, str] | None` and
`_probe_common_paths(...) -> tuple[bytes, str] | None`, each with early returns.
`_discover` becomes four lines; nesting drops five → two.

### B6. `cmd_auth`'s pattern names are load-bearing data — M

The display label (`"X-API-Key"`, `"query ?api_key="`) is parsed back as data by three
consumers: a 10-branch `elif pattern_name == ...` chain, a
`pattern_name.replace("query ?", "").rstrip("=")` that reconstructs a key known literally
at construction time, and `_classify_winner`'s `name.startswith("query ")`.

`HEADER_PATTERNS` and the elif chain are two parallel lists of the same seven names.
Add a header pattern and forget the chain, and it silently vanishes from every
spec-filtered run — no error, no test failure.

Make cascade entries a `NamedTuple(name, headers, url, kind, key)`; the elif chain
becomes a membership test and `_classify_winner` a dict lookup.

### B7. Smaller duplications — S each

- **`_spec_pointer` vs `_jp_escape`** (`cmd_consolidate.py:128` / `cmd_fetch.py:252`) —
  two JSON-Pointer builders that must agree for provenance to be traceable across
  `source-map.json` and `docs.md`. They currently do. The consolidate copy carries a
  provable no-op branch (`f"~1{escaped[2:]}"` reconstructs `escaped`) — vestigial scar
  tissue from a double-escape bug the other one documents fixing.
- **Two probe-orphan scans** (`cmd_consolidate.py:342` and `:405`) differing only in
  guard and message; the first uses a counter as a boolean and never breaks.
- **Nine section emitters** (`cmd_consolidate.py:282-455`) with ~100 lines of identical
  `## Title` / body / blank scaffolding. "Minimal working example" is 17 lines that
  differ from the shared helper by one appended TODO comment.
- **`cmd_auth` reimplements `emit_probe`** as an f-string reproducing its output
  byte-for-byte (`cmd_auth.py:309-313`).
- **No `write_json` helper, and no atomic write anywhere** — the same three-line
  `open/json.dump/write("\n")` appears six times. `manifest.json` is read-modify-write,
  so an interrupt mid-write truncates the audit trail and `verify_hashes` then reports
  the workspace corrupt. A `tmp + os.replace` helper buys crash-safety for free.
- **Argparse boilerplate** — `--allow-host`, `--workspace`, `-q`, `--timeout`,
  `--no-follow-redirects` repeated across six parsers. The user-visible cost is already
  realised: `--allow-host` is security-critical and required by four subcommands, but
  only `validate --help` explains it. Parent parsers would propagate the help text.

### B8. Dead code — S

`cmd_consolidate._section_or_default` (unreferenced), `CANONICAL_H2` (unreferenced; the
nine names are hardcoded twice more, with different spellings), `_schema.NormalizedSpec`
(unreferenced), `cmd_quick_diff.PLACEHOLDER_VALUES` (unreferenced; the values are
inlined), `cmd_auth._try`'s unread `timeout` parameter, `cmd_auth:303`'s `host` silenced
with `# noqa: F841`, `cmd_validate._section_has_provenance`'s unread `text` parameter,
`cmd_quick_diff.py:66`'s unreachable `or spec_path == target_path`.

`test/conftest.py` is 61 lines of which 45 are dead: `tmp_workspace`, `hcloud_workspace`,
`make_mock_transport`, `mock_transport` have **zero** references, while
`test_cmd_fetch._transport` and `test_staleness._make_client` hand-roll the same
routes-dict → `MockTransport` that `make_mock_transport` already implements.

---

## C. Performance — one finding that matters, measured

Profiled against a synthetic Stripe-scale workspace: 5.7 MB spec, 1000 operations,
50 tags, 30 probe fixtures. Baseline `consolidate` 0.47 s, `validate` 0.082 s.

### C1. `_match_probe` re-parses every probe URL on every call — 63% of consolidate's CPU — S

```python
def _match_probe(probe: ProbeFixture, path: str) -> bool:
    from urllib.parse import urlparse as _u      # function-local import
    pp = _u(probe.request.url).path              # recomputed every call
```

105,842 calls at this scale; **0.80 s of a 1.28 s profiled run, 0.735 s of it in
`urlparse`** — for a value that has only 30 distinct inputs. Five independent passes each
redo the matching.

- *Tier 1 (S):* hoist `urlparse` into `_load_probes`. **Measured: 0.47 s → 0.27 s, a 43%
  wall-clock cut**, no structural change.
- *Tier 2 (M):* build one `path -> [probe]` index; 105,842 matches → 30,000, and removes
  the linear-in-probe-count multiplier.

### C2. Everything else is below the noise floor

Recorded so nobody re-derives it: `consolidate` traverses the spec three times (<27 ms
combined), `_derive_coverage_checklist` re-scans docs.md eight times (14.7 ms),
`len(docs_md_text.split())` materialises 458k strings for a token count (24 ms, ~40 MB
transient), descriptions are sanitised twice (~12 ms), `validate` calls
`find_all_provenance` twice on the same string (25 ms of an 82 ms run) and stats the same
path 1117 times where 32 distinct paths exist (2 ms). `quick-diff` reads the spec file
twice (parse, then hash). Fix these for clarity if the file is open; none is a
performance reason to touch anything.

Explicitly checked and correct as-is: `record_run` is called once per process;
`cmd_auth`'s cascade is sequential **deliberately** (concurrency would fire 7–12
credentialed requests at a live API and "first 200 wins" is order-dependent by design);
regexes compiled in loops go through `re`'s internal cache (1111 compiles = 0.2 ms).

---

## D. Architecture — considered and mostly rejected

### D1. Bind the host allowlist to the client instead of checking at five call sites — M, conditional

Today one invariant has four hand-written enforcements. No site currently bypasses, but
the invariant is held by five people remembering, and **B2** is evidence that memory
fails.

Binding the allowlist to `build_client` via an httpx event hook would make bypass
structurally impossible. **The blocker:** `cmd_fetch` deliberately uses a *function-local*
allowlist for the staleness check (`api.github.com` is intentionally NOT in the global
allowlist — that narrowing is a documented security property). So the policy is genuinely
per-call for at least one of five callers, and a client-bound allowlist would need
per-call narrowing anyway — two mechanisms instead of one.

**Verdict: only worth it bundled with B2.** Alone it is marginal; combined, five
enforcement points become one. `require_allowlist` itself is at exactly the right
altitude — leave it.

### D2. A redaction choke point at the write boundary — M, rejected

Tempting after the `spec_url` leak (fixed at the source in `2e67091`), but a single write
helper that redacts everything would have to exempt `raw/spec.json` or shift its recorded
hash, and exemptions reintroduce the per-call-site judgement the choke point was meant to
eliminate. The policies genuinely differ by artifact (headers vs body keys vs URLs), so
"one choke point" mostly means "one place that dispatches to four policies."

**Do instead (S):** a test that runs a full `fetch → probe → consolidate` with a sentinel
credential in every input position and greps every workspace artifact for it. That is the
cheap version of the same guarantee, and it would have caught the `spec_url` leak.

### D3. A doc-lint framework — rejected, with evidence

Given how often this repo has had docs describing controls the code did not implement, a
doc-lint is the obvious reach. It was evaluated against the ~20 actual drifts found
(§E) and **would have caught approximately zero of them**: no documented example
misspells a flag or omits a required argument. Every real drift is semantic — quoted
filenames, quoted stderr strings, check counts, cascade ordering.

**Do instead:** extend the existing one-line `docs-guard` CI job with a few forbidden
literals (`auth-discovery.json`, `locations-200.json` outside `test/`, `Pass: 10/10`),
and — highest value — **add a test that executes the documented offline sequence verbatim
and asserts it succeeds**, because `probing-tools.md` explicitly claims CI does this and
that claim is currently false.

---

## E. Documentation drift — ~20 items, unfixed

Found by an audit of `references/`, `SKILL.md`, and `scripts/README.md` against the code.
Recorded rather than fixed because they want one pass by someone deciding which side is
right, not piecemeal edits.

**Wrong filenames.** Docs name the auth fixture `probes/auth-discovery.json`; the code
writes `probes/auth-<host>-<status>.json`. Docs name probe fixtures
`probes/locations-200.json`; the code writes `get-v1-locations.json` (no status).

**Documented sequences that fail.** `probing-tools.md:5` says every subcommand takes an
optional positional `WORKSPACE`; only `consolidate` and `validate` do, and they default to
`os.getcwd()`, so the bare `consolidate` / `validate` in composition examples 1, 2 and 4
exit 1 or 3 after a `fetch`. `probing-tools.md:197`'s
`cp scripts/test/fixtures/hcloud-offline/* <ws>/` exits 3 — the fixtures are flat and
`_load_spec` reads `raw/spec.json`. The next line claims "CI exercises this sequence on
every PR"; CI runs pytest, whose conftest builds the workspace correctly. **The documented
sequence is not the tested sequence.**

**Wrong behaviour descriptions.** `--network` is documented as re-fetching *every*
`<!-- source: -->` URL and verifying content-type; it fetches exactly one URL and checks
only `status_code == 200`. The cascade order is documented as direct → common paths →
renderer regex in three places; the code does direct → renderer → common paths. Two docs
promise a "community mirror fallback" step that does not exist. A quoted stderr string
(`"prefer-header-automatically"`) appears nowhere in the source, and the quoted staleness
warning text does not match what the code emits.

**Wrong numbers.** `scripts/README.md` maps exit 1 to "missing required arg"; argparse
exits **2**, which the same table maps to "network error". The case study says
`Pass: 10/10`, the README says 16 checks, and the real count is dynamic.

**Undocumented surface.** 26 flags have zero mentions in any doc, including
`--output-spec`, `--user-agent`, `--workspace`, `--redact-body-key`, `--short-circuit`.
