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

## Museum-docent voice pattern (Wave 2, 2026-05-16)

When the output should read like a **museum wall plaque** (not a fact-sheet), use this voice:

**Base rule wording:**
> "You are a museum curator writing a single wall-plaque explanation for one artwork. Write in a knowledgeable but warm docent voice — authoritative and unhurried, not marketing copy. Use ONLY the metadata fields provided below. [standard grounding rules...] Write 2-3 plain-prose sentences. No greeting, no markdown, no lists, no trailing commentary."

**Status guardrail upgrades:**
- `exact`: "Weave the artist_bio note and credit_line naturally into the prose when present (e.g. 'Acquired by the museum in …' or 'painted during the artist's …'). Mention dimensions only if the work is notably large or small — skip for average-sized works. Lead with dynasty or period context for non-Western works if those fields are provided."
- `likely`: "Open with hedging language such as 'This appears to be' or 'This looks most like'. Weave in enrichment fields with the same hedging tone."
- `style_only`: "Focus on style, period, culture, medium, or dynasty — not the specific work. If you mention the artist field at all, frame it strictly as resemblance."

**Result** (Van Gogh L'Arlésienne, exact match):
> "Vincent van Gogh (Dutch, Zundert 1853–1890 Auvers-sur-Oise) painted L'Arlésienne in 1888–89 in oil on canvas. The painting is in The Metropolitan Museum of Art and entered the collection as the bequest of Sam A. Lewisohn in 1951."

The LLM naturally wove `artist_bio` and `credit_line` without any explicit field-to-sentence mapping.

## Reasoning model gotchas (gpt-5-mini / o-series)

If deploying on an Azure OpenAI reasoning model (`gpt-5-mini`, `o1`, `o3-mini`, etc.):

1. **No `temperature` param.** The API returns 400 if you pass it. Remove entirely.
2. **Use `max_completion_tokens` not `max_tokens`.** The old param returns 400.
3. **Reasoning tokens consume budget first.** With `max_completion_tokens=250`, the model used all 250 on internal reasoning and returned empty content (`finish_reason=length`, `reasoning_tokens=250`, `content=""`). Use ≥1500 for a 2-3 sentence output (reasoning eats ~700–900 tokens).
4. **Detect via model name or by catching the 400 on the `temperature` param.**

