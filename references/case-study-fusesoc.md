# Case study: building `fusesoc-integration` from scratch

A fully worked walkthrough of this skill running against a real, messy open-source tool. Read this before attempting hard cases — it makes the abstract phases concrete.

**The setup.** A user asks:

> "I want a skill for integrating FuseSoC into my HDL build pipeline. Docs are at https://fusesoc.readthedocs.io. Target language Python. Scope: use it as a library to configure and run simulations, not just from the CLI."

---

## Phase 0 — Confirm inputs

The four required answers are mostly present. Confirm back to the user and fill the one gap:

- **Tool name**: FuseSoC ✓
- **Entry URL**: `https://fusesoc.readthedocs.io` ✓ — but also note the GitHub repo at `https://github.com/olofk/fusesoc` since this is likely a multi-source tool (see below).
- **Target language**: Python ✓
- **Scope**: library-mode simulation runner, not CLI wrapping ✓

One ambiguity worth flagging: FuseSoC integrates tightly with Edalize (a companion tool). Ask whether the skill should also cover Edalize integration or treat it as a black-box dependency. Assume "black-box" unless told otherwise — this keeps scope disciplined.

---

## Phase 1, Step 0 — Classify

Glance at the entry URL and the repo. Signals:

- Hosted docs site exists and is not a JS shell (content renders in plain HTML) → not *SPA*.
- English docs, no language segment in URL → not *non-English partial*.
- Docs clearly have more than a README (multiple sections visible) → not *sparse README*.
- No Swagger UI at the entry → not *OpenAPI-only*.
- ReadTheDocs-hosted with proper sidebar → could be *well-structured docs site*.

But also:

- Docs explicitly reference a GitHub wiki (`github.com/olofk/fusesoc/wiki`).
- Release notes live in a `NEWS` file in the repo, not on the docs site.
- Examples live in `tests/userguide/` in the repo (using Sphinx `literalinclude`), not embedded on the docs site.
- A separate companion tool (Edalize) has its own docs.
- There are at least three active doc versions visible in the version selector (stable, latest, schemafix branch) with content drift between them.
- The CLI ships with substantial `--help` output.

**Classification: multi-source scattered (primary) + docs-in-code (secondary).** Route to the multi-source strategy and supplement with CLI help extraction.

---

## Phase 1, Step 1 — Discovery

Build the URL set *before* fetching page content. For a multi-source case, enumerate each source.

**Source 1: ReadTheDocs site.**

Try the quick wins first:
- `https://fusesoc.readthedocs.io/llms.txt` → not present (as of retrieval date).
- `https://fusesoc.readthedocs.io/sitemap.xml` → present, lists all pages under `en/stable/`, `en/latest/`, and a few tagged versions.
- PDF bundle: `https://fusesoc.readthedocs.io/_/downloads/en/stable/pdf/` → the entire stable docs in one PDF. Grab it. Same for htmlzip: `/_/downloads/en/stable/htmlzip/`.

Decision: fetch the htmlzip. It bundles every page with assets in one request. Much cheaper than crawling the sitemap page-by-page. Keep the sitemap URL list as a cross-reference to confirm the zip is complete.

**Source 2: GitHub repo.**

- `README.md` — short, mostly a pointer to the docs. Fetch for completeness.
- `/doc/source/` — the rST source of the ReadTheDocs site. Already have the rendered version from Step 2; skip unless the rendered HTML loses important rST directives (rare).
- `NEWS` — changelog. Fetch raw. Important for version-awareness.
- `/tests/userguide/blinky/blinky.core` and similar — these are the literal-include sources the docs reference. Fetch the whole `/tests/userguide/` directory.
- `pyproject.toml` — to record the actual package name and version for the install section.

**Source 3: GitHub wiki.**

- `https://github.com/olofk/fusesoc/wiki` → the main page. Fetch once to check.
- On inspection: the CAPI2 page says "CAPI2 documentation is now available here" and redirects to ReadTheDocs. The wiki is mostly deprecated.

**Decision: skip the wiki after the first page confirms it's deprecated.** Record the check in provenance so future refreshes don't re-investigate.

**Source 4: Companion tools.**

- Edalize: scoped out in Phase 0. Record as an external dependency with a pointer to its own docs; don't harvest.
- `fusesoc-cores` (the default core library at `github.com/fusesoc/fusesoc-cores`) — this is how FuseSoC discovers cores. Fetch its README to understand the library system in practice.

