# Deferred work — what is left, and the record of what was not

Not a task list. This is where work that was **deliberately not done** is recorded, so
the decision to defer stays visible rather than becoming folklore. Each open entry says
what it costs to leave alone; §F says what every closed one turned out to be.

Effort tags are estimates: **S** ≈ under an hour, **M** ≈ half a day, **L** ≈ more.

**Status: the backlog is empty.** §A (defects), §B (simplifications), §C1
(the one measured performance finding) and §E (documentation drift) are all closed —
see §F. §D is decided, and D1 was implemented in the process. What remains is §G:
four things that are known, bounded and deliberately left, none of which is a defect.

The sweep that closed this list also found **eleven defects that were not on it**, seven of
them introduced by fixes made during the sweep itself. That ratio is the reason §F is
long: the record of what a fix broke is worth more than the record of what it fixed.

---

## Recurring failure modes

Read this before adding a security control or a doc. Every item below bit this codebase
at least twice, and each was found only by execution, never by reading.

**1. A documented control that does not exist.** Nine cases now: the `manifest.json`
`allowed_hosts` array, `--network` "re-fetches every source URL and verifies
content-type", the `allowed_hosts` enforcement in the case study, a `validate` `warn`
verdict that could not occur, a documented offline sequence that exited 3, a "community
mirror fallback" discovery step, a `mirror: unofficial` label three docs said drove
downstream trust that nothing emitted, a `validate` check keyed on a field name
`consolidate` never writes, and — the first one found in code rather than prose — a
stderr message telling users to pass `--no-prefer-header-automatically`, a flag that has
never existed. A doc-lint would have caught none of them (§D3): the drift is always
semantic. What works is executing the documented commands in a test
(`test_documented_offline_smoke.py`, the `cli-contract` CI job) and treating any claim
about behaviour as unverified until something runs it.

**2. Redaction that silently does nothing.** `redact_body` only redacts by key while
walking a **dict**. Any body left as a string skips key-based redaction entirely — that
was the JSON request-body leak, then the form-encoded leak, then a padded-base64 blob
becoming a dict *key* where the pattern pass never looked, and then — after the request
side had been fixed and its docstring claimed the response side "gets this for free via
`resp.json()`" — the same leak in every non-JSON *response* body. Whenever data reaches
`redact_body`, ask what type it actually is at that point.

**3. Credentials travel further than the call that produced them.** `spec_url` was
redacted at exactly one of seven read sites. Redact at the point a value enters the
workspace, not at each place it leaves — one source-level fix closed six leak paths.
The standing guard is now `test_sentinel_credential_e2e.py`, which runs a full
`fetch → probe → consolidate` with a sentinel in seven input positions and asserts
which files may and may not contain it.

**4. `--allow-host ""` is truthy.** argparse `append` turns an unset shell variable into
`[""]`, and `HostAllowlist` drops empty strings, so a gate testing the raw arg list
admits an allow-everything allowlist. Always test the constructed object
(`_http.require_allowlist`). This shipped in three subcommands. The allowlist is now
bound to the client (§D1), so the remaining gate is `require_allowlist` itself.

**5. Two hashes of "the same" artifact.** `fetch` hashed the fetched bytes while
`quick-diff` re-hashed the re-serialised file, so the drift detector cried wolf on every
run. If two layers compare a digest, they must agree on which bytes. The same rule
applies to URLs: since the display/fetchable split (§A8), anything comparing two URLs
must normalise both sides first.

**6. A fix can be worse than the bug.** Adding `--follow-redirects` to give a no-op flag
a counterpart introduced a credential-forwarding hole that httpx's own follower did not
have. Removing the capability was the right fix. Ask what a new option obliges you to
maintain before adding it.

