"""
Editorial quality scorer (Phase 2, 2026-06-14).

Semantic complement to voice_validation.py. The deterministic validator
catches what regex can see — banned words, banned openers, hallucinated
numbers, owner attribution. This module catches what regex can't —
atmospheric editorial invention, distance/capacity inference, marketing
voice, generic filler.

Uses Claude (Haiku by default) to evaluate a rewrite against the
voice rules via a structured JSON-out prompt. The model returns a score
plus a list of categorized violations; this module parses, validates,
and returns it as a typed result.

Cost target: ~$0.005 per call at Haiku rates (2K input + 500 output).
At Phase 4.5's $5/day operational cap, this supports ~1,000 calls/day
which is well above the realistic content-drafting volume.

Designed to be called after voice_validation.validate_text passes (which
filters out the mechanical violations the LLM doesn't need to think
about). The two layers are complementary, not redundant.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from betty_claw.anthropic_client import (
    AnthropicAPIError,
    AnthropicClient,
    AnthropicClientError,
    AnthropicResponseError,
    AnthropicResponse,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default editorial model when ANTHROPIC_EDITORIAL_MODEL env var is unset.
# Haiku 4.5 is the cost-optimal default — semantic editorial review doesn't
# need Opus-tier reasoning at scale. Operator can override per-process.
DEFAULT_EDITORIAL_MODEL = "claude-haiku-4-5-20251001"

# Soft warning if a single call costs more than this (in USD). Doesn't
# block — just logs. Real spend cap lives in spend_ledger if/when wired.
_PER_CALL_WARN_THRESHOLD_USD = 0.05


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EditorialViolation:
    """One semantic violation detected by the scorer.

    - category    : violation type slug from the rubric
                    (e.g., "atmospheric_invention", "distance_inference")
    - passage     : the offending phrase from the rewrite
    - explanation : why it violates the voice rules
    """
    category: str
    passage: str
    explanation: str


@dataclass(frozen=True)
class EditorialScore:
    """Structured result from one scoring call.

    - score         : 0-10. 10 = clean editorial output, 0 = unsalvageable.
                      Treat >= 8 as ready-to-publish, 5-7 as needs-revision,
                      < 5 as restart-from-source.
    - violations    : per-issue findings, possibly empty.
    - summary       : one-paragraph human-readable evaluation.
    - cost_usd      : what this call cost the operator.
    - model         : which model produced the score.
    - input_tokens  : token accounting for visibility.
    - output_tokens : token accounting for visibility.
    """
    score: int
    violations: tuple[EditorialViolation, ...]
    summary: str
    cost_usd: float
    model: str
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# Rubric prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an editorial quality reviewer for travelpec.com, a curated
directory of stays in Prince Edward County, Ontario. You enforce a
voice calibration that values:

- Source-grounding: every concrete fact in the output must trace back
  to the source text.
- Editorial restraint: short, factual, declarative prose. No marketing
  voice. No atmospheric invention. No flourishes that fabricate vibe
  or sensory experience without source grounding.
- Editorial-we: first-person plural ("we", "our") used only for
  editorial recommendations ("we recommend three nights"). Factual
  descriptions are third-person declarative without first-person.
- Multi-night framing for recommendations.

You score rewrites against this voice on a 0-10 scale and identify
specific violations by category. You return your evaluation as JSON
only, with no preamble, no markdown wrapper, and no commentary
outside the JSON object.
"""