**Source 5: PyPI.**

- `https://pypi.org/project/fusesoc/` → confirms package name, current version, minimum Python, platform support.

**Source 6: CLI help.**

- `fusesoc --help` gives the top-level command list.
- For each subcommand (`list-cores`, `core-info`, `run`, `library`, etc.), `fusesoc <cmd> --help`.
- If the tool isn't installed in the working environment, extract help text from the source — `src/fusesoc/main.py` (or equivalent) defines the argparse tree.

**Final URL / source list** (before fetching page content):

```
HTMLZIP  https://fusesoc.readthedocs.io/_/downloads/en/stable/htmlzip/
GITHUB   https://github.com/olofk/fusesoc/blob/main/README.md
GITHUB   https://github.com/olofk/fusesoc/blob/main/NEWS
GITHUB   https://github.com/olofk/fusesoc/tree/main/tests/userguide/   (recursive)
GITHUB   https://github.com/olofk/fusesoc/blob/main/pyproject.toml
WIKI     https://github.com/olofk/fusesoc/wiki                         (one-shot check, skip if deprecated)
PYPI     https://pypi.org/project/fusesoc/
COMPANION https://github.com/fusesoc/fusesoc-cores/blob/main/README.md
CLI      fusesoc --help; fusesoc <each subcommand> --help
```

---

## Phase 1, Step 2 — Fetch exhaustively

Execute the list. Save each to `./skill-from-docs-workspace/fusesoc/raw/<slug>.md`.

Watch for these during fetch:
- **Version conflict.** ReadTheDocs has `stable` pinned at v2.4.5, `latest` at `2.4.5.devNN+g…`. Pick `stable` as canonical and note the dev version only in a "bleeding edge features" annotation. Do not mix content from both into the same section — that's how ghost features end up in the skill.
- **Literal-includes.** The rendered docs include content from `tests/userguide/blinky/blinky.core` via the rST `literalinclude` directive. If the rendered HTML is being used, those code blocks are already inlined and don't need re-fetching.
- **Deprecated subsections.** Some rendered sections carry a visible note: "This section was taken from older documentation and needs to be adjusted." Mark these with `<!-- quality: upstream flagged as stale -->` — do not silently omit, but surface for the user.

After fetching, do one queue pass for new links. Common additions surfaced this way:
- Links to specific GitHub examples (keep local, resolve when building the minimal-example section).
- Links to the "LED to Believe" project — a demo, not documentation; skip.
- Links to external tutorials / blog posts — only include if the completeness check (Step 4) later requires them.

---

## Phase 1, Step 3 — Consolidate

Merge `raw/` into `docs.md` using the template. The header for FuseSoC:

```markdown
# FuseSoC

- version: 2.4.5
- docs site: https://fusesoc.readthedocs.io/en/stable/
- retrieved: <YYYY-MM-DD>
- language: en
- target SDK(s): python (library mode)
- scope: configure and run simulations from Python; treat Edalize as external

## Coverage status
- [x] Installation
- [x] Authentication  (N/A — local tool, no auth; noted as such)
- [x] Core concepts (core files, VLNV, targets, flows, CAPI2)
- [x] Full API surface (CLI + Python module)
- [x] Minimal working example
- [x] Error handling (partial — see TODO in Errors section)
- [~] Rate limits, quotas, versioning (N/A for rate limits; version policy covered)
- [x] Gotchas
```

Section-level decisions for FuseSoC specifically:

- **Installation**: pip is canonical. Include Windows-specific note about `fusesoc.exe` PATH (present in docs). Do not include from-source install unless scope needs it.
- **Authentication**: write `_N/A — FuseSoC is a local tool without authentication. However, remote core libraries may require git credentials; see 'Library management' below._`
- **Core concepts**: VLNV, core, core file, target, flow, generator. Preserve FuseSoC's terminology exactly — do not rename "core" to "package" even though the docs say it's analogous.
- **API surface**: split into two — CLI reference (from `--help` outputs) and Python module reference (from the `/src/fusesoc/` module docstrings and the "using as a library" section of the docs).
- **Minimal example**: use the `fusesocotb` quickstart example from the companion repo — it's complete, runnable, and already exists. Copy verbatim.
- **Errors**: docs are thin here. Section in `user/knowledgebase.rst` has some common problems; include verbatim. Flag the gap with `<!-- TODO: systematic error code list not in upstream; consider adding from src/fusesoc/main.py exception handlers -->`.
- **Gotchas**: CAPI1 vs CAPI2, BUILD_ROOT redefinition, `.system` file deprecation — all from the migration guide, all real gotchas users hit.

