# Deferred work — simplification opportunities and known drift

Not a task list. This is the backlog of things found during the PR #3 / PR #4 review
sweeps that were **deliberately not fixed**, recorded so the decision to defer is
visible rather than forgotten. Each entry says what it costs to leave alone.

Sources: the PR #3 review rounds (fable plan review, security / testing /
maintainability specialists, adversarial pass) and a four-angle `/simplify` sweep
(reuse, simplification, efficiency, altitude) run against the package at `2e67091`.

Effort tags are the reviewers' estimates: **S** ≈ under an hour, **M** ≈ half a day,
**L** ≈ more.

**Status:** §A (all six defects), §B1 (the duplicated spec walk) and §B8 (dead code) are
**done** — see §F. §B2–B7, §C, §E are open. §D is decided, not pending.

---

## Recurring failure modes

Read this before adding a security control or a doc. Every item below bit this
codebase at least twice, and each was found only by execution, never by reading.

**1. A documented control that does not exist.** Six separate cases so far: the
`manifest.json` `allowed_hosts` array, `--network` "re-fetches every source URL and
verifies content-type", the `allowed_hosts` enforcement in the case study, a `validate`
`warn` verdict that cannot occur, a documented offline sequence that exits 3, and a
"community mirror fallback" discovery step. A doc-lint would have caught none of them
(§D3) because the drift is always semantic. What works: run the documented commands in a
test (`test_documented_offline_smoke.py`), and treat a claim about behaviour as unverified
until something executes it.

**2. Redaction that silently does nothing.** `redact_body` only redacts by key while
walking a **dict**. Any body left as a string skips key-based redaction entirely — that
was the JSON request-body leak, then the form-encoded leak, then a padded-base64 blob
becoming a dict *key* where the pattern pass never looked. Whenever data reaches
`redact_body`, ask what type it actually is at that point.

**3. Credentials travel further than the call that produced them.** `spec_url` was
redacted at exactly one of seven read sites. Redact at the point a value enters the
workspace, not at each place it leaves — one source-level fix closed six leak paths.

**4. `--allow-host ""` is truthy.** argparse `append` turns an unset shell variable into
`[""]`, and `HostAllowlist` drops empty strings, so a gate testing the raw arg list
admits an allow-everything allowlist. Always test the constructed object
(`_http.require_allowlist`). This shipped in three subcommands.

**5. Two hashes of "the same" artifact.** `fetch` hashed the fetched bytes while
`quick-diff` re-hashed the re-serialised file, so the drift detector cried wolf on every
run. If two layers compare a digest, they must agree on which bytes.

**6. A fix can be worse than the bug.** Adding `--follow-redirects` to give a no-op flag
a counterpart introduced a credential-forwarding hole that httpx's own follower did not
have. Removing the capability was the right fix. Ask what a new option obliges you to
maintain before adding it.

---

## A. Defects — all six closed

A1 through A6 were the observable bugs on this list. All six are fixed; the record of
what each one was and how it was resolved moved to **§F**, so this section stays a
pointer rather than a second copy that can drift from it.

Nothing in §A is outstanding. A new defect goes here as A7.

---

## B. Simplification opportunities

### B1. The spec-operation walk is duplicated four times — DONE

Was: the same nested `paths → {path: {method: op}}` walk in `cmd_fetch` (×2) and
`cmd_consolidate` (×2), with the method tuple already forked — see **A1** in §F.

Now `_spec.iter_operations` owns the walk and the whitelist, and all four call sites go
through it. `trace` is included. Both JSON-Pointer builders collapsed into
`_spec.json_pointer` at the same time.

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
what the split forces you to notice: the spec is still walked three times per run — twice
via `_group_ops_by_tag` and once for the endpoint/tag counts in `_build_handoff`. **B1**
made those three walks share one implementation; it did not make them one walk. Build a
`WalkedSpec` value once in `run()`, pass it to both builders; then `_handoff.py` imports
one thing and `cmd_consolidate` drops to ~450 lines with a single traversal.

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

