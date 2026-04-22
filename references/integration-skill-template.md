# Integration skill template

The skill produced in Phase 2 targets a coding agent (Claude Code), not a human reader. This template describes what to tell `skill-creator` to produce.

## Directory layout

```
<tool-slug>-integration/
├── SKILL.md                  # entry point, <400 lines
└── references/
    ├── api.md                # the full docs.md from Phase 1, lightly reorganized
    ├── examples/             # runnable example files, if >1
    └── troubleshooting.md    # errors and recovery, if long enough to split
```

## SKILL.md structure

### Frontmatter

```yaml
---
name: <tool-slug>-integration
description: <two-to-four sentences covering what the tool does, what this skill enables, and specific trigger phrases. See triggering guidance below.>
---
```

### Triggering — description guidance

The description is the only thing an orchestrator sees when deciding whether to trigger. Include:

1. **What the tool is** in one clause.
2. **What the skill does for integration** — install, auth, SDK calls, webhooks, etc.
3. **Specific trigger phrases** — the exact language a user would use. Pushy is better than vague. Examples:
   - `"integrate <tool> into a project"`
   - `"add <tool> to my backend"`
   - `"set up <tool> auth"`
   - `"how do I call <tool>'s API for <operation>"`
4. **Language(s) covered** explicitly. If the skill only has Python examples, don't claim Node coverage.

### Body sections, in order

Use these exact H2 headings:

#### `## What this integrates`

One short paragraph: what the tool does, what this skill helps the agent do with it, what's *not* covered (link to `references/api.md` for the full surface).

#### `## Install`

Every supported method, each as a verified copy-paste command block. If the target language has multiple package managers (pip/poetry/uv for Python, npm/pnpm/yarn for Node), include all common ones.

#### `## Authenticate`

The canonical credential flow:
- How to obtain a credential.
- Where to put it (env var name — be specific, not "e.g. some env var").
- A short code snippet showing the first authenticated call succeeding.
- If OAuth: the full flow with callback handling.

#### `## Minimal working example`

One self-contained runnable snippet for each supported language. Should produce a success response end-to-end. Include expected output so the using-agent can verify.

#### `## Common patterns`

Three to five realistic recipes. Each is 5–30 lines of code with a sentence of framing. Good candidates:
- Pagination / iterating large result sets
- Bulk operations
- Webhook signature verification
- Error retry with backoff
- Idempotency keys
- Streaming responses

Pick the patterns the user's declared scope will actually need. A skill full of patterns nobody uses is noise.

#### `## Troubleshooting`

Each entry: symptom → likely cause → fix. Short; push long prose into `references/troubleshooting.md` if it grows.

#### `## Reference`

Pointer to `references/api.md` for the full API surface. Tell the using-agent when to read it: "If the operation you need isn't in Common patterns, read `references/api.md`."

## references/api.md

This is the consolidated `docs.md` from Phase 1, preserved as the source of truth. Light reorganization is fine (e.g. reordering endpoints by resource); content cuts are not. The provenance comments stay.

## Anti-hallucination check

Before declaring the skill done, read it end-to-end and verify: for every endpoint, method, class, env var, and error code that appears in SKILL.md, there is a supporting section in `references/api.md`. If any claim doesn't trace back, delete it. Add a short top-of-SKILL.md note if the check was done:

```markdown
<!-- verified: every API reference in this skill is traceable to references/api.md as of <YYYY-MM-DD> -->
```

## Scope discipline

The skill should cover the user's declared integration scope plus a small halo, not the entire tool. If the user asked for "OAuth + invoice creation," the skill covers those deeply and links to `references/api.md` for everything else. This keeps SKILL.md under the 400-line target and keeps triggering precise.

## Example skeleton

```markdown
---
name: didox-integration
description: Integrate the Didox invoicing API into a Python backend — covers install, OAuth 2.0 authentication, creating and signing electronic invoices, and handling webhook notifications. Use whenever the user wants to "integrate Didox", "add Didox to my project", "send invoices via Didox", "set up Didox auth", or asks how to call any Didox API from Python.
---

# didox-integration

## What this integrates

Didox is an electronic document and invoicing service used in Uzbekistan. This skill covers the Python integration path: install, OAuth 2.0 login, the invoice lifecycle API, and webhook verification. For endpoints outside that scope, see `references/api.md`.

## Install
...

## Authenticate
...

## Minimal working example
...

## Common patterns
...

## Troubleshooting
...

## Reference

The full API surface (every endpoint, every field) is in `references/api.md`. Read it when the operation you need isn't covered above.
```