**7. A fix's failure mode is often quieter than the bug's.** Seven defects in this sweep
were introduced by its own fixes, and every one was *harder* to notice than what it
replaced. Reconciling the three credential key sets folded `location` into the query set,
so `?location=eu-central` — an ordinary parameter, and one this repo's own case study
harvests — was recorded redacted. Making `warn` reachable let the advisory array move the
verdict, flipping every local-file harvest from `pass`. `validate --network` created the
manifest it had just reported missing, so a red CI step went green on a bare retry.
Fixing the split-workspace bug made `probe` adopt the only harvest on the machine
regardless of what was being probed — writing into a populated directory, so nothing
exited 3 any more. In each case the test suite was green and the fix was *correct*; what
was wrong was the blast radius. After fixing something, ask what the new code does on
inputs the old code never reached, and check that the fix's own tests fail against the
unfixed source — several here did not.

The sharpest case: A9's superseded-digest warning fired on **every** ordinary
`consolidate` re-run, because `consolidate` is not byte-deterministic (each `retrieved:`
timestamp moves) — and `--strict` promoted it to `fail`, breaking the very CI sequence
that exists to prove the tool works. Its tests passed because they ran inside one clock
second and never produced a superseded entry. A test whose setup is faster than the
resolution of the thing it depends on is not testing that thing. It was found by running
the documented commands by hand *after* the suite and the doc-guards were all green,
which is the only reason it did not ship.

The sweep also found eleven defects on no list, of which seven came from its own fixes;
the ratio is the argument for reviewing a fix as hostilely as the bug.

---

## A. Defects — none open

A1 through A12 are closed. What each one was and how it was resolved is in **§F**, so
this section stays a pointer rather than a second copy that can drift from it.

A7–A12 were raised by the review of the A1–A6 fixes, which is the pattern worth keeping:
the review of a fix found as many defects as the fix closed.

---

## B. Simplification opportunities — none open

B1 through B8 are closed; see **§F**. Two are worth carrying forward as rules rather
than history:

- **`_spec.iter_operations` is the only definition of "an operation."** Four hand-rolled
  copies of the `paths -> {path: {method: op}}` walk drifted far enough that one
  workspace reported two different endpoint counts. Never re-inline it.
- **B4 was evaluated and rejected**, and the reasoning still holds: `cmd_validate.run()`
  stays linear. A check registry needs a context object every check ignores most of, and
  the reader loses execution order. The numbered comments already do the registry's job.

---

## C. Performance

### C1. `_match_probe` re-parsed every probe URL on every call — DONE

Was 63% of `consolidate`'s CPU: 105,842 `urlparse` calls for a value with 30 distinct
inputs. Replaced by `ProbeIndex`, which parses once at load and memoises the suffix scan
per queried path. **Measured 0.369s → 0.141s (−62%)** on a synthetic Stripe-scale
workspace (5.7 MB spec, 1000 operations, 50 tags, 30 probes).

Note for anyone revisiting it: the match is a **suffix** test, so a plain
`path -> probe` dict cannot replace it. That is why the index memoises per queried path
rather than keying on the probe path directly.

### C2. Everything else is below the noise floor

Recorded so nobody re-derives it. `_derive_coverage_checklist` re-scans `docs.md` eight
times (14.7 ms); `len(docs_md_text.split())` materialises 458k strings for a token count
(24 ms, ~40 MB transient); descriptions are sanitised twice (~12 ms); `validate` stats
the same path 1117 times where 32 distinct paths exist (2 ms); `quick-diff` reads the
spec file twice (parse, then hash). Fix these for clarity if the file is already open;
none is a performance reason to touch anything.

Explicitly checked and correct as-is: `record_run` is called once per process;
`cmd_auth`'s cascade is sequential **deliberately** (concurrency would fire 7–12
credentialed requests at a live API, and "first 200 wins" is order-dependent by design);
regexes compiled in loops go through `re`'s internal cache (1111 compiles = 0.2 ms).

The three-walk traversal C2 used to list is gone — B3 made it one walk — and
`find_all_provenance` is no longer called twice on the same string, which B4 merged.

---

## D. Architecture — decided

### D1. Bind the host allowlist to the client — DONE

Originally judged "only worth it bundled with B2". B2 was done, so this was too.

