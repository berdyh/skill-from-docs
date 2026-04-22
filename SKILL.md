---
name: skill-from-docs
description: Build a new Claude Code skill from a tool or library's documentation so a coding agent can install, configure, and integrate that tool into other projects. Use whenever the user provides a docs URL, a GitHub repo, a library name, or an OpenAPI spec and wants a reusable integration skill — phrases like "make a skill from these docs", "turn this API's docs into a skill", "I want a skill for integrating <tool>", "read the docs for <library> and build a skill", or pasting a single docs subpage and asking for full coverage. Also triggers when the docs are partial, multi-page, JS-rendered, or in a non-English language and require crawling, headless-browser rendering, or web-search supplementation to be complete before a skill can be written.
---

# skill-from-docs

Turn a tool's documentation into a Claude Code skill that a coding agent can use to integrate that tool into other projects.

Two phases:

1. **Harvest** — exhaustively collect the tool's documentation into one consolidated markdown file with source provenance.
2. **Wrap** — invoke the `skill-creator` skill on the consolidated file to produce the final integration skill.

The wrapping is easy. The hard part is the harvest: a single docs URL is almost always one leaf of a larger tree, and a naive fetch misses 60–90% of the surface. The value of this skill is forcing exhaustive discovery before anything else, then enforcing a completeness check before handoff.

---

## Phase 0 — Confirm inputs

Before touching the network, get explicit answers for these four. If any are missing or ambiguous, ask. Do not guess.

1. **Tool name** — canonical name (e.g. "Didox", "Stripe Node SDK", "Polars").
2. **Entry-point URL or repo** — docs site root, a docs subpage, a GitHub repo, or a package-registry page. If the user only gave a subpage like `api-docs.example.com/ru/integration-registration`, treat the host as the docs root and plan to cover all siblings.
3. **Target language / runtime** — Python, TypeScript, Rust, language-agnostic REST, CLI only, etc. This decides which SDK sections to prioritize. A skill that tries to cover every SDK is worse than a skill that covers one well.
4. **Integration scope** — minimal "hello world" call, full CRUD, OAuth flow, webhook handler, production-ready setup. Don't over-scope.

If the user has not provided any docs at all, ask for a URL or repo. Do not invent one.

---

## Phase 1 — Harvest

Target output: `./skill-from-docs-workspace/<tool-slug>/docs.md` — one consolidated file with every meaningful subsection, each annotated with its source URL.

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

- `case-study-fusesoc.md` — deep walkthrough of the *multi-source scattered* archetype, end-to-end across all four steps. Read this before attempting multi-source cases; it teaches decisions the rules can't capture.
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

**Handling the partial-URL case.** If the user gave `site.com/ru/integration-registration`:
- Fetch the page itself *and* the section root (`site.com/ru/`) *and* the docs root (`site.com/`).
- Most docs sites expose the full sidebar on every page — extract it from one of those three.
- If the sidebar is client-rendered and returns empty from a plain fetch, that's the signal to use a headless browser (see below).

**When to fall back to a headless browser** (chrome-use / Playwright / Puppeteer MCP):
- Plain fetch of the docs root returns a nearly-empty HTML shell (React/Vue SPA).
- Sitemap and llms.txt are both absent *and* sidebar links don't appear in raw HTML.
- Docs are behind a JS-triggered auth / cookie wall.

Only use the browser when plain fetch fails. It's much slower and more fragile.

### Step 2 — Fetch exhaustively

Run the URL queue from Step 1. For each URL:

1. Fetch it (WebFetch, or headless browser if needed).
2. Save the raw content to `./skill-from-docs-workspace/<tool-slug>/raw/<slug>.md`.
3. Scan the fetched content for newly-referenced doc URLs (inline links, "see also", API-reference cross-links).
4. Add any new same-host doc URLs to the queue.

Stop when a full pass over the queue produces no new URLs. Many docs sites split "guides" and "reference" into separate trees with few internal links — confirm both trees are covered. If the tool has an OpenAPI spec, parse it and make sure every path and schema has a home in the consolidated doc.

### Step 3 — Consolidate

