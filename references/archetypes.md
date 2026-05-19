# Docs archetypes

Most docs fall into one of six archetypes. Classify the docs you're given *before* fetching anything — the archetype tells you which discovery sources to try first, which to skip, and where the hidden failure modes are. Doing this wrong burns tokens on low-value crawls; doing this right often makes Phase 1 nearly free.

How to use this file: read the "Recognize" bullets for each archetype against the entry URL the user gave you. Often multiple archetypes co-apply (FuseSoC is both *multi-source scattered* and has *docs-in-code* via its CLI) — pick the primary one, note any secondary archetype, and combine strategies.

---

## 1. Well-structured docs site

A modern hosted docs site with a working search index, a version selector, a visible sidebar, and a dedicated /reference/ section.

**Recognize by**: the URL is a dedicated docs host (`docs.<tool>.com`, `<tool>.dev/docs`), the landing page has a search bar and sidebar on desktop, the HTML source contains navigation links (not a JS shell), usually built with Docusaurus, Mintlify, Nextra, or similar.

**Real examples**: Stripe, FastAPI, Tailwind CSS, tRPC, Prisma.

**Strategy**: try `<host>/llms-full.txt` first — if present it's usually the whole corpus in one file and Phase 1 collapses to a single fetch. If not, the sitemap is reliable. You rarely need GitHub or a headless browser. OpenAPI spec is often published and worth grabbing for API references.

**Pitfall**: these sites often have both "guides" (tutorials) and "reference" (API spec) as separate trees with few cross-links. Confirm both trees are in your URL set.

---

## 2. Sparse README + examples

A small library where the entire "docs" are the README plus a `/examples/` folder. No hosted docs site, or the hosted site is a thin auto-generated API reference.

**Recognize by**: docs link in repo points back to the README or to godoc.org / docs.rs (auto-generated); project has fewer than ~50 stars or is a single maintainer; `/examples` folder in repo; no `docs/` folder or only a stub.

**Real examples**: most hobby CLI tools, many Rust crates with only `docs.rs`, niche Python packages.

**Strategy**: GitHub-first, always. Fetch `README.md` raw, then `ls` the repo for `/examples/`, `/demo/`, `/sample/`, `/docs/` in that order. For each example file in those directories, fetch and include verbatim — the runnable code is the doc. Also check the package registry page (PyPI / npm / crates.io) — the description field there sometimes has content not in the README. Check `docs.rs/<pkg>` for Rust crates (this is usually richer than anywhere else).

**Pitfall**: the README often assumes prerequisites it doesn't list. Read the examples' imports and setup to infer install and env-var requirements that are never stated.

---

## 3. Multi-source scattered

Docs intentionally split across multiple locations — a main docs site *and* a GitHub wiki *and* READMEs in companion repos *and* inline CLI help. Often the result of an evolving project where no single source is canonical.

**Recognize by**: the hosted docs site references "see the wiki" or "see the examples repo"; the GitHub repo has a `/doc/` folder distinct from the README; there's a separate "companion" tool with its own docs (e.g. FuseSoC + Edalize, Poetry + poetry-core); explicit "this section needs updating" notes in the hosted docs; multiple docs versions visible with content drift between them.

**Real examples**: **FuseSoC** (see `case-study-fusesoc.md`), Yosys, Waybar, most of the Linux systems-tooling ecosystem (iwd, Hyprland plugins).

