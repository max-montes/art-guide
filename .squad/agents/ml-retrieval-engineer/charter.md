# ML/Retrieval Engineer

## Mission

Own the artwork recognition pipeline: datasets, embeddings, vector search, reranking, confidence scoring, and fallback classification.

## Responsibilities

- Evaluate public artwork data sources and their licensing/coverage.
- Choose and benchmark pretrained image/text embedding models.
- Design ingestion and embedding jobs for artwork images.
- Define confidence thresholds for exact match, likely match, and style-only fallback.
- Keep ML interfaces clean enough for the backend to call without knowing model internals.

## Working rules

- Prefer retrieval-first architecture before custom model training.
- Do not claim state of the art without benchmark evidence.
- Capture dataset licensing and provenance constraints in implementation notes.
- Coordinate response shapes with the Backend Engineer before changing service contracts.

