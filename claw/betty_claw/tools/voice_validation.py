"""
Voice calibration validator (Phase 1.7, 2026-06-14).

Deterministic checks Betty runs against her rewritten output BEFORE
calling emdash_create_content_draft. The model in use (Qwen3.5-35B-A3B
under Hermes) cannot reliably enforce a long banned-word list or detect
its own hallucinated numbers — this module does so mechanically.

The rules come from a SiteVoiceValidation block in the site's YAML
(loaded by site_config.py). The voice doc itself is authoritative for
human-judgment rules (tone, examples, the "what to do when source
lacks a fact" guidance); this validator handles only the checks that
can be answered with regex or substring match.

Five checks, all returning structured Violation records:

  1. banned words           — case-insensitive word-boundary regex
  2. banned openers         — anchored at paragraph starts
  3. first-person singular  — \\bI\\b / \\bmy\\b word boundaries
  4. owner attribution      — host(s) / owner(s) / your host
  5. numbers not in source  — every \\d(.\\d+)? in output must appear
                              as a substring of source_text

No LLM calls. No external state. Pure function in / pure function out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from betty_claw.site_config import SiteVoiceValidation


# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

# First-person singular: \bI\b and \bmy\b (case-insensitive — "My" at
# sentence start is just as forbidden as lowercase "my"). The \b boundaries
# prevent matching "Picton" or "myself" — only standalone tokens.
_FIRST_PERSON_PATTERN = re.compile(r"\b(I|my)\b", re.IGNORECASE)

# Owner attribution: host, hosts, owner, owners, "your host", "your hosts",
# "the host", "the hosts". Catches the most common attributions without
# being so broad it flags "hostel" or "owner-occupied" (the \b boundaries
# do this; "the host" is matched as the two-word phrase, not "the hostel").
_OWNER_ATTRIBUTION_PATTERN = re.compile(
    r"\b(?:host|hosts|owner|owners|your host|your hosts|the host|the hosts)\b",
    re.IGNORECASE,
)

# Numbers in text: integers and decimals. Negative numbers are rare in
# stay descriptions so we don't try to handle them; if they appear, the
# minus sign won't be captured but the number after it will be checked.
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    """One rule failure in the validated text.

    - rule         : short slug naming which check fired ("banned_word",
                     "banned_opener", "first_person_singular",
                     "owner_attribution", "number_not_in_source")
    - match        : the exact substring that triggered the violation
    - position     : character offset into the validated text
    - explanation  : human-readable note for Betty's self-correction loop
    """
    rule: str
    match: str
    position: int
    explanation: str


# ---------------------------------------------------------------------------
# Per-rule checks
# ---------------------------------------------------------------------------

def _check_banned_words(text: str, banned: tuple[str, ...]) -> list[Violation]:
    """Flag any banned word in text. Case-insensitive, word-boundary aware.

    Multi-word banned phrases (e.g., "hidden gem", "bucket list") are
    matched as substrings with case-insensitive comparison — the regex
    boundary trick doesn't work cleanly for phrases. Single-word entries
    use \\b boundaries so "perfect" doesn't match inside "imperfect".
    """
    violations: list[Violation] = []
    for word in banned:
        if " " in word:
            # Phrase — substring match, case-insensitive.
            pattern = re.compile(re.escape(word), re.IGNORECASE)
        else:
            pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            violations.append(Violation(
                rule="banned_word",
                match=m.group(0),
                position=m.start(),
                explanation=(
                    f"The word {m.group(0)!r} appears in the §1 Rule 7 "
                    f"banned list. Remove it or replace with a source-"
                    f"grounded specific."
                ),
            ))
    return violations


def _check_banned_openers(text: str, openers: tuple[str, ...]) -> list[Violation]:
    """Flag any banned opener at the start of a paragraph.

    Checks each paragraph (split by blank lines) — a banned opener
    buried mid-sentence is fine; the rule is about marketing-pitch
    OPENERS like "Escape to your perfect retreat" or "Welcome to...".
    Match is case-insensitive but \\b-anchored at the paragraph start.
    """
    violations: list[Violation] = []
    paragraphs = re.split(r"\n\s*\n", text)
    offset = 0
    for para in paragraphs:
        stripped = para.lstrip()
        leading_ws = len(para) - len(stripped)
        for opener in openers:
            pattern = re.compile(
                rf"^{re.escape(opener)}\b",
                re.IGNORECASE,
            )
            m = pattern.match(stripped)
            if m:
                violations.append(Violation(
                    rule="banned_opener",
                    match=m.group(0),
                    position=offset + leading_ws,
                    explanation=(
                        f"Paragraph opens with {m.group(0)!r}, which is a "
                        f"marketing-voice opener (see §1 Rule 7 and §3 "
                        f"sentence patterns). Lead with a concrete fact "
                        f"instead — village, room count, distance."
                    ),
                ))
        # +2 accounts for the blank-line delimiter we split on.
        offset += len(para) + 2
    return violations


def _check_first_person_singular(text: str) -> list[Violation]:
    """Flag any \\bI\\b or \\bmy\\b in text. Case-insensitive."""
    violations: list[Violation] = []
    for m in _FIRST_PERSON_PATTERN.finditer(text):
        violations.append(Violation(
            rule="first_person_singular",
            match=m.group(0),
            position=m.start(),
            explanation=(
                f"First-person singular {m.group(0)!r} found. travelpec.com "
                f"never uses 'I' or 'my' (§1 Rule 4). Editorial-we appears "
                f"only in framing sentences (§1 Rule 5)."
            ),
        ))
    return violations


def _check_owner_attribution(text: str) -> list[Violation]:
    """Flag any host/owner attribution in text."""
    violations: list[Violation] = []
    for m in _OWNER_ATTRIBUTION_PATTERN.finditer(text):
        violations.append(Violation(
            rule="owner_attribution",
            match=m.group(0),
            position=m.start(),
            explanation=(
                f"Owner attribution {m.group(0)!r} found. The property "
                f"exists; who runs it is not the reader's concern (§1 "
                f"Rule 3). Remove and rephrase to describe the property, "
                f"not the people."
            ),
        ))
    return violations


def _check_numbers_against_source(
    text: str, source_text: str
) -> list[Violation]:
    """Flag any number in text that doesn't appear as a substring of source.

    Implementation: for each \\d+(\\.\\d+)? match in text, check whether
    that exact numeric string appears anywhere in source_text. Substring
    check (not word-boundary) so "5" in source matches "5" inside
    "5 minutes" without needing space-around handling. Catches the
    "4.97 average rating" case (4.97 doesn't appear in source → flag)
    while allowing "5 minutes" when source says "5 minutes from Picton".
    """
    violations: list[Violation] = []
    seen: set[str] = set()
    for m in _NUMBER_PATTERN.finditer(text):
        num = m.group(0)
        if num in seen:
            # Don't flag the same number twice — once is enough signal.
            continue
        seen.add(num)
        if num not in source_text:
            violations.append(Violation(
                rule="number_not_in_source",
                match=num,
                position=m.start(),
                explanation=(
                    f"The number {num!r} appears in the rewrite but not "
                    f"in the source text. §1 Rule 1 forbids inventing "
                    f"specifics. If you cannot find this number in the "
                    f"parsed body or frontmatter, remove it."
                ),
            ))
    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_text(
    text: str,
    source_text: str,
    rules: SiteVoiceValidation,
) -> list[Violation]:
    """Run every enabled check against `text`. Returns the full violation list.

    The order of violations in the returned list is: banned words first
    (typically the most numerous), then openers, first-person, owner
    attribution, numbers. Each violation has a position so Betty can
    locate it in her draft.

    Empty list = compliant.
    """
    if not rules.enabled:
        return []
    violations: list[Violation] = []
    violations.extend(_check_banned_words(text, rules.banned_words))
    violations.extend(_check_banned_openers(text, rules.banned_openers))
    if rules.ban_first_person_singular:
        violations.extend(_check_first_person_singular(text))
    if rules.ban_owner_attribution:
        violations.extend(_check_owner_attribution(text))
    if rules.check_numbers_against_source:
        violations.extend(_check_numbers_against_source(text, source_text))
    return violations


def violation_to_dict(v: Violation) -> dict[str, Any]:
    """JSON-safe dict for MCP transport."""
    return {
        "rule": v.rule,
        "match": v.match,
        "position": v.position,
        "explanation": v.explanation,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Exercises every check with positive and negative cases."""
    print("voice_validation.py self-test\n")

    rules = SiteVoiceValidation(
        enabled=True,
        check_fields=("description", "persona"),
        banned_words=(
            "stunning", "luxurious", "cozy", "perfect", "charming",
            "hidden gem", "bucket list",
        ),
        banned_openers=("Escape", "Welcome", "Looking for"),
        ban_first_person_singular=True,
        ban_owner_attribution=True,
        check_numbers_against_source=True,
    )

    # ---- 1. clean text passes ----
    source = "Two bedrooms, 5 minutes from Picton, sleeps four."
    text = "A two-bedroom suite, five minutes from Picton, sleeping four."
    out = validate_text(text, source, rules)
    assert out == [], f"clean text should pass; got {out}"
    print("  [ok] clean text passes with no violations")

    # ---- 2. banned word detected ----
    text = "A cozy two-bedroom suite, five minutes from Picton."
    out = validate_text(text, source, rules)
    assert any(v.rule == "banned_word" and v.match.lower() == "cozy" for v in out)
    print("  [ok] banned word 'cozy' caught")

    # ---- 3. banned word boundary works ----
    text = "Perfection is the enemy of done. We focus on real outcomes."
    out = validate_text(text, source, rules)
    # 'perfect' is banned; 'Perfection' should NOT trigger (word boundary).
    assert not any(v.rule == "banned_word" and v.match.lower() == "perfect" for v in out), \
        f"'Perfection' should not match 'perfect' with word boundary; got {out}"
    print("  [ok] word boundary prevents 'Perfection' matching 'perfect'")

    # ---- 4. multi-word banned phrase ----
    text = "A hidden gem on the south shore."
    out = validate_text(text, source, rules)
    assert any(v.rule == "banned_word" and v.match.lower() == "hidden gem" for v in out)
    print("  [ok] multi-word phrase 'hidden gem' caught")

    # ---- 5. banned opener ----
    text = "Escape to a quiet two-bedroom suite, five minutes from Picton."
    out = validate_text(text, source, rules)
    assert any(v.rule == "banned_opener" for v in out)
    print("  [ok] banned opener 'Escape' caught")

    # ---- 6. banned opener only fires at paragraph start ----
    text = "A two-bedroom suite. Escape with the car for a day trip."
    out = validate_text(text, source, rules)
    assert not any(v.rule == "banned_opener" for v in out), \
        f"'Escape' mid-paragraph should not fire opener rule; got {out}"
    print("  [ok] mid-paragraph 'Escape' doesn't trigger opener rule")

    # ---- 7. first-person singular ----
    text = "I think this place is great. My partner agrees."
    out = validate_text(text, source, rules)
    assert sum(1 for v in out if v.rule == "first_person_singular") == 2
    print("  [ok] first-person 'I' and 'my' both caught")

    # ---- 8. first-person word boundary ----
    text = "Picton's main street is walkable. mysteries abound here too."
    out = validate_text(text, source, rules)
    # 'mysteries' should NOT match 'my' even though it starts with those letters.
    assert not any(v.rule == "first_person_singular" for v in out), \
        f"'mysteries' should not match 'my' with word boundary; got {out}"
    print("  [ok] 'mysteries' doesn't trigger first-person 'my'")

    # ---- 9. owner attribution ----
    text = "The hosts will meet you at the door."
    out = validate_text(text, source, rules)
    assert any(v.rule == "owner_attribution" for v in out)
    print("  [ok] 'hosts' caught as owner attribution")

    # ---- 10. owner attribution word boundary ----
    text = "There's a hostel down the street."
    out = validate_text(text, source, rules)
    # 'hostel' starts with 'host' but should NOT match with word boundary.
    assert not any(v.rule == "owner_attribution" for v in out), \
        f"'hostel' should not match 'host' with word boundary; got {out}"
    print("  [ok] 'hostel' doesn't trigger 'host' rule")

    # ---- 11. number not in source ----
    text = "Two bedrooms, 5 minutes from Picton, 4.97 average rating."
    source_no_rating = "Two bedrooms, 5 minutes from Picton, sleeps four."
    out = validate_text(text, source_no_rating, rules)
    assert any(v.rule == "number_not_in_source" and v.match == "4.97" for v in out)
    print("  [ok] hallucinated '4.97' caught as number-not-in-source")

    # ---- 12. number in source passes ----
    text = "Five-minute walk; 5 minutes from town."
    source = "Five-minute walk from the bakery; 5 minutes from town."
    out = validate_text(text, source, rules)
    assert not any(v.rule == "number_not_in_source" for v in out), \
        f"'5' appears in source — should not flag; got {out}"
    print("  [ok] source-grounded '5' passes the number check")

    # ---- 13. number appearing as substring still passes ----
    # "5" appears inside "150" which is in source.
    text = "We charge a 5% fee."  # 5 appears as substring of 150
    source = "Property capacity 150 guests for events."
    out = validate_text(text, source, rules)
    assert not any(v.rule == "number_not_in_source" for v in out)
    print("  [ok] substring number ('5' inside '150') passes")

    # ---- 14. disabled rules return empty even on violating text ----
    disabled = SiteVoiceValidation(
        enabled=False,
        check_fields=(),
        banned_words=("cozy",),
        banned_openers=(),
        ban_first_person_singular=True,
        ban_owner_attribution=True,
        check_numbers_against_source=True,
    )
    text = "I think this is the perfect cozy host. 4.97 rating."
    out = validate_text(text, "", disabled)
    assert out == [], "disabled rules should return no violations"
    print("  [ok] disabled rules short-circuit cleanly")

    print("\nvoice_validation.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
