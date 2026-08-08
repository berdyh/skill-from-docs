---
name: skill-from-docs
description: Exhaustively harvest a tool or library's documentation into a consolidated markdown bundle (including image-text extraction and, for OpenAPI-only APIs, optional live-probing to capture real response shapes, auth patterns, and spec-vs-reality drift) and hand it off to `skill-creator:skill-creator`, which then produces the actual integration skill. Use whenever the user provides a docs URL, a GitHub repo, a library name, or an OpenAPI spec and wants a reusable integration skill — phrases like "make a skill from these docs", "turn this API's docs into a skill", "I want a skill for integrating <tool>", "read the docs for <library> and build a skill", "capture real responses from this API", "validate my OpenAPI spec against the live API", "figure out the auth pattern for this REST API", "my docs are SPA-rendered Swagger UI / ReDoc / Stoplight / Scalar / RapiDoc", or pasting a single docs subpage and asking for full coverage. Also triggers when the docs are partial, multi-page, JS-rendered, image-heavy, OpenAPI-spec-only, or in a non-English language and require crawling, headless-browser rendering, vision, live API probing with a token, or web-search supplementation to be complete before a skill can be written. This skill is discovery-only: it never produces a SKILL.md itself; that decision belongs to skill-creator.
---

# skill-from-docs

Exhaustively harvest a tool's documentation, then hand the result off to `skill-creator:skill-creator` to produce the actual integration skill.

Two phases:

1. **Harvest** — collect every meaningful page of the tool's documentation into one consolidated markdown file with source provenance, plus a transcribed sidecar for any text-bearing images.
2. **Handoff** — invoke `skill-creator:skill-creator` against the harvested workspace. skill-creator decides the resulting skill's name, structure, and body. This skill never writes a SKILL.md.

This split is deliberate. Harvesting is the hard, mechanical part — a single docs URL is almost always one leaf of a larger tree, and a naive fetch misses 60–90% of the surface. Skill structuring is judgement-heavy and skill-creator already owns it through its progressive-disclosure interview. Two implementations of "what a skill looks like" is one too many, so this skill pre-decides nothing about the output skill. It only describes what the docs contain; skill-creator's interview decides what the skill becomes.

---

## Phase 0 — Confirm inputs

Before touching the network, get explicit answers for these four. If any are missing or ambiguous, ask. Do not guess.

1. **Tool name** — canonical name (e.g. "Didox", "Stripe Node SDK", "Polars").
2. **Entry-point URL or repo** — docs site root, a docs subpage, a GitHub repo, or a package-registry page. If the user only gave a subpage like `api-docs.example.com/ru/integration-registration`, treat the host as the docs root and plan to cover all siblings.
3. **Target language / runtime** — Python, TypeScript, Rust, language-agnostic REST, CLI only, etc. This decides which SDK sections to prioritize. A skill that tries to cover every SDK is worse than a skill that covers one well.
4. **Integration scope** — minimal "hello world" call, full CRUD, OAuth flow, webhook handler, production-ready setup. Don't over-scope.

If the user has not provided any docs at all, ask for a URL or repo. Do not invent one.

---

## Phase 0.5 — Preflight tool check

Before starting Phase 1, verify the tools this workflow depends on are actually available in the current session, and check whether a previous harvest of this tool already exists. Failing loudly here is much cheaper than failing partway through a 200-URL harvest, and catching a cached workspace prevents re-harvesting the same docs for the third time this week.

### Tool checks

Check each row; if a required tool is missing, stop and report — don't improvise around it.