Provenance comments at the end of every H2 section:

```markdown
## Core concepts
...content...
<!-- source: https://fusesoc.readthedocs.io/en/stable/user/overview.html retrieved: YYYY-MM-DD -->
<!-- source: https://github.com/olofk/fusesoc/blob/main/README.md retrieved: YYYY-MM-DD -->
```

---

## Phase 1, Step 4 — Completeness check

Against the checklist:

- Installation ✓
- Authentication ✓ (marked N/A with reason)
- Core concepts ✓
- Full API surface — **partial.** The Python library-mode API is mentioned in the docs but not fully documented ("Better usability as a module" in the 2.0 release notes). Targeted search: `"fusesoc" python library "import fusesoc"` → a few blog posts and GitHub issues fill the gap. Include with community-source provenance.
- Minimal example ✓
- Error handling — **partial, TODO flagged.** Don't block on this — the skill will be useful without a full error-code list, and the TODO is visible.
- Rate limits ✓ (N/A)
- Gotchas ✓

Surface the partial items to the user. Decide together whether to block Phase 2 on them or accept the gaps. For FuseSoC, the gaps are small enough to accept; the TODO markers preserve them.

---

## Phase 2 — Create the skill

Invoke `skill-creator` with `docs.md` as primary input. Target output:

```
fusesoc-integration/
├── SKILL.md
└── references/
    ├── api.md            # the full docs.md, preserved
    └── examples/
        └── blinky.core   # the canonical minimal example
```

**SKILL.md frontmatter:**

```yaml
---
name: fusesoc-integration
description: Integrate FuseSoC — the HDL package manager and build system — into Python projects. Covers pip install, writing .core files (CAPI2 YAML schema), using FuseSoC as a Python library to configure and run simulations (Icarus, Verilator, VHDL flows), managing local and remote core libraries, and the VLNV naming scheme. Use whenever the user wants to "integrate FuseSoC", "use FuseSoC from Python", "set up an HDL build pipeline", "run simulations programmatically", or asks how to work with .core files or CAPI2 schema.
---
```

**SKILL.md body, in order**:

1. **What this integrates** — FuseSoC as a Python library for HDL build orchestration. Scope is simulation configuration; synthesis and Edalize usage are out of scope (link to `references/api.md` for full surface).
2. **Install** — pip primary, Windows PATH note.
3. **Authenticate** — N/A, with the remote-library git-credential caveat.
4. **Minimal working example** — the `fusesocotb` quickstart, copy-paste-runnable.
5. **Common patterns** — three: (a) adding a local core library, (b) running a simulation target from Python, (c) handling a dependency tree with `core-info`.
6. **Troubleshooting** — the knowledgebase entries, CAPI1→CAPI2 migration, `.system` file deprecation.
7. **Reference** — pointer to `references/api.md`.

### Anti-hallucination verification pass

Before declaring done, read the produced skill end-to-end and cross-check every claim against `references/api.md`:

- Every CLI flag mentioned in SKILL.md exists in the captured CLI help output.
- Every Python API call mentioned in SKILL.md (e.g. `from fusesoc.config import Config`) traces to a real import path in the repo.
- Every `.core` file key used in examples exists in the CAPI2 schema section.
- No invented YAML keys, no invented CLI subcommands, no version-drifted features (e.g. a feature only in `latest`/dev that's not in `stable`).

Delete anything that doesn't trace. Add the verification comment at the top of SKILL.md:

```markdown
<!-- verified: all references traceable to references/api.md as of YYYY-MM-DD -->
```

---

## What this case study teaches

Reading this walkthrough in context before your own harvest gives you three things:

1. **Classification matters.** FuseSoC looks like a well-structured docs site at first glance (it has ReadTheDocs!) but it's really multi-source scattered. The wrong classification wastes the first three hours.
2. **Source-exhaustive ≠ page-exhaustive.** The value is in finding the sources, not in fetching every page of every source. The htmlzip trick collapses 40+ page fetches into one.
3. **Partial completeness is honest completeness.** FuseSoC's error-code documentation is genuinely thin upstream. The right response is a TODO marker the user can see, not an invented error-code taxonomy.
