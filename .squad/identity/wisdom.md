---
last_updated: 2026-05-10T17:53:00.000Z
---

# Team Wisdom

Reusable patterns and heuristics. NOT transcripts — each entry is a distilled, actionable insight.

## Patterns

- **Pattern:** Versatility comes from the catalog, not the model. **Context:** Whenever someone wants to "make recognition smarter," prefer ingesting more sources or improving retrieval/grounding before reaching for fine-tuning or new models.
- **Pattern:** The LLM is the storyteller, not the source of truth. **Context:** Any new explanation work must use only fields present in the retrieved record. Hallucination is a defect.
- **Pattern:** Confidence-aware UX is non-negotiable. **Context:** Every new feature must respect the status enum and never overclaim. Low-confidence answers must hedge; `no_match` must not invent facts; `style_only` may name an artist only with "resembles"-style framing.
- **Pattern:** Don't store original images. **Context:** Catalog grows by metadata + URL + embedding only. Raw user uploads never persist; debug thumbnails are off by default.
- **Pattern:** One image pipeline shared by ingest and query. **Context:** If you process images anywhere new, call `services/ml/ml/imageops.py::prepare_for_embedding`. Drift between paths silently kills retrieval.
- **Pattern:** Prefer evaluation depth over model novelty. **Context:** Resume signal comes from rigorous Azure AI Foundry eval (retrieval top-K, MRR, groundedness, factuality, confidence honesty), not from training a custom LLM.
- **Pattern:** Local + prod only; preview later. **Context:** Don't add a staging environment until TestFlight betas need isolation.
- **Pattern:** Lock criteria, defer name choices. **Context:** For tech that moves fast (embedding models, LLM versions), lock evaluation criteria in docs and pick the actual name at implementation time.
- **Pattern:** Top 1 unless ambiguous. **Context:** Only return top 3 candidates for `likely` with `gap < 0.05`; otherwise return one. Keeps UX clean and signal high.
