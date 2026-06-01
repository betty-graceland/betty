"""
Airbnb dossier parser (Phase 4.6.1, refactored 2026-05-31 for Pattern B).

Reads an Airbnb research dossier from a site's read-allowed paths and
returns a dict shaped to that site's Stays collection schema. This is the
tool that bridges scraped Airbnb dossiers into Betty's content-population
pipeline: parse → emdash_create_content_draft → (human review) →
emdash_publish_content.

PATTERN B MULTI-SITE
====================
This module is site-agnostic. The MCP server wrapper resolves the active
site config (~/.betty/sites/{site}.yaml) and passes `allowed_roots` and
`fixed_fields` as explicit arguments. The parser does NOT read environment
variables, filesystem globals, or any other ambient state — everything
that varies per site flows in through the args dict. This makes the same
parser work for travelpec.com today, lingerieshoppe.com next, and any
future directory site that ingests Airbnb listings.

DOSSIER SHAPE (observed 2026-05-26 from one sample, presumed uniform
across the ~35 listings)
========================
YAML frontmatter at the top, delimited by `---` lines, followed by a
markdown body of scraped Airbnb page text. Frontmatter fields seen:

  url:             absolute Airbnb URL (-> Stays.outbound_url)
  listing_id:      integer Airbnb listing ID
  title:           listing title string (-> Stays.title)
  property_type:   e.g. "Entire home in Prince Edward County, Canada"
  village:         e.g. "Sandbanks" (-> Stays.village)
  guests:          integer max guests (-> Stays.capacity)
  bedrooms:        integer bedrooms (-> Stays.bedrooms)
  beds:            integer beds
  baths:           float baths
  superhost:       boolean
  host_name:       string
  years_hosting:   integer
  license:         string
  rating:          float (0-5)
  review_count:    integer
  price_per_night: string (often empty)
  captured_at:     ISO 8601 timestamp
  images:          list of URL strings

The body is the raw scraped page text, full of navigation cruft
("Show all photos", review cards, "Add dates for prices", etc.) plus
the actual listing description. The parser extracts a `persona`
(short, ~one-sentence summary) and a longer `description` from the
body's substantive content.

OUTPUT SHAPE
============
Returns a Stays-compatible dict that passes
`_validate_data_for_collection('stays', data, partial=False)`. The
ToolResult payload includes:

  - `data`: the Stays dict (ready for emdash_create_content_draft)
  - `frontmatter`: the raw parsed frontmatter (for Qwen to inspect /
    cross-reference if needed)
  - `body_excerpt`: the cleaned-up body content (cruft-stripped),
    for Qwen to compose richer content if she wants
  - `summary`: human-readable one-liner

HARD RULES ENFORCED
===================
- Site-level invariants (e.g., `provider: airbnb`, `is_advertised: 0`,
  `featured_eligible: 0`) flow in via the `fixed_fields` arg from the
  site's parsers.airbnb_dossier.fixed_fields block in YAML. The parser
  overwrites any value the dossier might claim — site config wins.
- Images stay as `<!-- IMAGE: ... -->` placeholders in any composed
  content. The parser does not embed actual image URLs in body text.
- The parser is read_only — no MCP calls, no filesystem writes.

DEPENDENCY POSTURE
==================
Rolls a minimal YAML-frontmatter parser rather than pulling PyYAML
into betty-claw. The frontmatter shape is constrained (flat key:value
plus the images list) and the rolled parser handles exactly those
cases. If the dossier format grows (nested objects, multi-line
strings), revisit.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from betty_claw.contracts import ToolResult
from betty_claw.site_config import SiteCollection
from betty_claw.tools.emdash_reads import _assert_dict_keys, _assert_str
from betty_claw.tools.emdash_writes import _validate_data_for_collection
from betty_claw.tools.filesystem import _validate_path_under


# ---------------------------------------------------------------------------
# YAML frontmatter parser (minimal — handles exactly the dossier shape)
# ---------------------------------------------------------------------------

# Top-level key:value line. The key is `^[a-z_]+`, value is everything
# after the first `:` and a single space, with optional quotes.
_KEY_VALUE_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.*)$")

# List-item line under an enclosing key.
_LIST_ITEM_RE = re.compile(r"^\s+-\s+(.*)$")


def _coerce_scalar(raw: str) -> Any:
    """Convert a YAML-ish scalar string into a Python value.

    Handles: empty, true/false, integer, float, and quoted/unquoted
    string. Quoted strings have their outer quotes stripped.
    """
    if raw == "":
        return ""
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null" or raw == "~":
        return None
    # Strip outer quotes (single or double) if present.
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
        return raw[1:-1]
    # Integer?
    try:
        return int(raw)
    except ValueError:
        pass
    # Float?
    try:
        return float(raw)
    except ValueError:
        pass
    # Bare string.
    return raw


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract the YAML frontmatter block from `text`.

    Returns (frontmatter_dict, body_text). Frontmatter is identified as
    the block between the first `---\\n` line and the next `---\\n` line.
    If no frontmatter is present, returns ({}, text).

    Handles flat key:value pairs plus lists (key: with `- item` lines
    on subsequent lines). When a key with empty value is followed by
    list items, the value gets promoted from "" to [] on the first
    list item. A key with empty value and no following list items
    stays as "".

    Raises ValueError if the frontmatter is malformed (e.g., opening
    `---` with no closing).
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    # Find the closing ---
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(
            "Frontmatter opening `---` found but no closing `---` line. "
            "Cannot parse."
        )

    frontmatter_lines = lines[1:close_idx]
    body_lines = lines[close_idx + 1:]
    body = "\n".join(body_lines)

    fm: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw_line in frontmatter_lines:
        if raw_line.strip() == "" or raw_line.lstrip().startswith("#"):
            continue

        # List item under the current key — promote str→list on first hit.
        list_match = _LIST_ITEM_RE.match(raw_line)
        if list_match and current_list_key is not None:
            value = _coerce_scalar(list_match.group(1).strip())
            if not isinstance(fm[current_list_key], list):
                fm[current_list_key] = []
            fm[current_list_key].append(value)
            continue

        # Top-level key:value
        kv_match = _KEY_VALUE_RE.match(raw_line)
        if not kv_match:
            continue

        key = kv_match.group(1)
        raw_value = kv_match.group(2).rstrip()

        if raw_value == "":
            # Empty value — could be an empty string field (e.g.
            # price_per_night:) or the opening of a list (e.g. images:
            # followed by `- url`). Set to "" for now; list items on
            # subsequent lines promote it to a list.
            fm[key] = ""
            current_list_key = key
        else:
            fm[key] = _coerce_scalar(raw_value)
            current_list_key = None

    return fm, body


# ---------------------------------------------------------------------------
# Body content extraction
# ---------------------------------------------------------------------------

# Lines/patterns that are pure Airbnb-page navigation cruft and should
# be filtered from any extracted body text.
_CRUFT_PATTERNS = [
    re.compile(r"^Share$"),
    re.compile(r"^Save$"),
    re.compile(r"^Show all photos$"),
    re.compile(r"^Show all \d+ amenities$"),
    re.compile(r"^Show more$"),
    re.compile(r"^Show all \d+ reviews$"),
    re.compile(r"^\d+\s+(?:guests?|bedrooms?|beds?|baths?)"),
    re.compile(r"^Guest$"),
    re.compile(r"^favourite$"),
    re.compile(r"^Rated [\d.]+ out of 5"),
    re.compile(r"^[\d.]+$"),  # bare numbers
    re.compile(r"^Reviews$"),
    re.compile(r"^Hosted by"),
    re.compile(r"^Superhost"),
    re.compile(r"^Listing highlights$"),
    re.compile(r"^Top \d+% of homes$"),
    re.compile(r"^Self check-in$"),
    re.compile(r"^Unbeatable location$"),
    re.compile(r"^Check yourself in"),
    re.compile(r"^\d+% of guests"),
    re.compile(r"^This home is"),
    re.compile(r"^Add dates? "),
    re.compile(r"^Check (availability|in|out)"),
    re.compile(r"^Add (your travel dates|date)"),
    re.compile(r"^Clear dates"),
    re.compile(r"^Report this listing"),
    re.compile(r"^How reviews work$"),
    re.compile(r"^\d+ of \d+ items showing$"),
    re.compile(r"^Overall rating$"),
    re.compile(r"^\d+ stars?,"),
    re.compile(r"^\d+$"),
    re.compile(r"^Cleanliness$|^Accuracy$|^Check-in$|^Communication$"
               r"|^Location$|^Value$"),
    re.compile(r"^More stays nearby$"),
    re.compile(r"^Explore other options"),
    re.compile(r"^Other types of stays on Airbnb$"),
    re.compile(r"^Vacation rentals$"),
    re.compile(r"^AirbnbCanadaOntario"),
    re.compile(r"^Where you'?ll (be|sleep)$"),
    re.compile(r"^Meet your host$"),
    re.compile(r"^Things to know$"),
    re.compile(r"^Cancellation policy$"),
    re.compile(r"^House rules$"),
    re.compile(r"^Safety & property$"),
    re.compile(r"^What this place offers$"),
    re.compile(r"^Bedroom \d+$"),
    re.compile(r"^Select check-in date$"),
    re.compile(r"^SMTWTFS$"),
    re.compile(r"^(?:January|February|March|April|May|June|July|August|"
               r"September|October|November|December)\s+\d{4}$"),
    re.compile(r"^Add your trip dates"),
    re.compile(r"^\$[\d,]+ CAD$"),
    re.compile(r"^★ [\d.]+$"),
    re.compile(r"^CHECK-?(IN|OUT)$"),
    re.compile(r"^GUESTS$"),
    re.compile(r"^\d+ guests?$"),
    re.compile(r"^1 (?:queen|king|double|single|sofa) bed$"),
    re.compile(r"^[A-Z][a-zA-Z]+,?\s*(?:Canada|USA|United States|UK)$"),
    re.compile(r"^Stayed (?:a few nights|one night|with kids|"
               r"with .* people|on .* trip|with pet|with infants?|"
               r"\d+ days?|\d+ months? on Airbnb)"),
    re.compile(r"^\d+ years? (?:hosting|on Airbnb)$"),
    re.compile(r"^Pets: "),
    re.compile(r"^Lives in "),
    re.compile(r"^Host details$"),
    re.compile(r"^Response rate"),
    re.compile(r"^Responds within"),
    re.compile(r"^Message host$"),
    re.compile(r"^To help protect your payment"),
    re.compile(r"^Patty is a Superhost$"),  # falsly broad; remove if real listings get caught
    re.compile(r"^Superhosts are experienced"),
    re.compile(r"^Check-?in: \d"),
    re.compile(r"^Check-?out: \d"),
    re.compile(r"^Show more$"),
    re.compile(r"^Registration details$"),
    re.compile(r"^Add date$"),
    re.compile(r"^Group trip$"),
    re.compile(r"^Neighbourhood highlights$"),
]


def _is_cruft(line: str) -> bool:
    """Return True if `line` matches a known Airbnb-page cruft pattern."""
    stripped = line.strip()
    if stripped == "":
        return True
    for pattern in _CRUFT_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def _extract_persona(body: str, fallback_title: str) -> str:
    """Extract a one-sentence/short-paragraph persona summary.

    Looks for the first content line in the body that:
      - is at least 60 chars long (filters out short labels)
      - isn't a known cruft pattern
      - doesn't start with a number or single capitalized word

    Falls back to a synthesized "<title> — Prince Edward County stay."
    if no descriptive line is found (extremely unlikely but defensive).
    """
    for raw in body.split("\n"):
        line = raw.strip()
        if len(line) < 60:
            continue
        if _is_cruft(line):
            continue
        # Take the first sentence (up to first . ! ?) so personas stay
        # tight. If no sentence boundary, take the whole line.
        sentence_end = -1
        for i, ch in enumerate(line):
            if ch in ".!?" and i > 30:
                sentence_end = i
                break
        if sentence_end != -1:
            return line[: sentence_end + 1].strip()
        return line.strip()

    return f"{fallback_title} — Prince Edward County stay."


def _extract_description(body: str) -> str:
    """Extract a longer descriptive block from the body.

    Strategy: take all non-cruft lines from the body, join them with
    single newlines, cap at ~2000 chars. This gives Qwen a clean,
    cruft-stripped block she can either pass through verbatim or
    rewrite into editorial voice.
    """
    clean_lines = [
        line.strip()
        for line in body.split("\n")
        if not _is_cruft(line)
    ]
    # Collapse runs of blank lines that survived the filter.
    text = "\n".join(line for line in clean_lines if line)
    return text[:2000].strip()


def _map_property_type(property_type: str | None) -> str:
    """Map an Airbnb `property_type` string to a Stays schema_subtype.

    Existing Stays entries use: Apartment, VacationRental, BedAndBreakfast.
    Defaults to VacationRental for unknown shapes — closest fit for
    most Airbnb listings.
    """
    if not property_type:
        return "VacationRental"
    lower = property_type.lower()
    if "apartment" in lower:
        return "Apartment"
    if "bed and breakfast" in lower or "b&b" in lower or "bedandbreakfast" in lower:
        return "BedAndBreakfast"
    if "hotel" in lower or "boutique hotel" in lower:
        return "Hotel"
    # Default — covers "Entire home", "Cottage", "Cabin", "Villa", etc.
    return "VacationRental"


# ---------------------------------------------------------------------------
# Tool: parse_airbnb_dossier
# ---------------------------------------------------------------------------

PARSE_AIRBNB_DOSSIER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "parse_airbnb_dossier",
        "description": (
            "Parse one Airbnb research dossier into a Stays-collection-"
            "compatible dict. Reads a markdown file with YAML frontmatter "
            "from one of the site's read-allowed roots (passed in by the "
            "MCP server from ~/.betty/sites/{site}.yaml). Returns the Stays "
            "data ready to pass to emdash_create_content_draft, plus the "
            "raw frontmatter and a cruft-stripped body excerpt for Qwen "
            "to optionally compose a richer description from. Site-level "
            "fixed_fields (provider, is_advertised, featured_eligible, etc.) "
            "are merged into the result from the site config; the dossier "
            "cannot override them. Images are NOT embedded in returned "
            "data; they stay as placeholder comments at content-write time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-prefixed path to the dossier .md "
                        "file. Must resolve under one of the site's read "
                        "roots (paths.astro / paths.docs / paths.research)."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


def parse_airbnb_dossier(
    args: dict,
    *,
    allowed_roots: tuple[Path, ...],
    fixed_fields: dict[str, Any],
    target_collection_schema: SiteCollection,
) -> ToolResult:
    """Parse one Airbnb dossier. risk_class=read_only.

    Args:
        args: Tool-call kwargs from MCP. Must contain `path`.
        allowed_roots: Read-allowed filesystem roots for the active site,
            from SiteConfig.read_roots. The dossier path must resolve under
            one of these.
        fixed_fields: Site-level invariants to merge into the Stays dict
            after parsing (overrides anything from the dossier). For
            travelpec these are `provider: airbnb`, `is_advertised: 0`,
            `featured_eligible: 0` per the v3 BRIEF.
        target_collection_schema: SiteCollection for the parser's target
            collection (e.g., site.collections["stays"] when parser_cfg
            says target_collection="stays"). Used for defense-in-depth
            validation of the parsed dict against the site's schema
            before returning.
    """
    _assert_dict_keys(
        args, required={"path"}, optional=set(),
        tool_name="parse_airbnb_dossier",
    )
    _assert_str(args["path"], "path", "parse_airbnb_dossier")

    if not allowed_roots:
        # Defense-in-depth: an empty allow-list would let any path through
        # _validate_path_under as "not under any root" → reject. But making
        # this explicit means the operator sees the real problem (site
        # config bug) instead of a misleading path-traversal error.
        raise ValueError(
            "parse_airbnb_dossier: allowed_roots is empty. The MCP server "
            "must pass site.read_roots from the active site config."
        )

    # Path must live under one of the site's read-allowed roots.
    path = _validate_path_under(args["path"], allowed_roots)
    if not path.exists():
        raise ValueError(f"Dossier file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Dossier path is not a regular file: {path}")

    content = path.read_text(encoding="utf-8")

    try:
        frontmatter, body = _parse_frontmatter(content)
    except ValueError as e:
        raise ValueError(
            f"parse_airbnb_dossier: failed to parse frontmatter in {path}: {e}"
        ) from e

    # Required Stays fields. Missing any of these in the frontmatter
    # is a dossier-quality problem, not a parser bug — surface clearly.
    missing: list[str] = []
    for required_key in ("title", "village", "url"):
        if required_key not in frontmatter or frontmatter[required_key] in ("", None):
            missing.append(required_key)
    if missing:
        raise ValueError(
            f"parse_airbnb_dossier: dossier {path.name!r} is missing "
            f"required frontmatter field(s): {missing}. Each Airbnb "
            f"dossier must have at minimum title, village, url."
        )

    title = str(frontmatter["title"]).strip()
    persona = _extract_persona(body, fallback_title=title)
    description = _extract_description(body)

    stays_data: dict[str, Any] = {
        "title": title,
        "village": str(frontmatter["village"]).strip(),
        "persona": persona,
        "outbound_url": str(frontmatter["url"]).strip(),
    }

    # Optional fields — populated only when present in frontmatter.
    if "bedrooms" in frontmatter and isinstance(frontmatter["bedrooms"], (int, float)):
        stays_data["bedrooms"] = float(frontmatter["bedrooms"])
    if "guests" in frontmatter and isinstance(frontmatter["guests"], (int, float)):
        stays_data["capacity"] = float(frontmatter["guests"])
    if "property_type" in frontmatter:
        stays_data["schema_subtype"] = _map_property_type(
            frontmatter["property_type"]
        )
    if description:
        stays_data["description"] = description

    # Merge site-level fixed fields LAST so they always win over anything
    # the dossier or the optional-field block might claim. This is how
    # travelpec's `provider: airbnb`, `is_advertised: 0`, and
    # `featured_eligible: 0` invariants get enforced on every parse.
    stays_data.update(fixed_fields)

    # Defense-in-depth: validate the assembled dict against the target
    # collection's schema before returning. If the parser produced
    # something the write tool would reject, that's a parser bug we
    # want to catch here, not when emdash_create_content_draft tries it.
    validated = _validate_data_for_collection(
        target_collection_schema,
        stays_data,
        "parse_airbnb_dossier",
        partial=False,
    )

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="parse_airbnb_dossier",
        status="executed",
        payload={
            "data": validated,
            "frontmatter": frontmatter,
            "body_excerpt": description,
            "summary": (
                f"Parsed {path.name!r}: title={title!r}, "
                f"village={stays_data['village']!r}, "
                f"bedrooms={stays_data.get('bedrooms', '?')}, "
                f"capacity={stays_data.get('capacity', '?')}."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Self-test the parser end-to-end.

    Uses a synthetic dossier written to a tempdir so the test runs
    deterministically without depending on any external filesystem state.
    The synthetic content mirrors the observed real-dossier shape (YAML
    frontmatter + scraped page body). allowed_roots and fixed_fields are
    constructed locally — this mirrors how the MCP server will build them
    from site config at tool-call time.
    """
    import shutil
    import tempfile

    print("Phase 4.6.1 Airbnb dossier parser self-test (Pattern B)\n")

    # Write a synthetic dossier into a scratch dir under BETTY_DOCS_DIR
    # so the path validator accepts it. If BETTY_DOCS_DIR doesn't
    # exist on this machine, we skip the live-path test and exercise
    # the inner parsers only.
    synthetic = """\
---
url: https://www.airbnb.ca/rooms/12345678
listing_id: 12345678
title: "Cozy Test Cottage near Sandbanks"
property_type: "Entire home in Prince Edward County, Canada"
village: Wellington
guests: 4
bedrooms: 2
beds: 2
baths: 1.5
superhost: true
host_name: Testy
years_hosting: 3
license: ST-TEST-0001
rating: 4.9
review_count: 42
price_per_night:
captured_at: 2026-05-26T12:00:00.000Z
images:
  - https://example.com/img1.jpg
  - https://example.com/img2.jpg
---

Cozy Test Cottage near Sandbanks

Share
Save
Show all photos
Entire home in Prince Edward County, Canada
4 guests · 2 bedrooms · 2 beds · 1.5 baths

Guest favourite
Rated 4.9 out of 5 stars.
4.9
42 reviews
42
Reviews

Hosted by Testy
Superhost · 3 years hosting
Listing highlights
Top 5% of homes
This home is highly ranked based on ratings, reviews, and reliability.

Welcome to a charming two-bedroom retreat in the heart of Wellington wine country, a five-minute walk from the lake and the bakery on the corner. This is a quiet getaway built for couples and small families who want walkable village access without giving up the country setting.

The space
A modern kitchen, a wood-burning stove, and a private back garden round out the experience.

Show more
"""

    # First test the frontmatter parser in isolation.
    fm, body = _parse_frontmatter(synthetic)
    assert fm["title"] == "Cozy Test Cottage near Sandbanks", fm
    assert fm["village"] == "Wellington"
    assert fm["url"] == "https://www.airbnb.ca/rooms/12345678"
    assert fm["guests"] == 4
    assert fm["bedrooms"] == 2
    assert fm["baths"] == 1.5
    assert fm["superhost"] is True
    assert fm["price_per_night"] == ""
    assert fm["images"] == ["https://example.com/img1.jpg",
                            "https://example.com/img2.jpg"], fm.get("images")
    print(f"  [ok] frontmatter parsed: {len(fm)} fields, "
          f"images={len(fm['images'])}")

    # Body should not contain the frontmatter delimiters.
    assert "---" not in body.split("\n")[0]
    assert "Welcome to a charming" in body
    print("  [ok] body extracted (no frontmatter delimiters in body)")

    # Persona extraction should find the descriptive paragraph.
    persona = _extract_persona(body, fallback_title=fm["title"])
    assert "two-bedroom retreat" in persona or "charming" in persona, persona
    assert len(persona) < 400, f"persona too long: {len(persona)}"
    print(f"  [ok] persona extracted ({len(persona)} chars): {persona!r}")

    # Description should be longer than persona and cruft-stripped.
    description = _extract_description(body)
    assert "Share" not in description
    assert "Show all photos" not in description
    assert "Welcome to a charming" in description
    print(f"  [ok] description extracted ({len(description)} chars, "
          f"cruft-stripped)")

    # property_type → schema_subtype mapping.
    assert _map_property_type("Entire home in PEC, Canada") == "VacationRental"
    assert _map_property_type("Apartment in Picton, Canada") == "Apartment"
    assert _map_property_type("Bed and breakfast near wineries") == "BedAndBreakfast"
    assert _map_property_type(None) == "VacationRental"
    assert _map_property_type("") == "VacationRental"
    print("  [ok] property_type → schema_subtype mapping")

    # Full tool round-trip against synthetic dossier in a controlled tempdir.
    # allowed_roots / fixed_fields / target_collection_schema are constructed
    # locally — the parser is site-agnostic; the MCP server wires real site
    # config in for production calls.
    #
    # Resolve the tempdir path because macOS tempdirs live under
    # /var/folders/... which is a symlink to /private/var/folders/...
    # _validate_path_under resolves the INPUT path before comparing, so the
    # allowed-root has to be resolved too or they'll never match. Production
    # paths (/Users/betty/...) aren't symlinks, so SiteConfig doesn't need
    # this — it's only a tempfile-on-macOS artifact.
    scratch = Path(tempfile.mkdtemp(prefix="betty-parser-selftest-")).resolve()
    allowed_roots = (scratch,)
    fixed_fields = {
        "provider": "airbnb",
        "is_advertised": 0,
        "featured_eligible": 0,
    }
    # Synthetic SiteCollection mirroring travelpec.yaml's stays schema.
    stays_schema = SiteCollection(
        slug="stays",
        fields={
            "title": "text",
            "village": "text",
            "persona": "text",
            "bedrooms": "number",
            "capacity": "number",
            "outbound_url": "text",
            "provider": "text",
            "is_advertised": "boolean",
            "featured_eligible": "boolean",
            "schema_subtype": "text",
            "description": "text",
        },
        required=("title", "village", "persona", "outbound_url", "provider"),
    )
    try:
        sample_path = scratch / "synthetic_cozy_test_cottage.md"
        sample_path.write_text(synthetic, encoding="utf-8")

        result = parse_airbnb_dossier(
            {"path": str(sample_path)},
            allowed_roots=allowed_roots,
            fixed_fields=fixed_fields,
            target_collection_schema=stays_schema,
        )
        assert result.status == "executed"
        data = result.payload["data"]
        assert data["title"] == "Cozy Test Cottage near Sandbanks"
        assert data["village"] == "Wellington"
        assert data["provider"] == "airbnb"
        assert data["is_advertised"] is False
        assert data["featured_eligible"] is False
        assert data["outbound_url"] == "https://www.airbnb.ca/rooms/12345678"
        assert data["bedrooms"] == 2.0
        assert data["capacity"] == 4.0
        assert data["schema_subtype"] == "VacationRental"
        assert "persona" in data and len(data["persona"]) > 0
        print(f"  [ok] full tool round-trip — Stays dict validates against "
              f"COLLECTION_SCHEMAS")

        # fixed_fields override anything the dossier might claim. Verify by
        # injecting bogus values into the dossier and confirming site config
        # wins.
        adversarial_dossier = synthetic.replace(
            "Cozy Test Cottage near Sandbanks",
            "Cozy Test Cottage near Sandbanks",
        )
        # We can't easily inject `provider:` into the parser since it
        # doesn't read that frontmatter key, but we can verify that
        # different fixed_fields produce different output.
        result2 = parse_airbnb_dossier(
            {"path": str(sample_path)},
            allowed_roots=allowed_roots,
            fixed_fields={"provider": "airbnb",
                          "is_advertised": 1,  # Different from default
                          "featured_eligible": 0},
            target_collection_schema=stays_schema,
        )
        assert result2.payload["data"]["is_advertised"] is True, (
            "fixed_fields should win over defaults"
        )
        print("  [ok] fixed_fields override applied correctly")

        # Path traversal blocked.
        try:
            parse_airbnb_dossier(
                {"path": "/etc/passwd"},
                allowed_roots=allowed_roots,
                fixed_fields=fixed_fields,
                target_collection_schema=stays_schema,
            )
        except ValueError as e:
            assert "not under any allowed root" in str(e)
            print("  [ok] path traversal blocked")
        else:
            raise AssertionError("parse_airbnb_dossier should reject /etc/passwd")

        # Empty allowed_roots → clear error.
        try:
            parse_airbnb_dossier(
                {"path": str(sample_path)},
                allowed_roots=(),
                fixed_fields=fixed_fields,
                target_collection_schema=stays_schema,
            )
        except ValueError as e:
            assert "allowed_roots is empty" in str(e)
            print("  [ok] empty allowed_roots caught")
        else:
            raise AssertionError("empty allowed_roots should error")

        # Missing required key.
        try:
            parse_airbnb_dossier(
                {},
                allowed_roots=allowed_roots,
                fixed_fields=fixed_fields,
                target_collection_schema=stays_schema,
            )
        except ValueError as e:
            assert "missing required keys" in str(e)
            print("  [ok] missing path arg rejected")
        else:
            raise AssertionError("parse_airbnb_dossier should reject empty args")

        # Dossier missing required frontmatter field.
        bad_path = scratch / "missing_title.md"
        bad_path.write_text(
            "---\nvillage: Wellington\nurl: https://www.airbnb.ca/rooms/1\n---\nbody\n",
            encoding="utf-8",
        )
        try:
            parse_airbnb_dossier(
                {"path": str(bad_path)},
                allowed_roots=allowed_roots,
                fixed_fields=fixed_fields,
                target_collection_schema=stays_schema,
            )
        except ValueError as e:
            assert "missing required frontmatter field" in str(e)
            print("  [ok] dossier missing required frontmatter field caught")
        else:
            raise AssertionError("dossier without title should error")

        print("\nairbnb_parser.py self-test PASSED")

    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    _self_test()