`build_client` returns a `GuardedClient` that rejects off-allowlist hosts in a request
event hook. Five hand-written enforcement points became one. The documented blocker —
that `cmd_fetch` deliberately uses a narrower allowlist for the GitHub staleness check —
was resolved by giving that call **its own client** bound to just that host, rather than
by teaching `narrowed()` to widen. `narrowed()` is intersection-only and raises at scope
entry if asked for a host the outer allowlist would reject.

Verified adversarially: `client.get`/`send`/`stream`, a redirect target, a
userinfo-disguised host, an off-allowlist IP literal, a subdomain, a homoglyph host, a
trailing-dot FQDN, clearing or replacing `event_hooks`, and three shapes of `narrowed()`
widening are all blocked. No source file constructs a raw `httpx.Client` any more, which
is what makes the hook unavoidable rather than merely conventional.

### D2. A redaction choke point at the write boundary — rejected

A single write helper that redacted everything would have to exempt `raw/spec.json` or
shift its recorded hash, and exemptions reintroduce the per-call-site judgement the choke
point was meant to eliminate. The policies genuinely differ by artifact (headers vs body
keys vs URLs).

**The recommended alternative was done instead:** `test_sentinel_credential_e2e.py`.
It would have caught the `spec_url` leak, and it now also pins the §A8 boundary — the
credential must appear in `raw/source-map.json` and nowhere else.

### D3. A doc-lint framework — rejected, with evidence

Evaluated against the ~20 real drifts in §E and would have caught approximately zero:
no documented example misspells a flag or omits a required argument. Every real drift is
semantic — quoted filenames, quoted stderr strings, check counts, cascade ordering.

**Both recommended alternatives were done instead:** `docs-guard` now bans four literals
that have already rotted once (`Pass: N/N`, `probes/auth-discovery.json`,
`probes/locations-200.json`, hardcoded endpoint counts), each with a failure message
saying *why* it is banned; and the documented sequences are executed by
`test_documented_offline_smoke.py` and the `cli-contract` CI job.

`scripts/README.md` is deliberately exempt from the `Pass: N/N` rule: its occurrence is
inside the `validate --json` schema block, which
`test_cmd_validate.py::test_summary_string_is_the_shape_the_readme_documents` parses and
asserts against a live run. An executable check beats a grep, so the digits stay.

---

## E. Documentation drift — closed

All ~20 items are fixed, plus a dozen more found while fixing them; see §F. The pass was
run under one rule — **the code is right, fix the prose** — with a standing exception
that a doc describing a *security* control the code lacks must be escalated, not quietly
reworded. That exception fired zero times: every security claim in
`references/probing-tools.md` was verified present in the code, and two undocumented
controls (`trust_env=False`, the `0o600` source map) were added to the prose.

The most consequential fix was not on the original list: **the documented Hetzner
walkthrough did not work.** `fetch` derived its workspace from the spec host while the
next command consolidated the API host, so a reader following it exactly hit exit 3 on a
workspace they had just created. That is now a code fix (§G notwithstanding) rather than
a prose one.

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

### Defects the A1–A6 fixes introduced or exposed — all six fixed in the same PR

A security / testing / adversarial review of the A1–A6 commits. Listed because two of
them were regressions the fixes themselves caused, which is the pattern §Recurring #6
warns about, and one was a test that pinned nothing.

