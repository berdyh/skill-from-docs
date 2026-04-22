# Vignette: non-English partial — Yandex Translate

A focused walkthrough of the *two* decisions that are hard in the non-English archetype: **choosing between parallel language versions** when the tool publishes both, and **preserving identifiers when prose gets translated**. Other phase mechanics are the same as in the FuseSoC case study and are not repeated here.

---

## The setup

> "Build me a skill for integrating Yandex Translate into a Python backend. Here's the URL: https://yandex.cloud/ru/docs/translate/operations/translate. Target Python."

The user gave a Russian subpage URL (deep — `/operations/translate/`, not the section root). Their working language appears to be English. The service publishes parallel EN and RU docs.

---

## Decision 1: which language to make canonical

Yandex Cloud exposes the same content at two paths:

- `https://yandex.cloud/ru/docs/translate/...` — Russian
- `https://yandex.cloud/en/docs/translate/...` — English

The naive approach is "pick the one the user linked." That's wrong. The user linked a specific *page*, which tells you nothing about which language version of the *corpus* is more complete. For non-English tools, language parity is rarely perfect:

- **Russian is usually more up-to-date** on Yandex's own services (Translate, Vision, SpeechKit) — Russian is authored first, English is translated after a lag.
- **English is usually more up-to-date** on services they resell or rebadge (S3-compatible Object Storage, Managed PostgreSQL) — the English upstream was translated to Russian once and diverged less.
- **Rate limits, regional availability, pricing, and legal pages** are sometimes EN-only or RU-only. Gaps rarely line up.

The right rule: pick the language that matches the user's working language as the *canonical* source, but fetch both versions of at least the critical pages (auth, quickstart, API reference) and diff them. When they differ materially, the language with the more recent update wins — and you annotate the divergence in `docs.md`.

For this case: canonical = English (user's working language); cross-reference = Russian (user's given URL + likely more current for a Yandex-native service).

### Implementation

1. Fetch `https://yandex.cloud/ru/docs/translate/operations/translate` (the given URL).
2. Rewrite to the English equivalent: `https://yandex.cloud/en/docs/translate/operations/translate`. Both were accessible at retrieval time.
3. Fetch the section root at both language codes (`/en/docs/translate/` and `/ru/docs/translate/`) to extract the full sidebar from each. Diff the page lists. If one language has pages the other doesn't, include those with a language-tagged provenance comment.

---

## Decision 2: preserving identifiers across translation

The API itself is language-neutral. Identifiers, endpoint paths, parameter names, JSON field names, enum values, and error codes are the same in both languages — they never get translated and must not be translated by you either.

A real example from the Translate quickstart:

```json
{
  "folderId": "<folder_ID>",
  "texts": ["Hello", "World"],
  "targetLanguageCode": "ru"
}
```

```bash
export API_KEY=<API_key>
curl --request POST \
  --header "Content-Type: application/json" \
  --header "Authorization: Api-Key ${API_KEY}" \
  --data '@<path_to_JSON_file>' \
  "https://translate.api.cloud.yandex.net/translate/v2/translate"
```

Both identical in the RU and EN versions. The surrounding prose differs:

> (RU) Создайте файл с телом запроса, например `body.json`
>
> (EN) Create a file with the request body, for example `body.json`

Rules:

- **Code blocks**: copy byte-for-byte from whichever source you're using. Never translate code, JSON keys, string literal values (`"ru"` is a language code here, not English text), header names, URL paths, or query parameter names.
- **Response values that include non-Latin text**: preserve exactly. If the API returns `{"text": "Привет"}`, the Cyrillic stays. Translating response examples is how integration skills get silently broken.
- **Prose in Russian sources**: translate to English. Mark first-use of each technical term with `[EN]` if the translation isn't obvious:
  > Запрос на перевод [EN: translation request] uses POST to the `translate` endpoint.
- **Cyrillic or other non-Latin text that's part of human-facing content** (examples in documentation like "translate 'Hello' to 'Привет'"): keep verbatim; these are demonstrations of behavior, not prose to localize.

---

## Secondary source: the protobuf repo

Yandex Cloud's APIs are defined by `.proto` files at `github.com/yandex-cloud/cloudapi`. For any Yandex Cloud service integration, this repo is the canonical source of truth — the REST and gRPC API surfaces are both generated from these protos. Rendered docs are a lossy view of this.

When building a skill that needs the full API surface (all methods, all field types, all enum values), fetch the relevant proto file directly. For Translate:

```
https://github.com/yandex-cloud/cloudapi/tree/master/yandex/cloud/ai/translate/v2
```

This replaces a lot of page-by-page reference crawling with a few `.proto` fetches. In archetype terms, Yandex Cloud is "non-English partial" *plus* effectively "OpenAPI-only" for API reference content. Layer both strategies.

---

## Consolidated `docs.md` header for this case

```markdown
# Yandex Translate

- version: v2 API
- docs site: https://yandex.cloud/en/docs/translate/
- canonical language: en
- cross-reference language: ru (parallel docs at https://yandex.cloud/ru/docs/translate/)
- api spec source: https://github.com/yandex-cloud/cloudapi/tree/master/yandex/cloud/ai/translate/v2
- retrieved: <YYYY-MM-DD>
- target SDK(s): python
- scope: text translation + language detection; not custom models or glossary management

## Coverage status
- [x] Installation (HTTP — no SDK install required; `requests` or `httpx`)
- [x] Authentication (IAM token and API key methods both covered)
- [x] Core concepts (folders, service accounts, IAM)
- [x] API surface (from proto + rendered docs)
- [x] Minimal example
- [~] Error handling (error codes in proto; human-readable messages partial)
- [x] Rate limits, quotas
- [x] Gotchas (language code formats, folder vs cloud vs organization scoping)
```

---

## Transferable lessons

Three things this case teaches that the abstract rules can't:

1. **Parallel language versions aren't interchangeable.** Fetch both the canonical and the cross-reference for critical sections; diff; note divergences. The rule "pick the user's working language" is wrong if it's not also the authoring language.

2. **Auto-translation breaks code.** The instinct to run the whole source through a translator produces a corpus where `folderId` becomes `идентификаторПапки` or JSON keys get localized. Code blocks, identifiers, and response-example literals are never translated, regardless of surrounding prose.

3. **Regional cloud APIs usually have a machine-readable source of truth elsewhere.** Yandex has proto files; Alibaba has OpenAPI; Tencent has both. Find it before committing to HTML crawling. The rendered docs are for humans; the proto/spec repo is for programmatic consumers, which is the use case here.
