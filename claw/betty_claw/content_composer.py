"""
Claude-driven content composer for the Phase 3.0 deterministic pipeline.

Given a parser's raw field value (typically marketing-voice prose from
an Airbnb listing body) plus the source body and frontmatter, asks
Claude to rewrite the field following the site's voice calibration
rules. Returns the rewritten text and the call's cost so the batch
script can track total spend.

This replaces the Qwen-as-agent loop with a single Claude call per
field, driven from a Python script. No tool use, no agent reasoning,
no state-carrying between calls. Deterministic input/output.

Cost: ~$0.005-0.020 per field call on Haiku 4.5 at typical voice-doc
sizes (~3K input tokens + ~500 output tokens). Operator can override
the model via ANTHROPIC_CONTENT_MODEL env var if Sonnet quality is
needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from betty_claw.anthropic_client import (
    AnthropicAPIError,
    AnthropicClient,
    AnthropicClientError,
    AnthropicResponseError,
)


# Default model for content composition. Haiku is the cost-sweet-spot
# for editorial rewrites; operator overrides to Sonnet if voice quality
# proves insufficient.
DEFAULT_CONTENT_MODEL = "claude-haiku-4-5-20251001"


# Output rules are enforced via system prompt. The composer is strict
# about returning plain text only — no preamble, no markdown wrapper,
# no meta-commentary. This makes the script's downstream parsing
# trivial: the entire response IS the rewritten field.
_SYSTEM_PROMPT_TEMPLATE = """\
You are an editorial content writer for {domain}, following the voice
calibration rules in the document below.

{voice_doc_text}

OUTPUT RULES (non-negotiable):

- Return ONLY the rewritten text. No preamble. No "Here is the
  rewrite:" or similar lead-in. No markdown code fence. No closing
  remarks. Your entire response is the rewritten field value as
  plain text.
- If a fact in the source is not appropriate to include, omit it
  silently. Do not explain that you omitted it.
- Do not include any meta-commentary about your process.
- If you cannot rewrite the field without violating the voice rules
  (e.g., source is empty or unusable), return the literal string
  "UNABLE_TO_COMPOSE" and nothing else.
"""


# User prompt has the field-specific context plus optional retry
# feedback from a previous validation failure.
_USER_PROMPT_TEMPLATE = """\
Rewrite the {field_name} field for an Airbnb dossier into editorial
voice for {domain}.

PARSER'S RAW VALUE (this is what we have today; usually marketing voice,
sometimes empty, sometimes a guest review quote — your job is to
replace it):

{parsed_value}

SOURCE BODY (your only allowed source for descriptive facts):

{body_excerpt}

SOURCE FRONTMATTER VALUES (numbers and identifiers here are valid in
your rewrite; treat them as authoritative):

{frontmatter_summary}

{retry_feedback_section}

Return only the rewritten {field_name} text. No preamble, no wrapper.
"""


_RETRY_FEEDBACK_TEMPLATE = """\
PREVIOUS ATTEMPT FAILED VALIDATION. Your prior rewrite was:

{previous_attempt}

The validator flagged these violations:

{violations_summary}

Produce a new rewrite that addresses every flagged violation. Do not
preserve the flagged passages.
"""


@dataclass(frozen=True)
class ComposeResult:
    """One Claude content-composition call's result."""
    text: str
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


def _format_frontmatter_summary(frontmatter: dict[str, Any]) -> str:
    """Render frontmatter as a compact bulleted list for the prompt."""
    if not frontmatter:
        return "(no frontmatter)"
    lines = []
    for k, v in frontmatter.items():
        v_str = str(v)
        if len(v_str) > 200:
            v_str = v_str[:197] + "..."
        lines.append(f"  {k}: {v_str}")
    return "\n".join(lines)


def _format_retry_feedback(
    previous_attempt: str | None,
    violations: list[dict[str, Any]] | None,
) -> str:
    """Render the retry-feedback block, or empty string if first attempt."""
    if not previous_attempt or not violations:
        return ""
    lines = []
    for v in violations:
        rule = v.get("rule", "(unknown)")
        match = v.get("match", "(unknown)")
        explanation = v.get("explanation", "")
        lines.append(f"  - rule={rule}, offending text: {match!r} — {explanation}")
    violations_summary = "\n".join(lines)
    return _RETRY_FEEDBACK_TEMPLATE.format(
        previous_attempt=previous_attempt,
        violations_summary=violations_summary,
    )


def compose_field(
    *,
    field_name: str,
    parsed_value: str,
    body_excerpt: str,
    frontmatter: dict[str, Any],
    voice_doc_text: str,
    domain: str,
    previous_attempt: str | None = None,
    violations: list[dict[str, Any]] | None = None,
    model: str | None = None,
    timeout_s: float = 60.0,
) -> ComposeResult:
    """Ask Claude to rewrite one field according to the voice rules.

    Args:
        field_name: "description" or "persona" — used in the prompt so
            Claude knows the target shape.
        parsed_value: The parser's raw extraction. Usually contains the
            marketing language we want to rewrite away. May be empty.
        body_excerpt: The dossier's cruft-stripped body. The only
            allowed source for descriptive facts.
        frontmatter: The dossier's structured frontmatter. Values here
            (license, rating, room counts) are authoritative for use
            in the rewrite.
        voice_doc_text: Full text of the site's voice calibration doc,
            embedded in the system prompt.
        domain: The site's public domain (e.g., "travelpec.com"). Used
            for prompt personalization.
        previous_attempt: If this is a retry after validation failure,
            the prior rewrite text. Triggers the retry-feedback block.
        violations: If this is a retry, the violation dicts from
            voice_validation.validate_text(). Each is {rule, match,
            position, explanation}.
        model: Override the content model. Defaults to env var
            ANTHROPIC_CONTENT_MODEL, then DEFAULT_CONTENT_MODEL.
        timeout_s: HTTP timeout for the Anthropic call.

    Returns:
        ComposeResult with the rewritten text, model used, cost, and
        token counts.

    Raises:
        AnthropicAPIError on transport/HTTP failure.
        AnthropicResponseError on malformed Anthropic body.
        ValueError if Claude returns the "UNABLE_TO_COMPOSE" sentinel.
    """
    resolved_model = (
        model
        or os.environ.get("ANTHROPIC_CONTENT_MODEL")
        or DEFAULT_CONTENT_MODEL
    )
    client = AnthropicClient(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        model=resolved_model,
        timeout_s=timeout_s,
    )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        domain=domain,
        voice_doc_text=voice_doc_text,
    )
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        field_name=field_name,
        domain=domain,
        parsed_value=parsed_value or "(empty)",
        body_excerpt=body_excerpt or "(empty)",
        frontmatter_summary=_format_frontmatter_summary(frontmatter),
        retry_feedback_section=_format_retry_feedback(
            previous_attempt, violations
        ),
    )

    response = client.send(
        prompt=user_prompt,
        system=system_prompt,
        max_tokens=2048,
    )

    text = response.content.strip()
    if text == "UNABLE_TO_COMPOSE":
        raise ValueError(
            f"compose_field({field_name!r}): Claude returned "
            f"UNABLE_TO_COMPOSE sentinel — source likely empty or "
            f"unusable. parsed_value={parsed_value!r}, "
            f"body_excerpt len={len(body_excerpt)}."
        )

    return ComposeResult(
        text=text,
        model=response.model,
        cost_usd=response.cost_usd,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