| Tool | Required for | If missing |
|---|---|---|
| `WebFetch` | Phase 1 Step 2 — fetching every discovered URL | **Hard fail.** Without it Phase 1 can't proceed. Ask the user to enable it and abort. |
| `skill-creator:skill-creator` skill | Phase 2 — handoff that produces the actual skill | **Hard fail.** This skill is discovery-only; without skill-creator there is no destination for the harvest. Abort with this exact message: *"skill-creator plugin is required. Install it (e.g. via `claude plugin install skill-creator`), then re-run /skill-from-docs. Aborting before any harvest runs to avoid wasted work."* Do not produce a partial skill. Do not write any harvest artifacts. There is no fallback template. |
| `WebSearch` | Phase 1 Step 4 — gap-filling searches | Proceed, but flag any still-empty checklist items as "couldn't verify, WebSearch unavailable" rather than guessing. |
| Browser-automation skill or MCP (see Phase 1 Step 1 fallback list) | Phase 1 fallback for SPA / JS-rendered docs only | Proceed. Only block if Step 0 classifies the docs as SPA *and* no browser tool is available — in that case ask the user to paste the rendered sidebar HTML. |
| Vision-capable Read on saved images | Phase 1 Step 2.5 — image-text extraction | Proceed but skip Step 2.5; flag any image-heavy archetypes (2/3/6) in the handoff packet so skill-creator knows the diagrams were not transcribed. |
| `sphobjinv` (optional) | Parsing ReadTheDocs `objects.inv` in Phase 1 | Skip that discovery probe. Sitemap coverage is usually sufficient. |
| Target tool's own CLI | Phase 1 Step 1 item 7 — iterating `<tool> --help` | Skip. Fall back to reading the argparse/clap tree from the tool's source in its repo. |
| Python ≥3.10 with `openapi-harvest` installed | Phase 1 archetype-4 probing | Proceed but skip probing; fall back to spec-only harvest with `<!-- TODO -->` markers. Install: `pip install -e ~/.claude/skills/skill-from-docs/scripts`. |

The two hard-fail rows are non-negotiable. The skill-creator hard-fail is the single most important policy in this skill: any local fallback that produces a SKILL.md re-encodes structural decisions that conflict with skill-creator's progressive-disclosure design, and a 200-URL harvest with no destination is wasteful — both are prevented by failing fast here.

### Cache detection

Compute the workspace path: `~/.claude/skill-from-docs/<tool-slug>/`. Slug convention: `<host>-<path-tail>` based on the entry-point URL, so two unrelated tools that share a name (e.g. two GitHub repos both named `agent-tools`) end up at distinct paths. Examples:

- `https://github.com/humanlayer/12-factor-agents/tree/main/content` → `github.com-humanlayer-12-factor-agents/`
- `https://docs.stripe.com/api` → `docs.stripe.com/`
- `https://api-docs.didox.uz/ru/integration-registration` → `api-docs.didox.uz/`

If the workspace path already exists, read its `harvest_metadata.retrieved_date` from `handoff.json` (or `mtime` of `docs.md` as a fallback) and prompt the user with three options:

1. **Re-use cached harvest** (default) — skip Phase 1 entirely, jump to Phase 2 handoff. Fast, free.
2. **Refresh** — re-fetch every URL from the cached URL queue, but skip rediscovery. Medium speed.
3. **Start clean** — wipe the workspace and re-harvest from scratch. Slowest; only when the cache is known stale or the source moved.

Never silently re-harvest. Cache hits are the common case; surface them.

### Preflight summary

Record the result in one short line before starting Phase 1, e.g. *"Preflight: WebFetch ✓, skill-creator ✓, WebSearch ✓, vision ✓; no existing workspace — fresh harvest"* or *"Preflight: tools ✓; existing workspace from 2026-04-12, re-using"*. That line makes the eventual failure modes (and cache decisions) legible rather than surprising.

---

## Phase 1 — Harvest

Target workspace: `~/.claude/skill-from-docs/<tool-slug>/` — a user-scoped, deterministic directory created on first run. **Never use a relative path or write into the current project directory.** The harvest must not pollute the user's working repo; cross-project re-use of cached harvests depends on this canonical location.

