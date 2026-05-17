"""Grounded explanation layer for /v1/identify.

Hard rules honored here (AGENTS.md #1, D-005, docs/data-model.md):

- The LLM does **not** own facts. The prompt feeds it ONLY the retrieved
  metadata fields plus a status-specific guardrail. There is no "you are
  a helpful art expert who knows about Renaissance painters..." preamble
  -- that's the canonical hallucination door.
- ``no_match`` never names an artwork or artist and we do **not** call
  the LLM at all (cheaper and safer to return canned re-shoot guidance).
- ``style_only`` may name an artist only via "resembles"-style framing.
- LLM failure is non-fatal: the match is still useful even if the
  description fails. Callers fall back to ``stub_explanation_text`` and
  keep the rest of the response intact.

The prompt template is ``_PROMPT_TEMPLATE`` below. Edit it here -- there
is no other prompt builder in the codebase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .config import Settings

logger = logging.getLogger("art_guide.llm")

# Fields from NormalizedArtwork that the LLM is allowed to ground in.
# Order is the order they appear in the prompt block.
_GROUNDABLE_FIELDS: tuple[str, ...] = (
    "title",
    "artist",
    "date",
    "medium",
    "culture",
    "period",
    "museum",
    # Tier (a) museum-plaque enrichment fields (D-024).
    "artist_bio",
    "credit_line",
    "dimensions",
    "dynasty",
)

_BASE_RULES: str = (
    "You are a museum curator writing a single wall-plaque explanation for one artwork. "
    "Write in a knowledgeable but warm docent voice — authoritative and unhurried, not marketing copy. "
    "Use ONLY the metadata fields provided below. "
    "Do NOT introduce any biographical, historical, geographic, or stylistic "
    "fact that is not explicitly present in the metadata. "
    "If a field is missing, do not speculate about its value. "
    "Do not name any artist, work, movement, or place that is not present in "
    "the metadata. "
    "Write 2-3 plain-prose sentences. No greeting, no markdown, no lists, no "
    "trailing commentary."
)

_STATUS_GUARDRAILS: dict[str, str] = {
    "exact": (
        "This is a confident, specific identification. State facts plainly and with authority. "
        "Weave the artist_bio note and credit_line naturally into the prose when present "
        "(e.g. 'Acquired by the museum in …' or 'painted during the artist's …'). "
        "Mention dimensions only if the work is notably large or small — skip for average-sized works. "
        "Lead with dynasty or period context for non-Western works if those fields are provided."
    ),
    "likely": (
        "This is a probable but not certain match. Open with hedging language such as "
        "'This appears to be' or 'This looks most like'. "
        "Weave in enrichment fields (artist_bio, credit_line) with the same hedging tone. "
        "If visual ambiguity is relevant, briefly note it (e.g. 'the brushwork resembles…'). "
        "Do not present the identification as definitive."
    ),
    "style_only": (
        "We could not identify a specific artwork; only stylistic signals are available. "
        "Do NOT name or claim the specific artwork in the metadata. "
        "Focus on style, period, culture, medium, or dynasty if those fields are provided. "
        "If you mention the artist field at all, frame it strictly as resemblance — "
        "e.g. 'resembles the work of X' or 'in the manner of X'. "
        "Never write 'by X' or 'this is X's work'. Do not claim this is the artwork in the metadata."
    ),
}

_NO_MATCH_TEXT: str = (
    "I can't place this one — it could be a private work, a reproduction, or simply outside "
    "what I know. Try moving closer so the artwork fills the frame, holding the camera steady, "
    "and using even, glare-free lighting. If it's not a painting or sculpture, that's fine — "
    "this app is built for museum-type works."
)

# Prompt template. The {metadata_block} substitution is built by
# ``_format_metadata_block``. Edits to the wording belong here, not in
# the route handler.
_PROMPT_TEMPLATE: str = """{base_rules}

Match status: {status}
{status_guardrail}

Metadata (verified):
{metadata_block}