### B8. Dead code — DONE in `2fcb700`

Removed after verifying zero references for each: `_section_or_default`, `CANONICAL_H2`,
`NormalizedSpec`, `PLACEHOLDER_VALUES`, `_try`'s `timeout` parameter, `cmd_auth`'s
`# noqa: F841` local, `_section_has_provenance`'s `text` parameter, `_spec_pointer`'s
no-op branch, and `quick_diff`'s redundant `or spec_path == target_path`.

`test/conftest.py` had 45 unreferenced lines while two test modules hand-rolled the
transport helper it already provided. `hcloud_workspace` was wired up rather than deleted
— its fixture corpus backs the offline sequence `probing-tools.md` documents, which is
now executed by `test_documented_offline_smoke.py`.

**Still open from this area:** `CANONICAL_H2`'s deletion removed the third copy of the
section-name list, but two copies remain (`_build_docs_md` and
`_derive_coverage_checklist`, which spell "Rate limits, quotas, versioning" differently).
Unifying them is part of **B3**.

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
exit 1 or 3 after a `fetch`. **Still open.**

*(Resolved: the flat `cp scripts/test/fixtures/hcloud-offline/* <ws>/` that exited 3, and
the neighbouring "CI exercises this sequence on every PR" claim that nothing backed.
`test_documented_offline_smoke.py` and the `cli-contract` CI job now run the sequence as
written, so it fails loudly if it rots again.)*

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

---

## F. Resolved — history

### §A defects — all six closed

Filed as "documented but unfixed" during the PR #4 sweep, then fixed in the same PR once
the two that needed a product decision got one. Effort tags held: five were S, none
needed the architecture change §D had rejected.

| ID | Defect | Resolution |
|---|---|---|
| A1 | `trace` operations produced two contradictory endpoint counts — the op reached `raw/source-map.json` but got no `docs.md` section | Fixed with **B1**: `_spec.iter_operations` owns the walk and the method whitelist, `trace` included. The four call sites cannot disagree because there is one list. `test_spec_walk.py` pins fetch's count against `handoff.json`'s rather than against a literal |
| A2 | `validate`'s `warn` verdict was unreachable while `scripts/README.md` documented `pass \| warn \| fail` as a stable contract | Kept the contract, made `warn` reachable: the orphan-capture check now carries `severity="warn"`, and the advisory `warnings` list feeds the verdict instead of being ignored outside `--strict`. `warn` exits 0; `--strict` promotes it to `fail` and exits 1. The doubly-dead `verdict == "warn" and args.strict` branch is gone |
| A3 | `cmd_auth` wrote `winner_pattern`, `bad_token_status`, `attempts` into a hand-built fixture dict that `ProbeFixture.from_dict` — the only reader's only entry point — could not read | Promoted all three into `ProbeManifest`, and `cmd_auth` now builds through `ProbeFixture(...).to_dict()` so a field it invents cannot again be a field nothing reads. The test asserts through `from_dict`, not against the raw JSON |
| A4 | Re-running `consolidate` made `validate` fail: `verify_hashes` walked every recorded run, so the first run's superseded `docs.md` digest mismatched | `verify_hashes` checks each path against the **newest** run that wrote it. `manifest.json` stays append-only — this was only ever about which entry claims to describe the file on disk |
| A5 | `fetch` took 210s to fail against a host that blackholes: 7 speculative spec paths × the full `--timeout` | `DISCOVERY_PROBE_TIMEOUT = 5.0` caps the guesses via a new per-request `timeout` on `request_with_retry`; the URL the user actually named keeps the full budget. ~35s worst case. Concurrency stayed rejected |
| A6 | `HostAllowlist.check` and `__contains__` read "empty" oppositely, and `host in allowlist` looked like `check` while meaning the reverse | `__contains__` became `lists_host()`, with the asymmetry and the reason for each half written into the class docstring. Deliberately not spelled `in` any more — that was the whole trap |