| Finding | Resolution |
|---|---|
| **A2's verdict rework flipped every local-file harvest from `pass` to `warn`.** `archetype4_warn_spec_url` fires whenever `spec_url` is absent, which is always for `fetch ./spec.json`, and the rework let the `warnings` array move the non-strict verdict. Verified: a clean workspace reported `Pass: 14/14, warn: 2, fail: 0 → warn`, while README's own smoke example prints `verdict: pass` | The `warnings` array is display-and-`--strict` only again; the verdict comes from `checks` severity. `warn` is still reachable via the orphan-capture check, which is the point of A2. Pinned by `test_local_file_harvest_still_verdicts_pass` |
| **`validate --network` healed the thing it was checking.** `record_run` writes through `write_manifest`, which `os.makedirs` the workspace. A first run failed `manifest_exists` and created a manifest, so a bare retry of a red CI step went green with nothing fixed; `validate --network /typo` also created `/typo` | Recording is guarded on the manifest already existing. `validate` reports on a workspace, it does not build one |
| **`test_rerunning_consolidate_still_validates` passed with A4 reverted.** consolidate is byte-deterministic, so two runs over an unchanged spec record the *same* digest twice and the pre-fix code never sees a superseded hash | The test now edits the spec between runs. Confirmed failing against reverted source before re-landing |
| **`redact_url` never touched userinfo.** `https://user:pass@host/spec.json` passes the allowlist — `urlparse().hostname` strips userinfo — and the credential reached `source-map.json`, every `<!-- source: -->` comment, `handoff.json`, and every fixture's `spec_url_at_capture`. Failure mode #3, one layer further out than the `spec_url` query-string fix | `redact_url` rewrites the netloc as `<redacted>@host[:port]` |
| **`iter_operations` crashed on a non-string path key.** YAML mapping keys need not be strings, so `paths: {1: {get: ...}}` reached `json_pointer`'s `path.replace(...)` and raised AttributeError — replacing the numeric exit-code contract with a traceback. The new module's own docstring promised callers "only well-formed operations" | Guarded alongside the existing method check |
| **`file_entry` double-joined a relative workspace.** `consolidate myws` looked for `myws/myws/docs.md` and died with FileNotFoundError *after* docs.md was written. Pre-existing; the README rewrite had been working around it with an absolute path | Both sides resolve against the cwd first |

Two smaller ones, same review: `redact_text` was not idempotent (`token=<redacted>` grew a
second sentinel, because the regex stopped at `<`), and `redact_url` percent-encoded every
non-sensitive value, so `?filter=a/b,c` was recorded as `?filter=a%2Fb%2Cc` — a provenance
URL byte-different from the one actually fetched. Both fixed; the escape set now covers
only the characters that could split one parameter into two.

Six further findings from the same review were **not** fixed — they are §A7–A12.

---

Two things were fixed alongside A3, because it made them reachable rather than theoretical:

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

### A8 — the display/fetchable URL split

`redact_url("...?key=petstore")` yields `?key=<redacted>`, and that string was the only
URL the workspace recorded. Two real costs: the audit trail named a URL nobody could
re-fetch, and `validate --network` GET it and reported a 404 that was not real. Dropping
`key` from `SENSITIVE_QUERY_KEYS` would have traded a broken audit trail for a leaked
credential, so the fix is the schema change the entry proposed.

**Where the fetchable URL lives:** `raw/source-map.json`, as `fetch_url`, next to the
redacted `spec_url`. `manifest.json` was the other candidate — `validate --network` is the
consumer, and it already reads the manifest — and was rejected on three counts. It is
read-modify-write, so a credential there is rewritten on every subsequent run rather than
written once. `_manifest._redact_recursive` redacts every URL-shaped string it stores, so
`fetch_url` would need an exemption, which is exactly the per-call-site judgement §D2
rejected a write-boundary choke point for reintroducing. And `source-map.json` is already
the file that answers "where did this spec come from", written exactly once, by `fetch`.

Three properties make the split hold, and each has a test rather than a convention:

- **`_schema.read_source_map` strips `fetch_url`.** `consolidate` and `probe` — the
  subcommands that write `docs.md`, `handoff.json` and the probe fixtures — read through
  it, so they are never handed the value. `read_fetch_url` is the only reader and
  `validate --network` its only caller. `test_sentinel_credential_e2e.py` now asserts the
  boundary rather than an absence: the sentinel is in `source-map.json`'s `fetch_url` and
  in no other key of that file and no other file, with `docs.md`, `handoff.json` and every
  probe fixture named explicitly.