Write the description now."""


@dataclass(frozen=True)
class GroundingResult:
    """Outcome of building + (optionally) running the LLM call."""

    text: str
    grounded_fields: list[str]
    llm_ms: int
    llm_called: bool
    llm_error: str | None = None


def stub_explanation_text(record: dict[str, Any], status: str) -> str:
    """Deterministic fallback explanation when the LLM is unavailable.

    Constructed from the same retrieved fields the LLM would see, so it
    still satisfies the "grounded in retrieved facts only" guarantee.
    Used in two cases:

    1. ``AZURE_OPENAI_*`` settings are not configured (local dev).
    2. The Azure OpenAI call raised (the match is still useful; we just
       drop in a safe placeholder so the iOS app can render something).
    """
    if status == "no_match":
        return _NO_MATCH_TEXT
    parts = []
    title = record.get("title")
    artist = record.get("artist")
    date = record.get("date")
    museum = record.get("museum")
    medium = record.get("medium")
    if status == "style_only":
        descriptor_bits = [b for b in (medium, record.get("period"), record.get("culture")) if b]
        descriptor = ", ".join(descriptor_bits) if descriptor_bits else "the same general style"
        if artist:
            parts.append(
                f"This image resembles the work of {artist} and shares "
                f"characteristics of {descriptor}."
            )
        else:
            parts.append(
                f"We can't pin this to a specific artwork, but it shares "
                f"characteristics of {descriptor}."
            )
        if museum:
            parts.append(f"A representative example is held by {museum}.")
        return " ".join(parts)
    if title:
        if artist and date:
            parts.append(f"{title} is attributed to {artist}, dated {date}.")
        elif artist:
            parts.append(f"{title} is attributed to {artist}.")
        elif date:
            parts.append(f"{title}, dated {date}.")
        else:
            parts.append(f"{title}.")
    if medium:
        parts.append(f"It is {medium}.")
    if museum:
        parts.append(f"It is held in the collection of {museum}.")
    if not parts:
        parts.append("Limited metadata is available for this work.")
    return " ".join(parts)


def grounded_fields_for(record: dict[str, Any]) -> list[str]:
    """Return the subset of ``_GROUNDABLE_FIELDS`` that are populated."""
    return [f for f in _GROUNDABLE_FIELDS if record.get(f)]


def _format_metadata_block(record: dict[str, Any], fields: Iterable[str]) -> str:
    lines = []
    for f in fields:
        val = record.get(f)
        if val is None or val == "":
            continue
        # Defensive: collapse newlines in case a museum string smuggles one in.
        sval = str(val).replace("\r", " ").replace("\n", " ").strip()
        lines.append(f"- {f}: {sval}")
    if not lines:
        lines.append("- (no fields available)")
    return "\n".join(lines)


def build_prompt(record: dict[str, Any], status: str) -> str:
    """Build the LLM prompt for a single match.

    The prompt only ever references fields present in ``record``. It
    never mentions the world outside the metadata. This is the central
    hallucination guardrail -- read AGENTS.md hard rule #1.
    """
    if status == "no_match":
        raise ValueError("build_prompt should not be called for status='no_match'")
    fields = grounded_fields_for(record)
    return _PROMPT_TEMPLATE.format(
        base_rules=_BASE_RULES,
        status=status,
        status_guardrail=_STATUS_GUARDRAILS.get(status, ""),
        metadata_block=_format_metadata_block(record, fields),
    )


def _azure_configured(settings: Settings) -> bool:
    return bool(
        settings.AZURE_OPENAI_ENDPOINT
        and settings.AZURE_OPENAI_API_KEY
        and settings.AZURE_OPENAI_DEPLOYMENT
        and settings.AZURE_OPENAI_API_VERSION
    )


async def explain(
    record: dict[str, Any] | None,
    status: str,
    settings: Settings,
    *,
    client_factory=None,
    timeout_s: float = 15.0,
) -> GroundingResult:
    """Build the explanation for a match.

    For ``no_match``, returns canned re-shoot guidance with no LLM call.
    For everything else, builds the prompt and calls Azure OpenAI. On any
    LLM failure (network, auth, timeout, content filter) we log and fall
    back to a deterministic stub so the rest of the response stays
    useful -- per AGENTS.md "match still returned" contract.

    ``client_factory`` is an optional injection seam for tests. It
    receives ``settings`` and returns an object with an async
    ``chat.completions.create`` method.
    """
    import time

    if status == "no_match" or record is None:
        return GroundingResult(
            text=_NO_MATCH_TEXT,
            grounded_fields=[],
            llm_ms=0,
            llm_called=False,
        )

    fields = grounded_fields_for(record)

    if not _azure_configured(settings) and client_factory is None:
        # Local dev: no Azure creds. Return a transparent stub so the
        # rest of the pipeline still demos.
        text = "[LLM call skipped -- AZURE_OPENAI not configured] " + stub_explanation_text(
            record, status
        )
        return GroundingResult(
            text=text,
            grounded_fields=fields,
            llm_ms=0,
            llm_called=False,
        )

    prompt = build_prompt(record, status)

    t0 = time.perf_counter()
    try:
        if client_factory is not None:
            client = client_factory(settings)
        else:
            client = _make_default_client(settings)

        # The openai SDK uses sync OR async clients; we accept either by
        # awaiting and unwrapping if needed.
        resp = await _chat_complete(client, settings, prompt, timeout_s=timeout_s)
        text = _extract_text(resp)
        if not text:
            raise RuntimeError("LLM returned empty content")
        llm_ms = int((time.perf_counter() - t0) * 1000)
        return GroundingResult(
            text=text.strip(),
            grounded_fields=fields,
            llm_ms=llm_ms,
            llm_called=True,
        )
    except Exception as exc:
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("LLM grounding failed; falling back to stub explanation")
        return GroundingResult(
            text=stub_explanation_text(record, status),
            grounded_fields=fields,
            llm_ms=llm_ms,
            llm_called=True,
            llm_error=str(exc) or exc.__class__.__name__,
        )


def _make_default_client(settings: Settings):
    """Construct an ``AsyncAzureOpenAI`` client from settings.

    Imported lazily so the api process doesn't hard-require the openai
    SDK at import time (and so tests that inject a ``client_factory`` can
    skip the dep).
    """
    from openai import AsyncAzureOpenAI  # type: ignore[import-not-found]

    return AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    )


async def _chat_complete(client, settings: Settings, prompt: str, *, timeout_s: float):
    """Run a chat completion. Tolerates sync or async clients."""
    import inspect

    create = client.chat.completions.create
    kwargs = dict(
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=1500,
        timeout=timeout_s,
    )
    result = create(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _extract_text(resp) -> str:
    """Extract assistant text from an openai.ChatCompletion-shaped object."""
    try:
        choice = resp.choices[0]
        msg = choice.message
        return (msg.content or "").strip()
    except Exception:
        return ""


__all__ = [
    "GroundingResult",
    "build_prompt",
    "explain",
    "grounded_fields_for",
    "stub_explanation_text",
]