Slug rules: `<host>-<path-tail>` based on the entry-point URL host (so two GitHub repos named `agent-tools` don't collide). Disambiguator examples are listed in Phase 0.5 cache detection.

Workspace layout at the end of Phase 1:

```
~/.claude/skill-from-docs/<tool-slug>/
├── docs.md                  # consolidated harvest with provenance + inlined image transcriptions
├── handoff.json             # pre-filled answers for skill-creator's interview (Step 5)
├── manifest.json            # archetype-4: run records (incl. per-run allowlist, audit only), spec/probe hashes
├── images/                  # per-image transcribed sidecars
│   ├── <slug>-fig01.md
│   └── ...
├── images-manifest.json     # image → source URL → docs.md anchor map
├── raw/                     # raw fetched pages (kept for refresh + audit); archetype-4 also holds spec.json + source-map.json
│   └── <slug>.md
├── narrative/               # archetype-4: sibling prose pages consolidate merges under canonical H2s
├── probes/                  # archetype-4: captured live response fixtures (redacted, scope-labeled)
└── url-queue.json           # discovered URLs (used by "Refresh" cache option)
```

Lifecycle: workspaces persist after handoff. Two reasons — a future refresh against upstream changes is cheap when the cache exists, and the workspace is the audit trail for skill-creator's anti-hallucination check. Cleanup is never automatic; users own deletion (`rm -rf ~/.claude/skill-from-docs/<tool-slug>/`). Workspaces are typically <50 MB; aggressive eviction isn't worth the risk of nuking a working set the user wanted to refresh.

### Step 0 — Classify the docs

Before fetching anything, identify which of six archetypes the docs fit. The archetype dictates which discovery sources to try first, which to skip, and where the hidden failure modes are. Skipping classification is the single biggest source of wasted tokens in this workflow.

The six archetypes, at a glance:

1. **Well-structured docs site** (Stripe, FastAPI, Tailwind) — `llms.txt` or sitemap usually sufficient alone.
2. **Sparse README + examples** (small libs, many Rust crates) — GitHub-first; `/examples` folder is the real doc.
3. **Multi-source scattered** (FuseSoC, Yosys, Waybar) — enumerate all sources exhaustively before fetching.
4. **OpenAPI-only** (many fintech, self-hosted tools) — parse the spec programmatically; supplement prose from landing page.
5. **SPA / JS-rendered** (Notion-hosted, some Mintlify) — check for repo-side config file (`mint.json`, `sidebars.js`, `mkdocs.yml`) first; headless browser only as last resort.
6. **Non-English partial** (didox, regional APIs) — language preservation + expanded discovery from both section root and docs root.

Read `references/archetypes.md` for the recognition signals, real-world examples, and per-archetype strategy. Multiple archetypes can co-apply — pick the primary and layer the secondary strategy on top.

For fully worked walkthroughs on realistic cases, the `references/` directory contains:

- `case-study-fusesoc.md` — deep walkthrough of the *multi-source scattered* archetype, end-to-end through the harvest. Read this before attempting multi-source cases; it teaches decisions the rules can't capture.
- `case-study-resend-spa.md` — focused vignette on the *SPA / JS-rendered* archetype. Covers the probe order that prevents the common "reach for a headless browser too early" mistake.
- `case-study-yandex-nonenglish.md` — focused vignette on the *non-English partial* archetype. Covers the parallel-language-version decision and identifier preservation.

Read the one that matches the primary archetype you classified. If two archetypes co-apply, read both.

### Step 1 — Discovery (find every page *before* fetching any)

Naive crawl-from-entry misses too much. First enumerate, then fetch.

The default priority order below is tuned for an unknown docs site. If Step 0 classified the docs into a specific archetype, follow that archetype's strategy from `references/archetypes.md` — it will reorder this list (e.g. for "sparse README" the GitHub sources come first; for "OpenAPI-only" the spec fetch is the whole discovery).

1. `<host>/llms.txt` and `<host>/llms-full.txt` — if present, frequently sufficient on their own. Try these first.
2. `<host>/sitemap.xml` (and `<host>/robots.txt`, which often points to more sitemaps).
3. OpenAPI / Swagger spec: `<host>/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/api-docs`, `/v1/openapi.json`.
4. The entry page's own nav sidebar — fetch the entry URL and parse `<a href>` links, filtered to same-host docs paths.
5. GitHub (if repo exists): `README.md`, `/docs/**`, `/examples/**`, `CHANGELOG.md` or `NEWS`, `/cookbook/**`, `/recipes/**`, `/tests/userguide/**` (some projects put canonical examples here).
6. Package registry page: pypi.org, npmjs.com, crates.io, docs.rs — the project description there usually has a usage example the site doesn't.
7. Official CLI `--help` output if the tool ships a CLI. For tools with many subcommands, iterate over all of them.
8. If the site is on ReadTheDocs: try `<host>/_/downloads/en/stable/htmlzip/` and `<host>/_/downloads/en/stable/pdf/` — these bundle the entire docs corpus in one fetch.

For the detailed per-platform patterns (Docusaurus, Mintlify, ReadTheDocs, MkDocs, GitBook, Swagger UI, Notion docs) see `references/discovery.md`.

For archetype 4 (OpenAPI-only), the discovery cascade is implemented in `openapi-harvest fetch`. The cascade tries direct fetch → common spec paths (`/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/v3/api-docs`, `/api-docs`, `/api/v1/openapi.json`, `/spec.json`) → renderer-config extraction from HTML view-source (Swagger UI, ReDoc, Stoplight Elements, Scalar, RapiDoc — see `references/discovery.md` for the patterns). See `references/case-study-hetzner-openapi.md` for a worked example.

**Handling the partial-URL case.** If the user gave `site.com/ru/integration-registration`:
- Fetch the page itself *and* the section root (`site.com/ru/`) *and* the docs root (`site.com/`).
- Most docs sites expose the full sidebar on every page — extract it from one of those three.
- If the sidebar is client-rendered and returns empty from a plain fetch, that's the signal to use a headless browser (see below).

**When to fall back to a browser tool.** Signals:
- Plain fetch of the docs root returns a nearly-empty HTML shell (React/Vue SPA).
- Sitemap and llms.txt are both absent *and* sidebar links don't appear in raw HTML.
- Docs are behind a JS-triggered auth / cookie wall.

Which tool to reach for, in priority order:

1. **A locally-installed browser-automation skill** — e.g. `browse`, `gstack`, or whatever appears in `available_skills`. These don't need a separate MCP handshake and are usually the fastest path when present.
2. **A browser MCP server** if one is configured — `chrome-use`, Playwright MCP, Puppeteer MCP.
3. **Ask the user to paste the rendered HTML** of the docs sidebar as a last resort.

If multiple are available, prefer whichever is already proven in the current session (e.g. the skill you used to open a page earlier in this conversation) — it avoids re-probing that a second tool is configured correctly. Only use the browser when plain fetch fails; it's much slower and more fragile.

### Step 2 — Fetch exhaustively

Run the URL queue from Step 1. Persist the queue to `~/.claude/skill-from-docs/<tool-slug>/url-queue.json` so the "Refresh" cache option in Phase 0.5 can re-use it. For each URL:

1. Fetch it (WebFetch, or headless browser if needed).
2. Save the raw content to `~/.claude/skill-from-docs/<tool-slug>/raw/<slug>.md`.
3. Scan the fetched content for newly-referenced doc URLs (inline links, "see also", API-reference cross-links). Add any new same-host doc URLs to the URL queue.
4. **Also collect every image reference** — `![alt](url)`, `<img src>`, and any markdown links pointing at `.png|.jpg|.jpeg|.gif|.webp|.svg`. Append each to a separate `image-queue` (with the alt text and a few sentences of surrounding prose for context). Step 2.5 will work that queue.

Stop when a full pass over the URL queue produces no new URLs. Many docs sites split "guides" and "reference" into separate trees with few internal links — confirm both trees are covered. If the tool has an OpenAPI spec, parse it and make sure every path and schema has a home in the consolidated doc.

### Step 2.5 — Image extraction (heuristic-gated)

Many tools' docs lean on diagrams (architecture sketches, sequence diagrams, flowcharts, screenshots with annotated callouts) for content that the prose only hints at. Skipping these silently drops content that is often more dense than the surrounding text. But pushing every favicon and decorative banner through vision is wasteful, so gate carefully.

**Step 2.5 is skipped entirely** when Step 0 classified the docs as **Archetype 4 (OpenAPI-only)** — those docs almost never carry diagram content; what looks like an "endpoint icon" is decorative.

**For all other archetypes:**

1. Walk the `image-queue` from Step 2.
2. For each image, apply the **text-bearing heuristic** — trigger the vision pass if *any* of these match:
   - Filename contains `diagram`, `figure`, `fig`, `chart`, `flow`, `arch`, `sequence`, `architecture`, `topology`.
   - Alt text length > 30 characters (decorative icons usually have ≤ 10-char alts or empty alts).
   - Image is referenced from prose containing "see figure", "as shown", "diagram below", "the following diagram", "illustrated above/below".
   - HTTP HEAD reports `Content-Length` > 30 KB (decorative icons are typically ≤ 20 KB).
3. **Archetype-aware budget tightening:**
   - Archetypes 1 (well-structured) and 5 (SPA): if the image-queue exceeds 50 entries and the heuristic matches < 25 % of them, only run the vision pass on heuristic matches and capture alt+caption-only for the rest.
   - Archetypes 2 (sparse), 3 (multi-source), and 6 (non-English): run vision on every heuristic match without further budget gating — these archetypes lean heaviest on diagrams.
4. For each image flagged for vision:
   1. Fetch the image with WebFetch and save to `~/.claude/skill-from-docs/<tool-slug>/images/_raw/<n>.<ext>`.
   2. Open it with the multimodal `Read` tool to produce a transcription. Capture: visible text (verbatim, including labels on arrows and box titles), structural description ("3-column flowchart, left column 'Input', middle 'Process', right 'Output'"), any code or tables embedded in the image.
   3. Write `images/<source-slug>-<n>.md` with sections: `source_url`, `alt_text`, `caption_context` (the surrounding prose from Step 2), and `transcription`.
5. For images that fail the heuristic, write `images/<source-slug>-<n>.md` with only `source_url`, `alt_text`, `caption_context`, and `transcription: <skipped: did not pass text-bearing heuristic>`.
6. Maintain `images-manifest.json`: an array of `{image_url, local_text_path, referenced_in_section, has_text_content}` records, one per image in the queue. Step 5 hands this off.

**Inlining into `docs.md` happens in Step 3** — Step 2.5 only produces the per-image sidecars and the manifest.

If the multimodal Read tool was missing in preflight, skip steps 4–5 entirely and record every image in the manifest with `has_text_content: unknown`. The handoff packet flags this so skill-creator knows the diagrams were not transcribed.

### Step 3 — Consolidate

Merge `raw/*.md` into one `docs.md` at `~/.claude/skill-from-docs/<tool-slug>/docs.md`. Use the template in `references/doc-template.md`. Core rules:

- **Provenance.** Every top-level section ends with `<!-- source: <url> retrieved: <YYYY-MM-DD> -->`. This is what makes the skill refreshable later when the upstream docs change.
- **Version + date header.** Top of the file records tool version (if known), docs site version, retrieval date, and docs language (e.g. `language: ru` if non-English).
- **Code verbatim.** Never paraphrase code. Copy examples byte-for-byte. Correctness depends on exact identifier names, quoting, import paths.
- **Prose, tighten.** Deduplicate auth boilerplate that repeats on every page. Strip marketing copy. Keep the technical substance.
- **Language preservation.** If docs are non-English, keep identifiers, parameter names, endpoint paths, and error codes in the original. Translate only prose needed for comprehension, and mark translations with `[EN]`.
- **Inline image transcriptions.** For every image referenced in `docs.md`, replace the original `![alt](url)` with the transcription text from `images/<source-slug>-<n>.md` (the `transcription` field), then immediately follow it with `<!-- image: see images/<source-slug>-<n>.md (source: <image_url>) -->`. This keeps `docs.md` self-contained for skill-creator while preserving the path back to the per-image sidecar. For images that did not pass the heuristic, leave the original `![alt](url)` and append `<!-- image: skipped, decorative; see images/<source-slug>-<n>.md -->`.
- **Flag gaps explicitly.** Where a topic is referenced but not documented, leave `<!-- TODO: no official coverage of <topic>; see <source-hint> -->` so the next phase sees the gap instead of silently skipping it.

### Step 4 — Completeness check

Before moving to Phase 2, verify `docs.md` covers every item below. For each missing item, use `WebSearch` *before* giving up.

- [ ] Installation — every supported method (pip / npm / cargo / brew / docker / from source)
- [ ] Authentication and configuration — tokens, env vars, config files, OAuth flow. If archetype 4 AND the auth section is missing or partial AND the user has supplied a token, run `openapi-harvest auth --allow-host <host> --token $TOKEN <known-good-endpoint>`. The captured response counts as a *scoped probe-source* per the probing-as-evidence model in `references/probing-tools.md` — provenance comment carries a `scope` label, never doc-page provenance. `--allow-host` is REQUIRED; without it the tool exits 1 (prevents poisoned-spec credential exfiltration).
- [ ] Core concepts and data model
- [ ] Full API surface (REST paths, SDK methods, or CLI commands — whichever applies)
- [ ] At least one minimal end-to-end working example in the target language
- [ ] Error handling — known error codes, common errors, how to recover
- [ ] Rate limits, quotas, pagination, versioning policy
- [ ] Gotchas the docs warn about explicitly
- [ ] Tool version number and docs retrieval date

Useful `WebSearch` queries for gap-filling: `"<tool> getting started"`, `"<tool> example <language> site:github.com"`, `"<tool> API reference"`, `"<tool> rate limit"`. Prefer official sources, then maintainer blogs, then community content. Mark every web-sourced section with its URL in the same provenance comment format.

If any checklist item is still empty after web search, surface the gap to the user before Step 5 rather than handing off a packet with silent holes.

### Step 5 — Build the handoff packet

The harvest itself is now complete. Before invoking skill-creator, materialise a single JSON file at `~/.claude/skill-from-docs/<tool-slug>/handoff.json` that pre-fills the answers skill-creator's interview will ask. The handoff packet's job is to *describe* what was found in the docs — never to *decide* what the resulting skill looks like.

Fields:

- `version: 1` — handoff format version. Bump if fields change so skill-creator can branch on it.
- `proposed_name` — `<tool-slug>-integration` as a *suggestion only*. skill-creator may rename during interview.
- `tool_summary` — one paragraph pulled from the harvested docs explaining what the tool is and what problem it solves. Verbatim from the docs where possible.
- `user_declared_scope` — verbatim from Phase 0 question 4 (e.g. "minimal create-payment-intent flow", "full OAuth + webhooks", "production-ready Node SDK setup").
- `user_declared_languages` — from Phase 0 question 3 (array, e.g. `["python"]` or `["language-agnostic"]`).
- `archetype_primary`, `archetype_secondary` — source-shape archetype IDs from Step 0 (1–6).
- `content_shape_signals` — **neutral observations only**. Examples of acceptable signals: `top_level_h2_count`, `repeated_factor_like_sections: 15` (when the docs have 15 parallel-shaped chapters), `repeated_endpoint_like_sections`, `repeated_module_like_sections`, `has_openapi_spec`, `multi_language_docs`, `code_block_languages: ["python", "typescript"]`. **Do not** decide "this is N independent variants" or "this should be split per-resource"; that's skill-creator's call. Surface the signal; let the interview interpret. Archetype-4 harvests SHOULD populate five OpenAPI-specific signals: `has_openapi_spec` (bool), `spec_url` (string), `spec_format` (one of `openapi-3.0`, `openapi-3.1`, `swagger-2.0`), `endpoint_count` (int), `tag_count` (int). All five are optional v1 extensions; absent keys mean "not yet checked" and readers MUST NOT infer false. Other archetypes typically omit them. When `openapi-harvest auth` has run, two additional signals appear: `auth_method` (one of `bearer`, `auth_token_header`, `api_key_header`, `basic`, `query_string`) and `security_warnings` (list of policy notes the generated integration skill MUST surface to users — query-string credentials leak via logs/proxies/caches, Basic credentials should load from env vars, etc.). `provenance_index` carries `sources` (spec-derived URLs with optional JSON Pointers) and `probes` (captured live responses with method, URL, status, scope, fixture path) in separate arrays so downstream verifiers can apply different trust levels.
- `coverage_checklist` — Step 4 checklist with each item marked `covered`, `partial`, or `missing` plus the source URL(s).
- `gap_list` — explicit unresolved gaps from Step 4, even after WebSearch. Empty array if none.
- `provenance_index` — map of `docs.md` H2 section → list of source URLs. Used by skill-creator for its own anti-hallucination check.
- `image_inventory` — array of `{image_url, local_text_path, referenced_in_section, has_text_content}` derived from `images-manifest.json`.
- `suggested_test_cases` — 3–5 trigger phrases pulled from the docs ("create a payment intent", "add OAuth to my Express app", etc.), each marked as a *suggestion* not a directive.
- `harvest_metadata` — `{retrieved_date, tool_version, raw_page_count, docs_md_token_count}`.

Also update `images-manifest.json` to reflect the final state (any images excluded after the heuristic pass should be present with `has_text_content: false`).

The full workspace at this point matches the layout in the Phase 1 intro. Hand off to Phase 2.

---

## Phase 2 — Handoff

This skill never writes a SKILL.md. The handoff is a single invocation:

> Invoke `skill-creator:skill-creator` with `workspace_path=~/.claude/skill-from-docs/<tool-slug>/` (absolute path). The packet at that path contains `docs.md`, `handoff.json`, `images/`, `images-manifest.json`, and `raw/`. Treat `handoff.json` as pre-filled answers to the standard interview — confirm or override each with the user, but do not skip the interview. Do not pass `docs.md` as a blob — pass the path.

If skill-creator's interview always starts cold (no workspace-path channel exists yet), tell the user this verbatim: *"skill-creator will ask you four questions next. The answers are pre-filled in `~/.claude/skill-from-docs/<tool-slug>/handoff.json` — `proposed_name`, `tool_summary`, `user_declared_scope`, `user_declared_languages`, plus content_shape_signals and provenance_index. Paste from there as it asks."*

**Do not pre-decide for skill-creator:**
- The skill's name (only suggested).
- The skill's body structure (number/order of H2 sections).
- Whether references are monolithic (`references/api.md`) or split per variant (`references/factor-01.md`, `references/factor-02.md`, …). The `content_shape_signals` describe; skill-creator decides.
- Which "common patterns" or recipes appear in the body.
- The trigger description (only suggested phrases).

**Hand-off note: anti-hallucination check.** After skill-creator produces the skill, it should walk every endpoint, method name, parameter, env var, and error code in the produced files and confirm each is traceable to a section in the harvested `docs.md` via `provenance_index`. Anything that fails this trace was hallucinated and must be removed. The handoff packet's `provenance_index` makes this check mechanical. This is the single most important quality gate downstream — but it lives in skill-creator's process, not here. (When skill-creator is missing, Phase 0.5 has already aborted, so we never reach this point with an undefined destination.)

---

## Anti-patterns

- **Fetching only the entry URL.** The most common failure mode. Always run Step 1 discovery first.
- **Inventing endpoints or parameters** that "should probably exist" by analogy to similar tools. Not in docs → not in skill.
- **Paraphrasing code examples.** Correctness depends on exact form. Copy verbatim.
- **Dropping source language.** Silently translating identifiers or error codes breaks the skill. Keep originals, annotate with `[EN]` where helpful.
- **Skipping the completeness check.** Missing auth or install sections make the handoff packet unusable downstream.
- **Pre-deciding output shape during harvest.** This skill describes signals; skill-creator's interview decides structure. Do not collapse 15 parallel chapters into one section because "the body should be ~400 lines"; do not split a single API into per-resource files because "this looks like multiple variants". Surface the signals in `content_shape_signals` and let skill-creator interpret.
- **Skipping image extraction on text-bearing diagrams.** A diagram with arrows, labels, or transcribed code is content, not decoration. Run the heuristic; trust it. The opposite mistake — running vision on every favicon — is what the heuristic exists to prevent.
- **Writing harvest artifacts to a relative path.** Always write to `~/.claude/skill-from-docs/<tool-slug>/`. Relative paths pollute whichever project the user happens to be in and orphan the cache.
- **Producing a SKILL.md as a fallback.** There is no fallback. If skill-creator is missing, Phase 0.5 aborts. Local fallback templates re-encode structural decisions that conflict with skill-creator and are how this skill ended up over-stepping in the first place.
- **Scraping before discovery.** Slower, incomplete, and wastes tokens on the same sidebar HTML repeatedly.
- **Treating a captured probe response as if it were a doc page.** Probe provenance comments use `<!-- probe: METHOD URL status: N retrieved: DATE scope: LABEL fixture: PATH -->`; doc provenance uses `<!-- source: URL retrieved: DATE raw_file: PATH -->` (with optional `spec-pointer: JSONPOINTER` for archetype 4). Mixing them breaks skill-creator's verifier.
- **Running probes without `--allow-host`.** A poisoned spec can specify an attacker-controlled endpoint URL. `openapi-harvest` enforces an allowlist on every outbound call; bypassing it is a security policy violation.