- **`validate --network` fetches one URL and prints another.** The GET uses `fetch_url`;
  every check id, message and `--json` field carries the redacted `spec_url`. `str(e)`
  goes through `redact_text` because httpx quotes the request URL in its own exception
  text — failure mode 3, and `URL {url} returned 404` is the shape it takes.
- **The file is written `0o600`,** via `_schema.write_source_map`, with the mode set on
  the descriptor before any content is written so overwriting a world-readable file from
  an older run does not leave a window.

Backward compatibility: a workspace harvested before this has no `fetch_url`. If its
`spec_url` carries no redaction sentinel it is fetchable as-is and nothing changes. If it
is redacted, the check is **skipped with an explanation**, emitted as a *passing* check so
it moves no verdict in either `--strict` mode — reporting a failure there is the bug being
fixed, and a skip that flipped the verdict would be the same lie relocated.

Failure mode 5 (two layers disagreeing about which artifact they compare) is guarded
structurally: everything that *compares* URLs still compares `spec_url`, and
`read_fetch_url` — the one place the two forms meet — normalizes both through `redact_url`
before treating them as the same URL. A workspace whose two recorded URLs disagree is
skipped, not fetched.

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

---

### The close-the-backlog sweep — A7–A12, B2–B7, C1, D1, E

Six parallel agents, each reviewed before merge. 177 → 379 tests. Recorded per item
because several of the write-ups above turned out to be wrong, and the corrections are
more useful than the fixes.

| ID | Resolution |
|---|---|
| A7 | `redact_url` no longer returns the URL verbatim when `urlparse` raises. A regex `key=value` fallback redacts sensitive keys and userinfo from the raw string. Failing closed was rejected: it destroys the audit trail for a malformed-but-harmless URL |
| A8 | The display/fetchable split. `raw/source-map.json` gained `fetch_url` at mode `0o600`; `read_source_map` **strips** it, so `consolidate`/`probe` cannot leak a value they are never handed. `manifest.json` was rejected as the home — `_redact_recursive` would have needed an exemption, which is the per-call-site judgement §D2 rejected |
| A9 | **Closed as won't-fix, after building it and finding it does not work.** The proposed fix — report superseded digests as advisory `warnings` — was implemented, then removed. Two independent reasons, both found by executing it: (1) `consolidate` is *not* byte-deterministic across runs, since every `retrieved:` timestamp moves, so an ordinary re-run supersedes the previous digest and the report fired on **every** re-run; under `--strict` that is a `fail`, which broke the documented consolidate → validate → re-consolidate → `validate --strict` sequence CI runs. (2) More fundamentally it carries no information: an older entry mismatching is *exactly* what a legitimate re-run and a tampered file both look like, so there is nothing to discriminate on. The underlying observation stands — `verify_hashes` is newest-wins, so a file can be edited and re-attested by appending a run — but manifest-internal tamper detection cannot work while the manifest lives inside the directory it attests. `test_a_consolidate_rerun_keeps_strict_green_across_a_clock_tick` pins the contract that the attempt broke |
| A10 | `--timeout <= 0` rejected with exit 1 in `run()`. **The write-up's prescribed fix was wrong**: an argparse `type=` exits 2, colliding with the network code the fix exists to disambiguate, and misses the hand-built `Namespace` path the tests and `SKILL.md` use |
| A11 | `spec_sha256` prose corrected in both places, including the migration consequence: a workspace fetched by an older version carries a body-hash that reports false drift until re-fetched |
| A12 | Tests added for the `record_run` guard, the `summary` contract, and the `allow_host` audit records. **Item 1 was half-wrong**: the guard *was* tested, but the test's workspace was a path that never existed, so weakening the guard to `os.path.exists(workspace)` left it green |
| B2 | The forked retry loop deleted. It had silently lost network-error retry — so `probe`, the subcommand most likely to hit a flaky API and the only one exposing `--max-retries`, was the one that did not retry. All existing 429/5xx tests passed unchanged under the swap |
| B3 | One `WalkedSpec` per run; `_handoff.py` split out. **The write-up undercounted**: there were **four** walks, not three, and the fourth disagreed with `iter_operations` about what an operation is. `_derive_coverage_checklist` deliberately still re-parses the markdown — it is a claim about what was *written*, and feeding it structured data would make it agree with the renderer by construction and stop being a check |
| B4 | Checks 5 and 6 merged into one `find_all_provenance` pass; blocks 8/9/10 extracted. No registry, per the rejection above |
| B5 | `_discover` split into `_fetch_direct` / `_probe_common_paths`. The real cascade order is **direct → renderer → common paths**; four docs claimed otherwise |
| B6 | The auth cascade became `AuthPattern` NamedTuples, each carrying its own `keep_when` predicate with **no default**, so a new pattern cannot be added without a filter gate. Verified equivalent across **928 combinations** of declared security schemes, zero divergences. The write-up's proposed field set was insufficient — `raw Authorization` is kept on bearer *or* basic while `Token header` is bearer-only, which a kind-based rule cannot express |
| B7 | `_io.write_json`/`write_text` (atomic), `_cli` parent parsers, `emit_probe` reuse, orphan-scan and section-emitter dedup. Flag sets verified **identical** across all six subcommands — only help text changed. The `_jp_escape` bullet was already done; the `0o600` trap was **not in the write-up at all** and is the one part that could have shipped a credential leak |
| C1 | `ProbeIndex`, Tier 2. 0.369s → 0.141s |
| D1 | Implemented — see §D above |
| E | All items fixed, plus the Hetzner walkthrough, the `handoff.json` shapes, an invented `consolidate` header block, and a claimed `quick-diff`→`consolidate` integration that does not exist |