_USER_PROMPT_TEMPLATE = """\
SOURCE TEXT (the raw Airbnb dossier the rewrite is derived from):

{source_text}

REWRITTEN DESCRIPTION (what you are evaluating):

{rewrite}

Evaluate the rewrite. Use these violation categories:

- atmospheric_invention: phrases that fabricate vibe or sensory
  experience without source support (e.g., "morning coffee has no
  rivals", "step back in time", "memories that will last").
- distance_inference: specific distances, walking minutes, or
  proximity claims not directly stated in the source (e.g., source
  says "close proximity" → rewrite says "within a block").
- capacity_inference: guest counts or occupancy claims that the
  source does not directly support.
- marketing_voice: pitch-style or sales-style sentences that don't
  match the editorial restraint described above, even if no specific
  banned word is used.
- generic_filler: sentences that add no concrete information and
  could be deleted without losing meaning (e.g., "this property
  offers everything you need").
- inverted_emphasis: leading with vibe/promotion when the source
  has concrete facts that should lead.

Each violation MUST quote the exact offending passage from the
rewrite (string match, not paraphrase). If the rewrite is clean,
return an empty violations list with a high score.

Return JSON only with this exact shape:

{{
  "score": <integer 0-10>,
  "violations": [
    {{
      "category": "<one of the categories above>",
      "passage": "<exact substring from rewrite>",
      "explanation": "<one sentence on why it violates>"
    }}
  ],
  "summary": "<one-sentence overall verdict>"
}}

Do not include any text outside the JSON object. Do not wrap the
JSON in ``` code fences. Output the JSON object and stop.
"""


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

# Models sometimes add stray text before or after the JSON despite
# instructions. Pull the first balanced `{...}` block out.
_JSON_BLOCK_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_block(text: str) -> str:
    """Return the first balanced top-level JSON object substring in text.

    Defensive: even with strict prompting, models occasionally emit a
    leading "Here is the evaluation:" or wrap the JSON in code fences.
    This finds the {...} block by braces-balance scan rather than naive
    regex (which fails on nested objects).
    """
    start = text.find("{")
    if start == -1:
        raise ValueError(
            f"No JSON object found in scorer response. "
            f"First 200 chars: {text[:200]!r}"
        )
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError(
        f"Unbalanced JSON in scorer response. "
        f"First 200 chars: {text[:200]!r}"
    )


