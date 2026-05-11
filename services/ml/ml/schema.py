"""Normalized cross-museum artwork schema.

Every ingestion adapter (Met, Wikimedia, Rijksmuseum, ...) MUST emit
records that conform to `NormalizedArtwork`. Downstream code (embedding
job, retrieval API, evaluator) only sees this shape, never the raw
museum-specific payload.

Principles
----------
* We store metadata + image URLs, NOT the original images.
* `id` is a globally unique, source-prefixed string (e.g. ``"met:436532"``).
* All free-text fields are optional -- museum data is messy.
* `tags` is the catch-all for source-specific facets (department,
  classification, object type) so the retrieval layer can use them for
  filtering or reranking without us having to expand the schema each time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class NormalizedArtwork(BaseModel):
    """Cross-source artwork record. One row per artwork."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(
        ...,
        description="Globally unique id, source-prefixed. Example: 'met:436532'.",
    )
    source: str = Field(
        ...,
        description="Short slug for the source dataset, e.g. 'met', 'wikimedia', 'rijks'.",
    )

    title: str | None = None
    artist: str | None = None
    date: str | None = Field(
        default=None,
        description="Free-text date string as published by the museum (e.g. 'ca. 1503').",
    )
    medium: str | None = None
    culture: str | None = None
    period: str | None = None

    museum: str = Field(
        ...,
        description="Human-readable museum name, e.g. 'The Metropolitan Museum of Art'.",
    )
    source_url: str = Field(
        ...,
        description="Canonical museum page URL for citation/attribution.",
    )
    image_url: str | None = Field(
        default=None,
        description="URL to a public-domain image. We never download it during ingestion.",
    )

    # --- D-024 tier (a) enrichment fields ---
    # Grounded: fed to the LLM via the grounded_fields allow-list.
    artist_bio: str | None = Field(
        default=None,
        description=(
            "Artist biography as published by the source museum "
            "(e.g. 'French, Paris 1748–1825 Brussels'). "
            "Met source field: artistDisplayBio."
        ),
    )
    credit_line: str | None = Field(
        default=None,
        description=(
            "Provenance / acquisition credit line "
            "(e.g. 'Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931'). "
            "Met source field: creditLine."
        ),
    )
    dimensions: str | None = Field(
        default=None,
        description=(
            "Physical dimensions as a free-text string "
            "(e.g. '51 x 77 1/4 in. (129.5 x 196.2 cm)'). "
            "Met source field: dimensions."
        ),
    )
    dynasty: str | None = Field(
        default=None,
        description=(
            "Dynasty or ruling period — sparse, essential for Egyptian / "
            "ancient Asian art (e.g. 'Dynasty 18'). "
            "Met source field: dynasty."
        ),
    )
    # Retrieval / filtering only — NOT grounded in LLM prompt.
    object_wikidata_url: str | None = Field(
        default=None,
        description=(
            "Wikidata entity URL for the artwork — tier (b) enrichment anchor. "
            "Not surfaced in the API response or LLM prompt. "
            "Met source field: objectWikidata_URL."
        ),
    )
    date_begin: int | None = Field(
        default=None,
        description=(
            "Earliest integer year for the object date range. "
            "Used for temporal filtering, not storytelling. "
            "Met source field: objectBeginDate."
        ),
    )
    date_end: int | None = Field(
        default=None,
        description=(
            "Latest integer year for the object date range. "
            "Met source field: objectEndDate."
        ),
    )

    tags: list[str] = Field(
        default_factory=list,
        description="Source-specific facets: department, classification, object type, etc.",
    )
    is_public_domain: bool = Field(
        ...,
        description="Whether the image rights allow public-domain reuse.",
    )

    @field_validator("id")
    @classmethod
    def _id_must_be_prefixed(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("id must be of the form '<source>:<local_id>'")
        return v

    @field_validator("source_url", "image_url")
    @classmethod
    def _validate_optional_url(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        # Use pydantic's HttpUrl for validation but persist as plain str for
        # downstream JSONL friendliness.
        HttpUrl(v)
        return v

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line (no trailing newline)."""
        return self.model_dump_json(exclude_none=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedArtwork":
        return cls.model_validate(data)