### Defects found by this sweep that were on no list

Eleven. Seven were introduced by the sweep's own fixes, which is failure mode 7. The
last two were found by an adversarial security review run over the finished branch,
after the suite, the doc-guards and the CI contract were all green — which is the
argument for running one.

| Defect | How it was found |
|---|---|
| **`location` folded into `SENSITIVE_QUERY_KEYS`**, so `?location=eu-central` was recorded redacted — the A8 damage pattern, freshly introduced by the key-set reconciliation. The agent's own test had encoded it as intended behaviour | Reading the diff of a *fix*, then executing `redact_url` on an ordinary URL |
| **`probe`/`auth` adopted the only harvested workspace regardless of what was being probed**, filing one API's fixtures under another's harvest. Quieter than the split-workspace bug it fixed, because the directory already looks populated | Constructing the two-tool case by hand after reading the resolution logic |
| **A third-party spec could forge provenance comments and inject agent instructions into `docs.md`.** `sanitize_spec_descriptions` only rewrites values keyed description/summary/title, so a parameter's `name`/`in`, a response status code (a *dict key*, so arbitrary text) and a securityScheme's name/`type`/`scheme` all reached the renderer raw. A backtick plus a newline escapes the inline code span. The neighbouring path/method/tag sites were already sanitized for exactly this reason | A security review of the branch, executing a hostile spec through `consolidate` and parsing the output with `find_all_provenance` |
| **A response body that stayed a string bypassed key-based redaction**, so a form-encoded `access_token=...` or a plain-text error quoting a credential was written verbatim to `probes/*.json` at 0644 — weaker than the `0o600` the branch gives the one file meant to hold a credential. The request path had already solved this; its docstring claimed the response path "gets this for free via `resp.json()`", true only for JSON payloads | Same review, mocking a form-encoded token response through `probe --scope auth-discovery` |
| **A9's own fix broke the documented CI sequence.** The superseded-digest warning fired on every ordinary `consolidate` re-run, and `--strict` promoted it to `fail`. The A9 tests passed only because they ran inside one clock second, so no digest was ever superseded | Running the `cli-contract` sequence by hand with a real clock, after `docs-guard` and the test suite were both green |
| **`validate` check ids derived from Python's salted `hash()`** — the same file produced `13db`, `48a6`, `4506` on three runs, and `id` is documented as a stable contract | Snapshot-diffing `validate --json` across a refactor; the ids were the only fields that moved between two runs of identical code |
| **`validate` check 10 keyed on `item["source"]`** while `consolidate` writes `sources` — the check had never fired on any workspace this tool has produced | Executing it against a real `handoff.json` |
| **`cmd_auth` told users to pass `--no-prefer-header-automatically`**, a flag that has never existed. The first instance of failure mode 1 found in code rather than prose | Grepping every quoted stderr string in the docs against the source |
| **`urlparse` raising inside `_match_probe`** took the whole run down with a traceback, breaking the exit-code contract, for any probe fixture with an unparseable URL | Writing an adversarial fixture for the C1 refactor |
| **A2's verdict rework flipped every local-file harvest from `pass` to `warn`** | Running the README's own smoke example and comparing to its documented output |
| **`validate --network` created the manifest it had just reported missing**, so a red CI step went green on a bare retry | Running `validate --network` twice |
| **`quick-diff --source-map` was accepted and read by nothing** | Enumerating the real flag set for the doc table |