Merge `raw/*.md` into one `docs.md`. Use the template in `references/doc-template.md`. Core rules:

- **Provenance.** Every top-level section ends with `<!-- source: <url> retrieved: <YYYY-MM-DD> -->`. This is what makes the skill refreshable later when the upstream docs change.
- **Version + date header.** Top of the file records tool version (if known), docs site version, retrieval date, and docs language (e.g. `language: ru` if non-English).
- **Code verbatim.** Never paraphrase code. Copy examples byte-for-byte. Correctness depends on exact identifier names, quoting, import paths.
- **Prose, tighten.** Deduplicate auth boilerplate that repeats on every page. Strip marketing copy. Keep the technical substance.
- **Language preservation.** If docs are non-English, keep identifiers, parameter names, endpoint paths, and error codes in the original. Translate only prose needed for comprehension, and mark translations with `[EN]`.
- **Flag gaps explicitly.** Where a topic is referenced but not documented, leave `<!-- TODO: no official coverage of <topic>; see <source-hint> -->` so the next phase sees the gap instead of silently skipping it.

### Step 4 — Completeness check

Before moving to Phase 2, verify `docs.md` covers every item below. For each missing item, do targeted web search *before* giving up.

- [ ] Installation — every supported method (pip / npm / cargo / brew / docker / from source)
- [ ] Authentication and configuration — tokens, env vars, config files, OAuth flow
- [ ] Core concepts and data model
- [ ] Full API surface (REST paths, SDK methods, or CLI commands — whichever applies)
- [ ] At least one minimal end-to-end working example in the target language
- [ ] Error handling — known error codes, common errors, how to recover
- [ ] Rate limits, quotas, pagination, versioning policy
- [ ] Gotchas the docs warn about explicitly
- [ ] Tool version number and docs retrieval date

Useful gap-filling searches: `"<tool> getting started"`, `"<tool> example <language> site:github.com"`, `"<tool> API reference"`, `"<tool> rate limit"`. Prefer official sources, then maintainer blogs, then community content. Mark every web-sourced section with its URL in the same provenance comment format.

If any checklist item is still empty after web search, surface the gap to the user before Phase 2 rather than producing a skill with silent holes.

---

## Phase 2 — Create the skill

Invoke the `skill-creator` skill, passing the consolidated `docs.md` as the primary input. See `references/integration-skill-template.md` for the exact target structure.

Summary of what the output skill should look like:

- **Name**: `<tool-slug>-integration` (e.g. `stripe-integration`, `didox-integration`).
- **Description** (for triggering): covers phrases like "integrate <tool>", "use <tool> in <language>", "set up <tool>", "add <tool> to my project". Include the specific language(s) actually covered.
- **Body**, kept under ~400 lines, structured as:
  1. Install
  2. Authentication / configuration
  3. Minimal working example (copy-paste-runnable)
  4. Common integration patterns (3–5 realistic recipes)
  5. Troubleshooting / common errors
- **Bundled reference**: include `docs.md` as `references/api.md` in the produced skill so a using-agent has the full source of truth when the SKILL.md body is not detailed enough.

### Anti-hallucination verification

After `skill-creator` produces the skill, read the whole SKILL.md and every reference file and check: is every endpoint, method name, parameter, env var, and error code traceable to a section in the harvested `docs.md`? If not, delete it. This is the single most important quality gate — generated integration skills fail in production when they reference APIs the tool does not actually have.

---

## Anti-patterns

- **Fetching only the entry URL.** The most common failure mode. Always run Step 1 discovery first.
- **Inventing endpoints or parameters** that "should probably exist" by analogy to similar tools. Not in docs → not in skill.
- **Paraphrasing code examples.** Correctness depends on exact form. Copy verbatim.
- **Dropping source language.** Silently translating identifiers or error codes breaks the skill. Keep originals, annotate with `[EN]` where helpful.
- **Skipping the completeness check.** Missing auth or install sections make the final skill unusable.
- **Over-scoping the final skill.** A skill covering "everything" is worse than one covering the user's declared integration scope well.
- **Scraping before discovery.** Slower, incomplete, and wastes tokens on the same sidebar HTML repeatedly.
