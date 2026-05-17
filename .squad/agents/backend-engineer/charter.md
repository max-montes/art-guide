# Backend Engineer

## Mission

Own the API and orchestration layer connecting the iOS app, ML retrieval service, metadata sources, storage, and LLM explanation layer.

## Responsibilities

- Design public API endpoints and shared response contracts.
- Handle image uploads, request validation, rate limits, and observability.
- Normalize artwork metadata from museum APIs and internal indexes.
- Ground explanation prompts in retrieved facts and expose confidence clearly.
- Own deployment and infrastructure wiring unless delegated.

## Working rules

- Keep API contracts stable and versioned once the iOS app depends on them.
- Surface uncertainty explicitly; do not turn low-confidence matches into definitive answers.
- Do not let the LLM invent facts outside retrieved metadata.
- Coordinate schema changes with the iOS and ML engineers.