### Tests that pinned nothing, found by reverting

Every fix in this sweep was required to fail against reverted source. Four did not, and
were rewritten:

- `test_rerunning_consolidate_still_validates` passed with A4 reverted, because
  `consolidate` is byte-deterministic — two runs over an unchanged spec record the *same*
  digest twice, so the pre-fix code never saw a superseded hash. Now edits the spec
  between runs.
- A cascade-reachability test passed against the unfixed elif chain, because when nothing
  matches the chain the filter falls back to the full brute-force cascade — the escape
  hatch masked the drop. The spec now declares `bearer` so the filtered cascade is
  non-empty and the drop is observable.
- Two `redact_url` fallback tests passed against the fail-open code by coincidence: an
  identity function is trivially idempotent and trivially preserves benign input. Kept,
  but relabelled as correctness checks rather than regression pins.
- One revert attempt produced `TypeError: run() got an unexpected keyword argument
  'transport'` — a scaffolding failure that pins nothing. Discarded and re-run reverting
  only the behaviour, which then showed the real failure: the old code GETting
  `...key=%3Credacted%3E`.

---

## G. Known, bounded, and deliberately left

None of these is a defect. They are recorded so they are not rediscovered as news.

### G1. `doc-template.md` describes a richer document than `consolidate` emits — S, unresolved question

The template specifies a fuller header block and an 8-item coverage checklist;
`consolidate` emits three metadata lines and a two-item checklist. The resolution written
into the docs is "the agent fills in the rest, `consolidate` cannot know the Phase 0
answers", which is consistent but is an *interpretation* — nothing in the code states it.

Someone should decide whether the template is the agent's target (current reading) or
`consolidate`'s spec (in which case `consolidate` is under-emitting). Left alone because
guessing wrong here changes what skill-creator receives.

### G2. `--bad-token-pattern` is user-overridable — documented tradeoff

The fixed bad-token string exists so a 401 baseline is distinguishable from a real
credential failure. A user who sets it to something resembling their real token defeats
that. Now documented in the flag table rather than changed: the flag has legitimate uses
against APIs that reject the default pattern's shape outright.

### G3. The slug's 3-segment ceiling merges deep subpages — deliberate

`<host>` plus up to three identifying path segments means two deep subpages of one docs
site share a workspace. That is what cache detection wants; disambiguation lives in the
leading owner/repo segments, which is where same-named projects actually differ. Recorded
because it looks like a bug from the outside.

### G4. Workspace auto-discovery is host-matched, not identity-matched — S

`probe`/`auth` without `--workspace` adopt the single harvested workspace only when its
spec declares the host being probed. A spec declaring no absolute host (a relative
`servers` entry) does not veto adoption, because there is nothing to contradict — so an
offline local-spec harvest can still adopt a workspace for an unrelated API. Passing
`--workspace` is exact and always available. Tightening this would break the common
offline case, which is why it was left.

---
