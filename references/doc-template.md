# docs.md template

The consolidated doc produced in Phase 1 follows this structure. Filename: `~/.claude/skill-from-docs/<tool-slug>/docs.md`.

## Required header

```markdown
# <Tool name>

- **version**: <tool version if known, e.g. v3.4.1, or "unknown">
- **docs site**: <canonical docs URL>
- **retrieved**: <YYYY-MM-DD>
- **language**: <en | ru | de | ...>
- **target SDK(s)**: <python | typescript | ...>  (what this harvest focused on)
- **scope**: <what the user asked to integrate — e.g. "OAuth + invoice creation">

## Coverage status

- [x] Installation
- [x] Authentication
- [x] Core concepts
- [ ] Full API reference  <!-- partial: rate-limit section TODO -->
- [x] Minimal example
- [x] Error handling
- [x] Rate limits
- [x] Gotchas
```

The checklist at the top makes gaps immediately visible to the handoff agent. Do not ship `docs.md` with all items checked unless they genuinely are.

## Required sections, in order

Use these exact H2 headings. Omit a section only by replacing its body with `_Not documented upstream._` — never silently drop.

### `## Installation`

Every supported install method, with commands copy-paste-runnable. Example per method:

```markdown
### pip
```
pip install <pkg>
```

### npm
```
npm install <pkg>
```

### from source
```
git clone <repo>
cd <pkg>
<build command>
```
```

### `## Authentication`

- How to obtain credentials (UI path, signup URL, CLI command).
- Where to put them (env var name, config file path, constructor argument).
- Token type (bearer, API key, OAuth, HMAC signature, mutual TLS).
- Expiry and refresh behavior.
- A complete example showing the first authenticated call.

### `## Core concepts`

Data model, resources, lifecycle states. Keep the upstream terminology exactly — don't rename. Diagrams are fine as ASCII or mermaid.

### `## API reference` (or `## CLI reference`)

The full surface. Structure depends on the tool:

- **REST API**: group by resource. Each endpoint: method + path, auth requirements, request params (name, type, required, description), request body schema, response schema, status codes, one example request + response.
- **SDK**: group by module/class. Each method: signature, params, return type, raises, one example.
- **CLI**: group by subcommand. Each: purpose, flags, positional args, exit codes, example invocation.

### `## Minimal working example`

One end-to-end script in the target language that demonstrates install + auth + one real operation. Must be runnable as-is given credentials. If multiple languages were requested, one per language.

### `## Common patterns` (optional but recommended)

3–5 realistic recipes the user would plausibly need: pagination, bulk operations, webhook verification, error retry, etc. Each as a copy-paste snippet.

### `## Errors`

- Known error codes / exception types with when they occur and how to handle them.
- Common misconfigurations and their symptoms.

### `## Rate limits, quotas, versioning`

- Rate limit values and scope (per key? per IP? per endpoint?).
- How the API signals rate-limit responses (status code, headers).
- Pagination defaults and max values.
- API versioning scheme and policy.

### `## Gotchas`

Anything the upstream docs warn about explicitly. Keep this list tight — only items the docs actually flag, not speculation.

## Per-section provenance

Every H2 ends with an HTML comment footer:

```markdown
## Authentication

<section body here>

<!-- source: https://api-docs.example.com/ru/auth retrieved: 2026-04-22 -->
```

If a section was merged from multiple sources, list all:

```markdown
<!-- source: https://api-docs.example.com/ru/auth retrieved: 2026-04-22 -->
<!-- source: https://github.com/example/sdk/blob/main/README.md retrieved: 2026-04-22 -->
<!-- source: https://dev.example.com/blog/auth-deep-dive (community, 2025-11) -->
```

This makes the skill refreshable: to update, re-fetch each listed URL and diff.

## Marking gaps

Where a topic is referenced but not documented:

```markdown
## Rate limits, quotas, versioning

API rate limits are mentioned in passing ("contact support for higher limits") but specific numbers are not published.

<!-- TODO: no official coverage of rate-limit values; needs web search or support ticket -->
```

Never silently skip. The TODO markers are what the next phase and future refreshes rely on.

## Code blocks

- Always triple-fenced with language tag.
- Never paraphrase code. Copy byte-for-byte from the source.
- If docs showed pseudocode, mark it `# pseudocode` in a comment.

## Images

Image content from the harvest gets inlined into `docs.md` next to its original reference. The per-image sidecar at `~/.claude/skill-from-docs/<tool-slug>/images/<source-slug>-<n>.md` carries the full transcription; `docs.md` carries enough to be self-contained.

For images that passed the text-bearing heuristic (transcribed):

```markdown
The build pipeline has three stages, illustrated below:

> **Figure 1.** Three-column flowchart. Left column "Source" feeds into middle column "Compile" (boxes labelled `parse`, `typecheck`, `codegen`); middle column feeds into right column "Output" (boxes labelled `bundle`, `minify`, `emit`). An arrow from `typecheck` loops back to `parse` labelled "errors".

<!-- image: see images/build-pipeline-fig01.md (source: https://example.com/img/pipeline.png) -->
```

For decorative or icon-shaped images that did not pass the heuristic, keep the original reference and append a transclusion comment:

```markdown
![pip logo](https://example.com/pip-icon.png)
<!-- image: skipped, decorative; see images/pip-icon-fig07.md -->
```

The intent: a downstream agent reading `docs.md` should see image-derived content as prose, not as a broken `![alt](url)` reference. The sidecar exists for skill-creator's anti-hallucination check (image-derived claims are still traceable) and for refreshes.

## Non-English docs

- Identifiers, parameter names, endpoint paths, error codes: always in the original.
- Prose: translate to English and annotate with `[EN]` on first use of a translated technical term.

Example (Russian source):

```markdown
### Регистрация интеграции `/integration/register`

[EN] _Registers a new integration._ Returns an `integration_id` used in all subsequent calls.

Request:
```json
{"name": "...", "callback_url": "..."}
```

Response:
```json
{"integration_id": "...", "status": "active"}
```
```
