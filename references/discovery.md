# Discovery patterns

Per-platform and per-source patterns for building an exhaustive URL list before fetching. Read this in Phase 1, Step 1.

## Ordered priority

Cheapest and most complete first:

1. **`llms.txt` / `llms-full.txt`** — an emerging standard for AI-readable docs indexes. Try first. If `llms-full.txt` exists, it often contains the entire docs corpus in one file and skips most of Phase 1 entirely. Common paths: `<host>/llms.txt`, `<host>/llms-full.txt`, `<host>/.well-known/llms.txt`.

2. **Sitemap** — `<host>/sitemap.xml` or `<host>/sitemap_index.xml`. Check `<host>/robots.txt` first — it frequently lists sitemap locations, including ones at non-standard paths. Sitemap gives the full URL set; filter to docs paths (`/docs/`, `/guide/`, `/reference/`, `/api/`).

3. **OpenAPI / Swagger spec**. For any tool with a REST API, look for:
   - `<host>/openapi.json` | `/openapi.yaml`
   - `<host>/swagger.json` | `/swagger.yaml`
   - `<host>/v1/openapi.json`, `<host>/api/openapi.json`
   - `<host>/api-docs`, `<host>/api-docs/v1`
   - GitHub repo: search for `openapi.yaml` or `openapi.json` in the repo.
   A spec gives the full API surface programmatically — every path, method, request/response schema — which is worth more than crawled HTML.

4. **Nav sidebar parsing**. Fetch the entry URL and the docs root; extract all `<a href="…">` links; filter to same-host docs paths; dedupe. Works for ~80% of docs sites. Breaks when the sidebar is client-rendered (see SPA section below).

5. **GitHub repository**. If the tool has a public repo:
   - `README.md` — usually the best single-page introduction.
   - `/docs/**` — long-form guides, often the source of the hosted docs site.
   - `/examples/**`, `/cookbook/**`, `/recipes/**` — runnable code that the docs site often doesn't surface.
   - `CHANGELOG.md` — version history; critical for knowing which API version the user is on.
   - `/src` or equivalent — last resort, for reading the actual code when docs are thin.

6. **Package registry**. The canonical page often has a usage section the docs site omits:
   - Python: `https://pypi.org/project/<pkg>/`
   - Node: `https://www.npmjs.com/package/<pkg>`
   - Rust: `https://crates.io/crates/<pkg>` (and the generated `docs.rs/<pkg>` page — often the richest Rust docs).
   - Go: `https://pkg.go.dev/<import-path>`
   - Ruby: `https://rubygems.org/gems/<pkg>`

7. **CLI `--help`**. If the tool ships a CLI, `<tool> --help`, `<tool> <subcommand> --help`. For tools with many subcommands, iterate over all of them. In a sandbox where the tool isn't installed, look in the repo for a `docs/cli/` directory or parse `--help` output from the source.

## Platform-specific notes

### Docusaurus (Facebook's open-source docs framework)

- Identified by `/docs/`, `/blog/`, and `docusaurus.config.js` in the repo.
- Sidebar structure lives in `sidebars.js` in the repo — fetching this from GitHub gives a clean URL tree without crawling.
- Search index at `<host>/search-index.json` on some versions.

### Mintlify

- Identified by a `mint.json` at the repo root (this is the canonical config).
- Fetch `mint.json` from the repo — it lists every page under `navigation`.
- Fast: this one file replaces crawling entirely.

### ReadTheDocs

- URL pattern: `<project>.readthedocs.io/en/<version>/`
- Sitemap reliably present at `<host>/sitemap.xml`.
- `objects.inv` (Sphinx inventory) at `<host>/objects.inv` — binary, but parseable with `sphobjinv` for a full symbol list.
- For offline reading: `<host>/_/downloads/en/<version>/htmlzip/` gives a complete zip of the docs.

### MkDocs / Material for MkDocs

- `mkdocs.yml` in the repo lists every page under `nav:`. Fetch from GitHub.

### GitBook

- Sitemap at `<host>/sitemap.xml`.
- Often exposes a JSON API — look for XHR requests in browser dev tools if manually inspecting.

### Swagger UI hosted endpoint

- The UI page itself is worthless to fetch; the `swagger.json` / `openapi.json` it points at is what matters. Look at page source for the spec URL.

### Notion-backed docs

- Hard to scrape from raw fetch (client-rendered). Go straight to headless browser, or check if the project also publishes a traditional docs site.

## Signals that mean "fall back to headless browser"

- Fetched HTML from the docs root is under ~5 KB or contains visibly no content links (React/Vue SPA shell).
- `<noscript>` block says "please enable JavaScript".
- Sitemap and llms.txt both absent.
- Docs are gated behind a cookie banner or auth wall that blocks WebFetch.

Specific options (availability varies by Claude Code install):
- chrome-use (Claude's Chrome extension)
- Playwright MCP server
- Puppeteer MCP server
- As a last resort, ask the user to paste the rendered HTML of the nav sidebar.

## Deduplication

Different discovery sources surface the same URLs with and without trailing slashes, with and without fragments. Before queueing:
- Strip `#fragment`
- Normalize trailing slashes
- Drop duplicates
- Drop obvious non-content URLs (`/_next/`, `/static/`, `.png`, `.svg`, `.css`, `.js`)
- Drop old-version URLs if a current version exists (e.g. `/v1/` when `/v3/` is the current version — ask user if unclear)