def _parse_scorer_response(content: str) -> tuple[int, tuple[EditorialViolation, ...], str]:
    """Parse the JSON the scorer returned. Raises ValueError on malformed."""
    block = _extract_json_block(content)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Scorer returned non-JSON content: {e!r} | "
            f"first 500 chars: {block[:500]!r}"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(f"Scorer JSON top level must be object, got {type(data).__name__}")
    if "score" not in data or "violations" not in data or "summary" not in data:
        raise ValueError(
            f"Scorer JSON missing required keys. Got: {sorted(data.keys())}"
        )
    score_raw = data["score"]
    if not isinstance(score_raw, int) or not 0 <= score_raw <= 10:
        raise ValueError(f"Scorer score must be int 0-10, got {score_raw!r}")
    violations_raw = data["violations"]
    if not isinstance(violations_raw, list):
        raise ValueError(f"Scorer violations must be list, got {type(violations_raw).__name__}")
    violations: list[EditorialViolation] = []
    for v in violations_raw:
        if not isinstance(v, dict):
            continue
        category = str(v.get("category", "unknown"))
        passage = str(v.get("passage", ""))
        explanation = str(v.get("explanation", ""))
        violations.append(EditorialViolation(
            category=category,
            passage=passage,
            explanation=explanation,
        ))
    return score_raw, tuple(violations), str(data["summary"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_editorial_quality(
    text: str,
    source_text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> EditorialScore:
    """Call Claude to evaluate a rewrite against editorial voice rules.

    Args:
        text: The rewritten description (or any content field) to evaluate.
        source_text: The raw source the rewrite was derived from.
        model: Override the editorial model. Default falls back to env var
            ANTHROPIC_EDITORIAL_MODEL, then DEFAULT_EDITORIAL_MODEL.
        api_key: Override the API key. Default reads from env.
        timeout_s: HTTP timeout for the Anthropic call.

    Returns:
        EditorialScore with score, violations, summary, cost.

    Raises:
        AnthropicAPIError on transport/HTTP failure.
        AnthropicResponseError on malformed Anthropic body.
        ValueError on malformed scorer JSON.
    """
    resolved_model = (
        model
        or os.environ.get("ANTHROPIC_EDITORIAL_MODEL")
        or DEFAULT_EDITORIAL_MODEL
    )
    client = AnthropicClient(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        model=resolved_model,
        timeout_s=timeout_s,
    )
    prompt = _USER_PROMPT_TEMPLATE.format(
        source_text=source_text or "(no source text provided)",
        rewrite=text,
    )
    resp: AnthropicResponse = client.send(
        prompt=prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=1024,
    )
    score, violations, summary = _parse_scorer_response(resp.content)
    return EditorialScore(
        score=score,
        violations=violations,
        summary=summary,
        cost_usd=resp.cost_usd,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
    )


def editorial_score_to_dict(score: EditorialScore) -> dict[str, Any]:
    """JSON-safe dict for MCP transport."""
    return {
        "score": score.score,
        "violations": [
            {
                "category": v.category,
                "passage": v.passage,
                "explanation": v.explanation,
            }
            for v in score.violations
        ],
        "summary": score.summary,
        "cost_usd": round(score.cost_usd, 6),
        "model": score.model,
        "input_tokens": score.input_tokens,
        "output_tokens": score.output_tokens,
    }


# ---------------------------------------------------------------------------
# JSON parser self-test (offline — no API call)
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Tests the JSON parsing logic against fixtures.

    Does NOT call Anthropic — that requires ANTHROPIC_API_KEY and would
    cost money. Live integration test runs separately via the MCP layer.
    """
    print("editorial_scorer.py self-test\n")

    # ---- happy path ----
    valid = """{
        "score": 7,
        "violations": [
            {
                "category": "atmospheric_invention",
                "passage": "has no rivals",
                "explanation": "Fabricates atmosphere not in source."
            }
        ],
        "summary": "One atmospheric flourish; otherwise compliant."
    }"""
    score, viols, summary = _parse_scorer_response(valid)
    assert score == 7
    assert len(viols) == 1
    assert viols[0].category == "atmospheric_invention"
    assert "rivals" in viols[0].passage
    print("  [ok] clean JSON parses to typed result")

    # ---- model adds leading text (defensive parsing) ----
    noisy = """Here is my evaluation:

    {"score": 9, "violations": [], "summary": "Clean."}
    """
    score, viols, summary = _parse_scorer_response(noisy)
    assert score == 9
    assert viols == ()
    print("  [ok] leading-prose ignored; JSON extracted")

    # ---- code-fence wrapper ----
    fenced = """```json
    {"score": 5, "violations": [], "summary": "Mid-tier."}
    ```"""
    score, _, _ = _parse_scorer_response(fenced)
    assert score == 5
    print("  [ok] code-fence wrapper ignored")

    # ---- malformed json raises ----
    try:
        _parse_scorer_response("{score: 5, not valid json}")
    except ValueError as e:
        assert "non-JSON" in str(e) or "JSON" in str(e)
        print("  [ok] malformed JSON raises ValueError")
    else:
        raise AssertionError("malformed JSON should error")

    # ---- missing required keys raises ----
    try:
        _parse_scorer_response('{"score": 5}')
    except ValueError as e:
        assert "required keys" in str(e)
        print("  [ok] missing required keys raises ValueError")
    else:
        raise AssertionError("incomplete JSON should error")

    # ---- score out of range raises ----
    try:
        _parse_scorer_response(
            '{"score": 15, "violations": [], "summary": "x"}'
        )
    except ValueError as e:
        assert "0-10" in str(e)
        print("  [ok] out-of-range score raises ValueError")
    else:
        raise AssertionError("score 15 should error")

    # ---- nested JSON in violation explanation ----
    nested = """{
        "score": 8,
        "violations": [
            {
                "category": "marketing_voice",
                "passage": "the perfect getaway",
                "explanation": "Uses the phrase {the perfect} which is in the banned list."
            }
        ],
        "summary": "One issue."
    }"""
    score, viols, _ = _parse_scorer_response(nested)
    assert score == 8
    assert len(viols) == 1
    assert "perfect" in viols[0].passage
    print("  [ok] nested braces in string fields handled by parser")

    print("\neditorial_scorer.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