**Strategy**: enumerate sources *exhaustively* before fetching anything. Typical source set:
- Hosted docs (readthedocs / GitHub Pages): grab the sitemap and the PDF/htmlzip bundle if ReadTheDocs hosts it.
- GitHub main repo: `README`, `/doc`, `/docs`, `NEWS`, `CHANGELOG`, `/tests/userguide` or similar "literal include" source directories.
- GitHub wiki: `<repo>/wiki` — but beware, wikis are often semi-deprecated and just redirect to the hosted docs (don't spend tokens crawling a deprecated wiki).
- Companion tool repos and "example template" repos (often named `<tool>-template` or `<tool>-examples`).
- CLI help output if the tool ships a CLI.
- Package registry page.

Unify by deduplicating aggressively — the same content often appears in 2-3 places with slight edits.

**Pitfall**: the #1 trap here is spending effort on one source before realizing another source has better content. Always enumerate first, *then* fetch. Also: deprecated sources lie. If the wiki homepage says "content moved to docs site," don't bother crawling it.

---

## 4. OpenAPI-only

The "docs" are essentially a REST API specification rendered through a JS-based viewer, with little prose beyond endpoint descriptions. The spec is the doc; everything else is supplementary.

**Recognize by**: a docs URL that renders one of the standard OpenAPI viewers. View-source signatures, one-liner each:
- **Swagger UI**: `<script>` with `SwaggerUIBundle({` or `swagger-ui-init`.
- **ReDoc**: `<redoc spec-url="...">` tag or `<script src="redoc.standalone.js">`.
- **Stoplight Elements**: `<elements-api apiDescriptionUrl="...">` tag.
- **Scalar**: `<script id="api-reference" data-url="...">`.
- **RapiDoc**: `<rapi-doc spec-url="...">` tag.

Common spec paths to probe directly: `/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/v3/api-docs`, `/api-docs`, `/api/v1/openapi.json`, `/spec.json`.

**Real examples**: Hetzner Cloud (see `case-study-hetzner-openapi.md`), Linear, Fly.io machines API, Miniflux, many internal-turned-public APIs, machine-generated SDK docs.

**Why it's its own archetype**: two failure modes archetype 1 (well-structured) doesn't have. First, **auto-generation noise** — descriptions are often unedited internal field comments, including literal `"string"` placeholders, multiple ID fields (`uuid` plus `legacy_id` plus `name`) where only one matters in practice, and `example` values that don't parse against their own schema. Second, **spec-vs-reality drift** — specs lie about `Link` headers, `RateLimit-*` headers, error envelope details, and `nullable` semantics. The rendered Swagger UI hides this; the consuming agent only learns about the gap when an integration breaks against a real response.

**Strategy**: parse the spec programmatically — the rendered viewer is worthless to fetch. The discovery cascade lives in `openapi-harvest fetch`: direct fetch → common spec paths → renderer-config regex over view-source (the five viewers above) → community mirror fallback. Resolve `$ref`s with `prance[osv]` and emit a sidecar `source-map.json` so original JSON Pointers remain accurate after the flatten. Group endpoints by OpenAPI tag under per-tag H3 sub-sections beneath `## API reference` (per `doc-template.md`). Merge prose context — what the tool is, auth flow, rate limits, error semantics — from sibling narrative pages or the landing page. Every section carries a JSON Pointer provenance comment (`spec-pointer: /paths/~1v1~1locations/get`); community-mirror sources additionally carry `mirror: unofficial`.

**Optional probing branch.** When the user has a token, `openapi-harvest auth` confirms the working auth pattern (bearer / API-key / Basic / query-string) and `openapi-harvest probe` captures a real response. `openapi-harvest quick-diff` then reports header gaps, type mismatches, and placeholder values. Probe captures become **scoped evidence sources** with their own provenance shape (`<!-- probe: METHOD URL status: N retrieved: DATE scope: LABEL fixture: PATH -->`), distinct from spec sources. The winning pattern is classified into one of five `auth_method` values (`bearer`, `auth_token_header`, `api_key_header`, `basic`, `query_string`) and propagated to `handoff.json.content_shape_signals` along with any policy `security_warnings` — skill-creator reads these to decide what the generated integration skill must warn users about. Header-based auth is preferred automatically: when `--spec PATH` is passed and the spec declares any header-based scheme, query-string patterns drop out of the cascade even if `--include-query-auth` was set. Security caveats are non-negotiable: every outbound call requires `--allow-host HOST`, query-string auth is opt-in (`--include-query-auth`) and triggers a "logs/proxies/caches leak" warning when it wins, Basic requires `--basic-creds USER:PASS` (CLI; warns) or `--basic-creds-env VARNAME` (preferred — reads `USER:PASS` from an env var instead of shell history) and triggers an "env vars not hardcoded" warning when it wins, default redaction covers auth headers and sensitive body keys and URL query strings, the 401-capture probe uses a fixed bad-token string, redirects are blocked by default, and spec descriptions are sanitized against prompt-injection before consolidation. Full inventory in `references/probing-tools.md`.

**Common gotchas**:
- Literal `"string"` placeholder values in `example` fields. Treat as missing example, not a real default.
- Multiple ID fields per resource (`uuid`, `id`, `legacy_id`, `name`). Note which one the API actually requires; consuming agents pick the wrong one consistently.
- Headers the spec body schema can't represent: `Link`, `RateLimit-*`, `Retry-After`, `X-Request-ID`, `Sunset`, `Deprecation`, `Warning`. Surface from a captured probe; flag as `<!-- TODO: capture via probe -->` if no token.

**Mirror staleness.** `openapi-harvest fetch` derives the staleness-check API target from the source URL — no hardcoded hosts. Four built-in styles work out of the box: GitHub (`raw.githubusercontent.com` → `api.github.com`), GitLab (`gitlab.com/<o>/<r>/-/raw/...` → `gitlab.com/api/v4`), Gitea (`codeberg.org/<o>/<r>/raw/branch/...` → Gitea v1 API), Bitbucket (`bitbucket.org/<w>/<r>/raw/...` → `api.bitbucket.org/2.0`). The fetcher warns on stderr if the file's last commit is older than `--staleness-days` (default 90). Self-hosted instances (Gitea, GitLab self-managed, Bitbucket Server, GitHub Enterprise) opt in via `--staleness-api-host HOST` + `--staleness-api-style {github,gitlab,gitea,bitbucket}`. Unknown hosts skip with a stderr note naming the flags that would enable the check; the harvest still succeeds.

**Neighbors**:
- **4 + 5 (SPA / JS-rendered)**: still archetype 4. The renderer is the SPA; the harvest target is the spec, not the rendered DOM. The five viewer signatures above route around the SPA layer entirely.
- **4 + 6 (Non-English partial)**: the spec is language-neutral (paths, parameter names, schema keys never translate). The narrative pages need the non-English handling from archetype 6, but the spec parse is unchanged.

**See**: `case-study-hetzner-openapi.md` for the end-to-end walkthrough — discovery cascade, mirror fallback, spec + narrative merge, optional probing, drift report, handoff packet.

---

## 5. SPA / JS-rendered

The docs site is a single-page application. A plain HTTP fetch returns a near-empty HTML shell. All content is loaded client-side.

**Recognize by**: raw HTML of the docs root is under ~10 KB or contains no visible links; `<div id="root">` or `<div id="app">` with nothing inside; `<noscript>` warns about JavaScript; Network tab shows XHR calls to a JSON API for content.

**Real examples**: Notion-hosted docs, some Mintlify sites (though most Mintlify docs also render server-side), GitBook sites, Linear's docs, various Webflow-hosted dev docs.

**Strategy**: skip straight to the config file on the repo side before reaching for a browser.
- **Mintlify**: look for `docs.json` in the repo root — it declares the full navigation. (Pre-2025 Mintlify used `mint.json`, which is deprecated but still appears in older repos.) Also try `<host>/llms-full.txt` first — Mintlify ships this by default for many customers and it's usually the whole corpus in one file.
- **GitBook**: sitemap usually still works even when HTML doesn't.
- **Docusaurus**: `sidebars.js` in the repo has the structure.
- **MkDocs**: `mkdocs.yml` in the repo has the page list.
- **Notion-backed**: no config file exists; headless browser is the only option.

If no config file is discoverable or the site isn't a known framework, reach for a browser tool — in priority order: a locally-installed browser-automation skill (e.g. `browse`, `gstack`), then a browser MCP (chrome-use, Playwright, Puppeteer), then asking the user to paste the rendered sidebar HTML. Be conservative: render the sidebar and the handful of pages you need, not the whole site.

**Pitfall**: a headless browser can get trapped in infinite scrollers, cookie banners, or JS-driven pagination. Set a hard page-count cap and fall back to "ask the user for the sidebar HTML" if it hits the cap.

**See**: `case-study-resend-spa.md` for a focused walkthrough of the probe order (`llms-full.txt` → framework config → sitemap → headless browser).

---

## 6. Non-English partial

The docs are in a non-English language, and the entry URL points to a specific subpage rather than the section root. Common in regional fintech, e-government APIs, and tooling from non-English software ecosystems.

**Recognize by**: the URL has a language segment (`/ru/`, `/zh/`, `/ja/`, `/uz/`); page content language differs from what the user's writing in; the given URL looks like a deep subpage (has 3+ path segments after the language code) without obvious sibling links rendered.

**Real examples**: Didox (Uzbekistan, Russian-language fintech), many CN cloud provider APIs (Alibaba Cloud, Tencent Cloud), Russian enterprise software (1C, Yandex Cloud), various regional banking APIs.

**Strategy**: combines the partial-URL handling and language preservation rules.
- Fetch the given page *and* the section root (`<host>/<lang>/`) *and* the docs root (`<host>/`).
- Most regional sites do render the sidebar server-side — check each of the three for link harvest before reaching for a browser.
- Preserve all identifiers, endpoint paths, parameter names, and error codes in the original language; never translate them.
- Translate only prose; mark first use of each translated technical term with `[EN]`.
- Record the language in the consolidated `docs.md` header (e.g. `language: ru`) so the handoff knows to preserve conventions.

**Pitfall**: auto-translation tools applied to code examples break them (they translate Python/YAML keys, string literals, etc.). Strict rule: code blocks are never translated, even when the surrounding prose is.

**See**: `case-study-yandex-nonenglish.md` for the two hard decisions — choosing between parallel EN/RU versions, and preserving identifiers when prose gets translated.

---

## Archetype combinations

Real tools often sit at the intersection of two archetypes. When you identify one, check whether a second applies, and layer the strategies:

- **Multi-source + docs-in-code**: FuseSoC, Yosys — the main docs are scattered *and* a CLI help command supplements them. Harvest both; CLI help often has the single-most-detailed option descriptions.
- **Non-English + SPA**: some Chinese cloud APIs. Apply language preservation *and* headless browser fallback.
- **OpenAPI-only + non-English**: regional fintech APIs where the spec is auto-generated and the prose is thin and localized. Prioritize parsing the spec; supplement prose in original language.
- **Sparse README + OpenAPI**: common for small self-hosted services. README for install/auth, OpenAPI for the actual API surface.

When in doubt, default to the **multi-source scattered** strategy — it's the most thorough and wastes the fewest decisions. The cost is more fetches; the benefit is not missing a source.