Two things were fixed alongside, because A3 made them reachable rather than theoretical:

- A failed `--include-query-auth` attempt recorded its exception message, which can quote
  the URL the token was in. `_redaction.redact_text` now redacts URLs found inside free
  text — failure mode #3 again, one step further out.
- CI gained a `cli-contract` job: it installs the package, runs the README's documented
  `consolidate` + `validate --json` sequence through the real binary, and asserts the
  documented keys and verdict from outside the package. The `warn` verdict shipped
  documented-but-impossible for a release; pytest could not have caught that, because
  pytest asserts what the code does.

Kept so a future scan does not re-report these as new, and so the "already fixed before
the report landed" cases are not re-investigated.

### `ISSUES-2026-05-16.md` scan report — all 9 closed

The report was written against `feat-archetype-4-openapi@adbbb6b`; by the time it was
worked, `origin/main` had moved to `127ed8f` and three findings were already fixed. That
gap is the reason every finding below was re-verified against the current tree before any
code was touched — worth repeating for the next scan.

| ID | Finding | Outcome |
|---|---|---|
| HIGH-1 | `--no-resolve` skipped `$ref` validation | Fixed, PR #3 — validator split into collect-all so the caller picks severity; fatal on the resolve path, warning under `--no-resolve` |
| HIGH-2 | Dead `last_exc` capture in the retry loop | Fixed, PR #3 — removed; retry accounting verified correct, no latent bug behind it |
| MEDIUM-1 | 19 ruff findings | Fixed, PR #3 (17 by then) — plus a pinned `ruff` CI job so it stays fixed |
| MEDIUM-2 | CI matrix stopped at 3.12 | Fixed, PR #3 — 3.10 through 3.14 |
| MEDIUM-3 | `__pycache__` not ignored | Already resolved before the report was worked |
| LOW-1 | `ad-hoc` probe scope undocumented | Already resolved — documented in `probing-tools.md` |
| LOW-2 | No `sys.version_info` gate | Already resolved — `openapi_harvest.main` exits 5 |
| LOW-3 | README install drift | Fixed, PR #3 — the archetype-4 `pip install -e` step is spelled out |
| LOW-4 | `handoff.json` shape not machine-checkable | Fixed, PR #3 — as `_schema.lint_handoff`, **not** the proposed dataclass; a `from_dict` in the house style defaults every missing key and raises on nothing, so it would have checked less than the `.get()` chains it replaced |

### Defects the scan report missed, found while fixing it

All fixed in PR #3 or #4. Listed because they outnumber the report's own findings, and
the most severe one outranked both of its HIGHs.

- Probe request bodies bypassed key-based redaction entirely — JSON, then form-encoded,
  then padded base64 landing in a dict key. Secrets written verbatim to fixtures under the
  **default** policy.
- `spec_url` carrying `?api_key=` reached `docs.md` and `handoff.json` — the two artifacts
  that leave the machine.
- `SENSITIVE_QUERY_KEYS` had drifted from `DEFAULT_BODY_KEYS`, missing `client_secret`,
  `refresh_token`, `client_assertion`, `private_key`, `session`.
- `redact_url` corrupted the URLs it recorded (`?q=one%26two` → `?q=one&two`, one
  parameter becoming two), and the damage compounded across `probe` → `quick-diff`.
- `--allow-host` gates tested the raw argparse list, so `--allow-host ""` admitted an
  allow-everything allowlist. Three subcommands.
- `validate --network` fetched a URL read from `handoff.json` with no allowlist at all.
- An off-allowlist renderer URL was swallowed by a broad `except Exception`, making the
  exit-1 path unreachable and the error message misleading.
- `quick-diff` reported phantom `spec_revision` drift on every run.
- `--no-follow-redirects` was a no-op; the counterpart added to fix that introduced a
  credential-forwarding hole, and the capability was removed instead.
- `Proxy-Authorization` was missing from the sensitive-header set.
- The CI case-study guard protected a `references/` file but only triggered on
  `scripts/**`.
