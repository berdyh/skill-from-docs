# skill-from-docs

A Claude Code **discovery skill** — it exhaustively harvests a tool's documentation into a consolidated markdown bundle, then hands that bundle off to `skill-creator:skill-creator` to produce the actual integration skill.

This skill never writes a SKILL.md itself. It does the mechanical, expensive part (find every doc page, fetch them, transcribe diagrams, dedupe, flag gaps); skill-creator's interview owns the judgement-heavy part (name, scope, body structure, references layout). Two implementations of "what a skill looks like" was the original bug, so this one stops cleanly at the boundary.

Hand it a docs URL, a GitHub repo, or an OpenAPI spec. It produces a workspace at `~/.claude/skill-from-docs/<tool-slug>/` containing:

- `docs.md` — every meaningful doc page consolidated, with per-section source URLs and inlined image transcriptions
- `handoff.json` — pre-filled answers for skill-creator's interview (tool summary, declared scope, content-shape signals, coverage checklist, provenance index)
- `images/` + `images-manifest.json` — per-image transcribed sidecars
- `raw/` + `url-queue.json` — raw fetched pages and the URL queue (used by the "Refresh" cache option)
- `manifest.json` — append-only run log with a sha256 for every input and output, which `validate` checks

`docs.md` and `handoff.json` are the two artifacts that leave the machine, and
credentials are redacted from both. See [Credentials on disk](#credentials-on-disk)
for the one file that is different.

skill-creator reads from this workspace, runs its standard interview (overriding the pre-filled answers as needed), and produces the final `~/.claude/skills/<name>/` skill.

## Why

Claude Code agents are good at using well-documented tools they were trained on. They are mediocre at using tools whose docs are sparse, scattered across a readthedocs site plus a GitHub wiki plus a `/examples` folder, written in a language other than English, image-heavy, or rendered by a JavaScript SPA.

The core value of this skill is *discipline during harvest*:

- **Classify the docs** into one of six archetypes before fetching anything, so the right discovery strategy is used first instead of last.
- **Enumerate all sources** before crawling any of them, to avoid spending effort on the wrong one.
- **Transcribe text-bearing diagrams** through a heuristic-gated vision pass — flowcharts and architecture sketches often carry content the prose only hints at.
- **Preserve source URLs** per section, so the generated skill is refreshable when the upstream docs change.
- **Surface signals, not decisions.** The handoff packet describes what the docs contain (parallel-shaped sections, OpenAPI presence, languages used). It does not pre-decide the resulting skill's name, body length, or references layout.

The result is integration skills that are accurate, bounded in scope, and maintainable — produced by skill-creator from a packet that has done the hard reading.

## Install

**Requires** Anthropic's `skill-creator` plugin. The handoff in Phase 2 is the only way this skill produces output — there is no fallback. If `skill-creator` isn't installed, the skill aborts during preflight before touching the network. Install it via Claude Code's plugin marketplace first.

Then clone into your Claude Code skills directory:

```sh
# user-scoped (all projects)
git clone https://github.com/berdyh/skill-from-docs ~/.claude/skills/skill-from-docs

# or project-scoped
git clone https://github.com/berdyh/skill-from-docs .claude/skills/skill-from-docs
```

That is the whole install for five of the six archetypes. **Archetype 4
(OpenAPI-only)** additionally uses the `openapi-harvest` CLI, which ships in
`scripts/` and needs a second step — cloning alone does not put it on your PATH:

```sh
pip install -e ~/.claude/skills/skill-from-docs/scripts
# or, isolated:
pipx install -e ~/.claude/skills/skill-from-docs/scripts

openapi-harvest --help   # verify
```

Requires Python ≥3.10. Skip it if you never harvest OpenAPI-only APIs; the skill
detects the missing CLI during preflight and says so rather than failing midway.

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

The skill will confirm four things before touching the network (tool name, entry URL/repo, target language, integration scope), then run the harvest. All workspace artifacts live at `~/.claude/skill-from-docs/<tool-slug>/` — never in the current project directory. Re-running against the same tool surfaces the existing workspace and lets you choose **re-use** (default), **refresh** (re-fetch using cached URL queue), or **start clean** (wipe and re-harvest from scratch).

When the harvest is done, skill-creator is invoked against the workspace path. Its interview produces the final skill at `~/.claude/skills/<name>/`. The harvest workspace stays around as the audit trail and the basis for future refreshes.

## Anatomy

```
skill-from-docs/
├── SKILL.md                                  # main workflow: classify → harvest → handoff
├── README.md
├── LICENSE
├── references/
│   ├── archetypes.md                         # six docs archetypes + per-archetype strategy
│   ├── discovery.md                          # per-platform patterns (ReadTheDocs, Mintlify, ...)
│   ├── doc-template.md                       # structure for the consolidated docs.md
│   ├── probing-tools.md                      # openapi-harvest subcommand reference + security model
│   ├── case-study-fusesoc.md                 # deep walkthrough: multi-source scattered
│   ├── case-study-hetzner-openapi.md         # deep walkthrough: OpenAPI-only, with optional live probing
│   ├── case-study-resend-spa.md              # vignette: SPA / JS-rendered
│   └── case-study-yandex-nonenglish.md       # vignette: non-English partial
└── scripts/                                  # optional CLI dev kit for archetype-4 harvesting
    ├── README.md
    ├── pyproject.toml
    ├── src/skill_from_docs/                  # openapi-harvest CLI (6 subcommands over a shared substrate)
    └── test/                                 # offline pytest suite + fixtures
```

The case studies are tiered. FuseSoC and Hetzner are the two deep end-to-end walkthroughs — read whichever matches what you classified in Phase 1 Step 0, once in full, to see how all four phases fit together. Resend and Yandex are shorter, focused on the one hard decision specific to their archetype.

## The six docs archetypes

The skill handles any of these routinely:

1. **Well-structured docs site** — Stripe, FastAPI, Tailwind
2. **Sparse README + examples** — most hobby libraries, many Rust crates
3. **Multi-source scattered** — FuseSoC, Yosys, Waybar
4. **OpenAPI-only** — many self-hosted tools and fintech APIs
5. **SPA / JS-rendered** — Notion-hosted docs, some Mintlify sites
6. **Non-English partial** — regional APIs where the entry URL is a deep subpage

Full recognition signals and strategies in `references/archetypes.md`.

### Optional CLI dev kit for archetype 4

For OpenAPI-only APIs, `skill-from-docs` ships an optional CLI tool, `openapi-harvest`, in `scripts/`. It exposes six subcommands — `fetch`, `auth`, `probe`, `quick-diff`, `consolidate`, `validate` — covering spec discovery (across Swagger UI, ReDoc, Stoplight, Scalar, RapiDoc renderers), auth-pattern detection, scoped live probing with default-redaction and host-allowlist, spec-vs-reality drift reporting, workspace consolidation (emits both `docs.md` and `handoff.json`), and a "you're done when…" completion checkpoint.

Install with `pip install -e ./scripts`. See [scripts/README.md](scripts/README.md) for the full error contract, security model, and smoke-test sequence, and [references/case-study-hetzner-openapi.md](references/case-study-hetzner-openapi.md) for a worked walkthrough against Hetzner Cloud (works offline using bundled fixtures, no third-party signup required).

Its security defaults are restrictive on purpose, and each one is a deliberate
choice rather than an oversight: `--allow-host` is **required** before any
credentialed request, and the allowlist is bound to the HTTP client itself so a
call to an unlisted host fails structurally rather than by convention; redirects
are never followed; query-string and Basic auth are opt-in; request/response
redaction and description sanitizing are on by default. Loosening any of them is
a behaviour change — the rationale for each is in
[references/probing-tools.md](references/probing-tools.md).

### Credentials on disk

If you harvest a spec whose URL carries a credential
(`https://api.example.com/spec.json?key=<secret>`), the workspace keeps the
usable URL in **`raw/source-map.json`** under `fetch_url`, and that file is
created mode `0600`.

It has to be stored somewhere: the recorded URL is the audit trail, and a
redacted one cannot be re-fetched — `validate --network` would try it and report
a failure that is not real. Everything else records only the redacted form.
`docs.md`, `handoff.json`, `manifest.json` and every probe fixture are clean, and
`consolidate`/`probe` read through an accessor that strips the field, so they
cannot leak a value they are never handed.

What this means for you: the workspace is as sensitive as the credential you
gave it. Do not commit it or copy it to a shared machine. Workspaces created
before this behaviour existed have no `fetch_url`; `validate --network` skips the
re-fetch and says so rather than failing.

## Limits

- This skill covers harvesting only. It does not produce a SKILL.md, choose an output structure, or evaluate the resulting skill — those live in `skill-creator`.
- Docs that are truly private (behind SSO, not on the public internet) are out of scope; the skill will surface the access problem rather than try to bypass it.
- Harvesting is bounded by a completeness checklist, not infinite crawling. If a section is genuinely undocumented upstream, the skill flags it with a `TODO` marker rather than inventing content.
- Image extraction is heuristic-gated. Decorative icons are skipped; diagrams and figures get a vision pass. The heuristic is conservative — if a diagram is missed, refresh and the user can flag it.

## Contributing

Issues and PRs welcome. Particularly valued:

- Case studies for the remaining archetypes — *sparse README + examples* and *well-structured docs site* don't yet have dedicated walkthroughs. Both are low priority (they're the easy cases) but useful for completeness.
- New archetype-combination cases (e.g. *non-English + SPA*, *OpenAPI-only + non-English*).
- Additional platform patterns in `references/discovery.md`.
- Refinements to the archetype recognition signals.

## License

MIT. See [LICENSE](./LICENSE).
