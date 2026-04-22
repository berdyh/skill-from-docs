# Vignette: SPA / JS-rendered — Resend (Mintlify)

A focused walkthrough of the *one* decision that's hard in the SPA archetype: **what to try before reaching for a headless browser.** This vignette skips the parts that look like the FuseSoC case (confirming inputs, consolidating, verifying) and concentrates on discovery.

---

## The setup

> "I want a skill for integrating Resend's email API into a Next.js backend. Docs: https://resend.com/docs. Target TypeScript."

---

## What plain fetch shows you

Fetch `https://resend.com/docs` as-is. You get HTML under ~15 KB that's mostly an empty `<div id="__next">` shell with a `<noscript>` tag telling you to enable JavaScript. The nav sidebar is not in the HTML. Links to subpages are not in the HTML.

**The wrong reflex at this point is reaching for chrome-use or a Playwright MCP.** It works, but it's slow, fragile, and burns tokens crawling a site a headless browser has to render page-by-page. For SPA-hosted docs there is almost always a faster path.

---

## The fast-path checklist

Run these four probes, in order. Stop as soon as one succeeds — later probes are fallbacks, not additive work.

### Probe 1: `llms-full.txt`

```
https://resend.com/docs/llms-full.txt
```

Mintlify added this feature in 2025 and many customers ship it by default. When present, it's the entire docs corpus — every page, concatenated, plain text — in one file. A single fetch replaces the whole crawl.

If this succeeds, Phase 1 is effectively done. Save it, add provenance metadata, move on to completeness check.

### Probe 2: Identify the framework

View source on the docs root. Look for one of these markers in the HTML `<head>` or near `<script>` tags:

- `mintlify` / `docs.json` / `_mintlify` → Mintlify (this case)
- `docusaurus` / `@docusaurus` → Docusaurus
- `nextra` → Nextra
- `vitepress` → VitePress
- Notion's `www.notion.so` asset paths → Notion-hosted

Each framework has a canonical config file in the docs repo that lists every page. Finding and fetching that file replaces crawling.

### Probe 3: Fetch the config from the repo

For Resend, the docs repo is `github.com/resend/resend` or sometimes a dedicated `github.com/resend/docs`. The Mintlify config lives at `docs.json` at the root:

```
https://raw.githubusercontent.com/resend/resend/main/docs.json
```

The `docs.json` structure has a `navigation` array with nested `tabs`, `groups`, and `pages`. Every `page` entry is a path (relative to the docs repo root) that resolves to an MDX file:

```json
{
  "navigation": {
    "tabs": [
      {
        "tab": "Send with Resend",
        "groups": [
          { "group": "Getting Started", "pages": ["introduction", "send-with-nodejs"] },
          { "group": "API Reference", "pages": ["api-reference/emails/send-email", ...] }
        ]
      }
    ]
  }
}
```

Walk the tree, collect every page path, and fetch each as a raw MDX file from the same repo. This gives you the full docs content without ever rendering JavaScript.

**Important (easy to get wrong)**: Mintlify deprecated `mint.json` in favor of `docs.json` in 2025. If your first instinct was to fetch `mint.json` and got a 404, that's why. Try `docs.json` next — not a headless browser.

### Probe 4: Sitemap, if the framework probe failed

```
https://resend.com/sitemap.xml
```

Even SPA sites usually have a server-rendered sitemap for SEO. Less efficient than `docs.json` (you have to fetch each page individually, and each fetch still gets a JS-rendered HTML) but works when no config file is accessible.

---

## When to actually use a headless browser

After all four probes fail. Concretely:

- `llms-full.txt` returns 404
- Framework markers absent or unrecognized
- Config file not accessible (private repo, or the docs aren't Git-backed — common for Notion-hosted docs)
- Sitemap absent or empty

For Resend, probe 1 usually suffices. For a generic Mintlify customer, probe 3 nearly always suffices. In practice the headless browser is needed mostly for Notion-hosted docs and a few custom SPAs.

---

## Per-framework quick reference

Skip the probes when the framework is obvious from the URL:

| Framework | Config file in repo | Notes |
|---|---|---|
| Mintlify | `docs.json` (was `mint.json` pre-2025) | Often also ships `llms-full.txt` |
| Docusaurus | `sidebars.js` or `sidebars.ts` | Plus `docusaurus.config.js` |
| Nextra | `_meta.json` per directory | No central file; walk the tree |
| VitePress | `.vitepress/config.mts` | `sidebar` property |
| MkDocs | `mkdocs.yml` | `nav:` key |
| Mintlify (monorepo-action) | `docs.json` in each subrepo | Aggregated at build time |

---

## Transferable lesson

The abstract rule ("fall back to headless browser when plain fetch fails") is wrong as stated — it has a middle step. The real rule:

1. Plain fetch the docs root.
2. If empty, check for an `llms-full.txt`.
3. If absent, identify the framework and fetch its config file from the docs repo.
4. If that fails, sitemap.
5. Only then, headless browser.

Most SPAs surrender to steps 2 or 3. Skipping them is a token-expensive mistake.
