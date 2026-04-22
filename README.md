# skill-from-docs

A Claude Code skill that turns a tool's documentation into another Claude Code skill — one that teaches a coding agent to install, configure, and integrate that tool into real projects.

Hand the skill a docs URL, a GitHub repo, or an OpenAPI spec. It exhaustively harvests the docs into a single markdown file with source provenance, then wraps that file in a structured integration skill. The output is ready to drop into `~/.claude/skills/` and use immediately.

## Why

Claude Code agents are good at using well-documented tools they were trained on. They are mediocre at using tools whose docs are sparse, scattered across a readthedocs site plus a GitHub wiki plus a `/examples` folder, written in a language other than English, or rendered by a JavaScript SPA.

The core value of this skill is *discipline during harvest*:

- **Classify the docs** into one of six archetypes before fetching anything, so the right discovery strategy is used first instead of last.
- **Enumerate all sources** before crawling any of them, to avoid spending effort on the wrong one.
- **Preserve source URLs** per section, so the generated skill is refreshable when the upstream docs change.
- **Verify against hallucination** after handoff — every endpoint and method in the produced skill must trace back to harvested text, or it gets deleted.

The result is integration skills that are accurate, bounded in scope, and maintainable.

## Install

Clone into your Claude Code skills directory:

```sh
# user-scoped (all projects)
git clone https://github.com/berdyh/skill-from-docs ~/.claude/skills/skill-from-docs

# or project-scoped
git clone https://github.com/berdyh/skill-from-docs .claude/skills/skill-from-docs
```

## Use

In Claude Code, invoke it naturally:

```
> I want a skill for integrating FuseSoC into my HDL pipeline.
  Docs: https://fusesoc.readthedocs.io. Target Python, library mode.
```

Or more minimally:

```
> Make a skill from these docs: <url>
```

The skill will confirm four things before touching the network (tool name, entry URL/repo, target language, integration scope), then run the two-phase workflow. Output goes into `./skill-from-docs-workspace/<tool-slug>/` during work and produces a ready-to-install integration skill at the end.

## What it produces

For a tool called `<X>`, you end up with a skill shaped like:

```
<X>-integration/
├── SKILL.md                 # install → auth → minimal example → patterns → troubleshooting
└── references/
    ├── api.md               # the full harvested docs, with source provenance per section
    └── examples/            # runnable code examples, if multiple
```

Every claim in `SKILL.md` traces back to a section in `references/api.md`. Every section in `references/api.md` carries a `<!-- source: <url> retrieved: <date> -->` comment, so when the upstream docs change you can re-fetch just the affected sections and diff.

## Anatomy

```
skill-from-docs/
├── SKILL.md                                  # main workflow: classify → harvest → wrap → verify
├── README.md
├── LICENSE
└── references/
    ├── archetypes.md                         # six docs archetypes + per-archetype strategy
    ├── discovery.md                          # per-platform patterns (ReadTheDocs, Mintlify, ...)
    ├── doc-template.md                       # structure for the consolidated docs.md
    ├── integration-skill-template.md         # structure for the output skill
    ├── case-study-fusesoc.md                 # deep walkthrough: multi-source scattered
    ├── case-study-resend-spa.md              # vignette: SPA / JS-rendered
    └── case-study-yandex-nonenglish.md       # vignette: non-English partial
```

The case studies are tiered. FuseSoC is a deep end-to-end walkthrough — read it once in full to understand how all four phases fit together. Resend and Yandex are shorter, focused on the one hard decision specific to their archetype. Read the case that matches what you classified in Phase 1 Step 0.

## The six docs archetypes

The skill handles any of these routinely:

1. **Well-structured docs site** — Stripe, FastAPI, Tailwind
2. **Sparse README + examples** — most hobby libraries, many Rust crates
3. **Multi-source scattered** — FuseSoC, Yosys, Waybar
4. **OpenAPI-only** — many self-hosted tools and fintech APIs
5. **SPA / JS-rendered** — Notion-hosted docs, some Mintlify sites
6. **Non-English partial** — regional APIs where the entry URL is a deep subpage

Full recognition signals and strategies in `references/archetypes.md`.

## Limits

- This skill covers harvesting and wrapping. It does not cover running the produced skill, evaluating its quality quantitatively, or iterating it against a test set — for those, use `skill-creator` directly on the harvested `docs.md`.
- Docs that are truly private (behind SSO, not on the public internet) are out of scope; the skill will surface the access problem rather than try to bypass it.
- Harvesting is bounded by a completeness checklist, not infinite crawling. If a section is genuinely undocumented upstream, the skill flags it with a `TODO` marker rather than inventing content.

## Contributing

Issues and PRs welcome. Particularly valued:

- Case studies for the remaining archetypes — *OpenAPI-only*, *sparse README + examples*, and *well-structured docs site* don't yet have dedicated walkthroughs. The last two are the lowest priority (they're the easy cases) but useful for completeness.
- New archetype-combination cases (e.g. *non-English + SPA*, *OpenAPI-only + non-English*).
- Additional platform patterns in `references/discovery.md`.
- Refinements to the archetype recognition signals.

## License

MIT. See [LICENSE](./LICENSE).
