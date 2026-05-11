# SKILL: grounded-llm-prompt

**When to use:** any time you wire an LLM into a request path where the LLM **must not** invent facts — RAG endpoints, document Q&A, "explain this database row", customer-data summarization, anything compliance-sensitive. The pattern below is what `/v1/identify` uses (`services/api/app/llm.py`); reuse the shape, not necessarily the file.

**Rule of thumb:** if a wrong answer from the LLM would be a defect, this skill applies.

## The pattern in five rules

1. **The prompt contains ONLY retrieved fields.** No "you are a helpful expert" preamble, no role-play, no world-knowledge hook. The system instruction is "use ONLY the metadata below; do not introduce facts not in the metadata; if a field is missing, do not speculate." That sentence is the entire personality.
2. **Build the metadata block from a fixed allowlist of field names**, and only include fields that are populated. Never serialize the full record dict (it usually carries internal junk like raw_metadata, ids, audit columns). Test that the prompt does not contain extra keys when given a record with junk fields.
3. **Status-specific guardrails go in a small lookup table**, not at runtime composition. Each status maps to ONE additional sentence appended to the base rules. e.g. for low-confidence matches, that sentence forces "resembles"/"in the manner of" framing and forbids authorship claims.
4. **Skip the LLM when you have nothing to ground in.** If the retrieval result is empty (or status = "no_match"), return canned text. Do NOT call the LLM with empty metadata "to be safe" — it will hallucinate something to fill the silence.
5. **LLM failure is non-fatal.** Wrap the chat completion in `try/except`. On failure, return a deterministic stub built from the SAME retrieved fields the LLM would have seen. The downstream consumer should always receive a usable answer; the LLM is a value-add, not the source of truth. Log the exception, count failures, but do not 500 the request.

## Prompt template (copy as a starting point)

```
You are writing a single {role/output type} explanation for one {entity}.
Use ONLY the metadata fields provided below.
Do NOT introduce any biographical, historical, geographic, or stylistic
fact that is not explicitly present in the metadata.
If a field is missing, do not speculate about its value.
Do not name any {actor / place / concept} that is not present in the metadata.
Write {length constraint}. No greeting, no markdown, no lists, no trailing commentary.

Match status: {status}
{status_guardrail}

Metadata (verified):
- field_a: {value}
- field_b: {value}
... (only populated fields)

Write the description now.
```

## Tests that pin the guardrail

- **"prompt only references populated fields"** — pass a sparse record, assert no `- field_x:` line for fields that are None.
- **"prompt does not contain a world-knowledge preamble"** — assert forbidden phrases (`"expert"`, `"you know"`, `"trained"`, `"<domain> history"`) do NOT appear, lowercase-search.
- **"prompt does not leak extra keys from the record"** — pass a record with `secret_field`, `internal_id`, `raw_metadata`; assert those substrings do NOT appear in the prompt.
- **"low-confidence guardrail forces hedging vocabulary"** — for the low-confidence status, assert the guardrail substring (e.g., `"resembles"`, `"in the manner of"`, `"do not claim"`) is in the prompt.
- **"failure mode is non-fatal"** — inject a `client_factory` that raises; assert the result still contains a stub text grounded in the record's fields, with `llm_called=True`, `llm_error="..."`, and the caller never sees the exception.

## File layout convention

- `app/llm.py` (or equivalent): owns the prompt template constants, `build_prompt(record, status) -> str`, `stub_explanation_text(record, status) -> str`, `explain(record, status, settings, *, client_factory=None) -> GroundingResult`.
- `GroundingResult` carries `text`, `grounded_fields`, `llm_ms`, `llm_called`, `llm_error`. The route reads all of these into its response + log line.
- The Azure/OpenAI/Anthropic SDK is imported lazily inside `_make_default_client` so tests don't need the dependency installed.
- Settings exposes the model deployment name + endpoint + key + api version, and a "not configured" branch (returns a marker-prefixed stub) makes local-dev painless.

## What this avoids

- The classic "the LLM said the painting is from 1503 but the metadata says ca. 1505" hallucination — by construction, anything the LLM mentions about a date came from `metadata.date` because it had nothing else.
- Style-blowback hallucinations (the LLM riffs on "Renaissance Italy" because it knows Renaissance painting from training) — the world-knowledge preamble forbidden test catches accidental reintroduction of this surface.
- Cascading failures from the LLM provider — explanation degrades, identification stays useful.

## Cross-references

- D-005, `docs/data-model.md` — the confidence model that `status` parameter comes from.
- AGENTS.md hard rule #1 — "the LLM does not own facts."
- `.squad/decisions/inbox/backend-engineer-identify-pipeline.md` — concrete instantiation in the art-guide `/v1/identify` route.
