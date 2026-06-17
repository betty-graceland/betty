"""
Deterministic Stays pipeline (Phase 3.0).

A CLI script that lists every pending Airbnb dossier for a site,
processes each end-to-end through parse → compose → validate → score
→ publish, and reports results. No agent loop. No tool reasoning. No
Qwen, no Hermes, no MCP. Just Python calling Claude for the content
composition step, with deterministic control flow for everything else.

The agent runtime architecture (compose_stays_draft_preview/publish,
worker prompts, SOUL.md directives) was the source of every reliability
failure we observed during Phase 2.0 testing — string-copying between
tool calls, scope-creep recovery cascades, single-task vs batch mode
ambiguity. The pipeline underneath that agent layer was always sound,
and this script is that pipeline directly.

USAGE
=====

    cd ~/code/betty
    uv run python -m betty_claw.process_pending_stays --site travelpec
    uv run python -m betty_claw.process_pending_stays --site travelpec --limit 3
    uv run python -m betty_claw.process_pending_stays --site travelpec --dry-run
    uv run python -m betty_claw.process_pending_stays --site travelpec --dossier-filename Foo.md

Exit code is 0 if every pending dossier was either successfully
published or cleanly skipped, 1 if any dossier failed in a way that
warrants operator inspection.

Cost: ~$0.02-0.10 per dossier on Haiku 4.5 (3-5 Anthropic calls per
draft including retries). Set ANTHROPIC_CONTENT_MODEL=claude-sonnet-4-6
to upgrade if voice quality on Haiku proves insufficient.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from betty_claw.anthropic_client import AnthropicClientError
from betty_claw.content_composer import ComposeResult, compose_field
from betty_claw.emdash_client import EmdashClient
from betty_claw.site_config import SiteConfig, load_site_config
from betty_claw.tools.airbnb_parser import (
    parse_airbnb_dossier as _parse_airbnb_dossier,
)
from betty_claw.tools.editorial_scorer import (
    score_editorial_quality as _score_editorial_quality,
)
from betty_claw.tools.emdash_writes import (
    emdash_create_content_draft as _emdash_create_content_draft,
)
from betty_claw.tools.voice_validation import (
    validate_text as _validate_text,
    violation_to_dict as _violation_to_dict,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("betty.pipeline")


# ---------------------------------------------------------------------------
# Retry budgets
# ---------------------------------------------------------------------------

# How many times each field gets re-asked of Claude after a validation
# failure. 2 means: initial attempt + up to 2 retries = 3 total tries
# per field. If we still have violations after the third, the dossier
# is marked failed for that field.
_VOICE_VALIDATION_RETRIES = 2

# How many times we re-ask for a low editorial score. Score-based
# retries are cheaper because they only trigger when the mechanical
# validator already passed.
_EDITORIAL_SCORE_RETRIES = 1

# Score threshold below which we ask Claude for another pass. 8/10 is
# the doc's published "ready" bar.
_EDITORIAL_SCORE_TARGET = 8


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DossierResult:
    """One dossier's end-state after processing.

    success=True means a draft was published. failed=True means the
    dossier reached a fatal error. skipped=True means it was filtered
    out (e.g., already published, or operator's --dossier-filename
    matched a different file).
    """
    dossier_filename: str
    success: bool = False
    failed: bool = False
    skipped: bool = False
    draft_id: str | None = None
    final_description: str | None = None
    final_persona: str | None = None
    editorial_score: int | None = None
    cost_usd: float = 0.0
    compose_calls: int = 0
    error_summary: str | None = None
    duration_s: float = 0.0


@dataclass
class BatchResult:
    """Outcome of one run of the script."""
    results: list[DossierResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_compose_calls: int = 0
    duration_s: float = 0.0

    @property
    def succeeded(self) -> list[DossierResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[DossierResult]:
        return [r for r in self.results if r.failed]

    @property
    def skipped(self) -> list[DossierResult]:
        return [r for r in self.results if r.skipped]


# ---------------------------------------------------------------------------
# Worklist helpers
# ---------------------------------------------------------------------------

import re as _re

_DOSSIER_URL_PATTERN = _re.compile(r"^url:\s*(.+?)\s*$", _re.MULTILINE)


def _extract_url_from_dossier(path: Path) -> str | None:
    """Read just the frontmatter to get the dossier's url field.

    Used to cross-reference against EmDash for already-published items.
    Mirrors the logic in mcp_server.list_pending_airbnb_dossiers so the
    two stay consistent.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                lines.append(line)
                if i > 50:
                    break
            head = "".join(lines)
    except OSError:
        return None
    m = _DOSSIER_URL_PATTERN.search(head)
    if not m:
        return None
    return m.group(1).strip()


def list_pending_dossiers(
    config: SiteConfig,
    emdash: EmdashClient,
) -> list[Path]:
    """Return paths to dossiers not yet published as Stays in EmDash.

    Walks the dossier directory, extracts each file's url, fetches the
    current Stays collection from EmDash, and returns dossiers whose
    url is not present. Same logic as the MCP worklist tool, but called
    directly as a Python function with no tool roundtrip.
    """
    dossier_dir = (
        config.paths.research
        / "01-source-data"
        / "research"
        / "airbnb-listings"
    )
    if not dossier_dir.exists():
        raise FileNotFoundError(
            f"Dossier directory not found: {dossier_dir}. "
            f"Check site.paths.research."
        )

    # Build set of URLs already in EmDash. Paginate as needed.
    published_urls: set[str] = set()
    cursor: str | None = None
    while True:
        args: dict[str, Any] = {"collection": "stays", "limit": 100}
        if cursor:
            args["cursor"] = cursor
        from betty_claw.tools.emdash_reads import emdash_list_content as _list
        result = _list(args, client=emdash)
        data = result.payload.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            item_data = item.get("data") or item
            if isinstance(item_data, dict):
                url = item_data.get("outbound_url")
                if isinstance(url, str) and url.strip():
                    published_urls.add(url.strip())
        next_cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not next_cursor:
            break
        cursor = next_cursor

    # Enumerate pending dossiers.
    pending: list[Path] = []
    for dossier_path in sorted(dossier_dir.glob("*.md")):
        url = _extract_url_from_dossier(dossier_path)
        if url and url not in published_urls:
            pending.append(dossier_path)
    return pending


# ---------------------------------------------------------------------------
# Per-dossier pipeline
# ---------------------------------------------------------------------------

def _parse_dossier(
    config: SiteConfig, dossier_path: Path,
) -> dict[str, Any]:
    """Parse one dossier into the data dict the rest of the pipeline uses.

    Wraps the underlying tools.airbnb_parser.parse_airbnb_dossier call
    with the site-config-derived allowed_roots, fixed_fields, and
    target collection schema. Returns the full payload dict.
    """
    parser_cfg = config.parser("airbnb_dossier")
    target_collection = parser_cfg.target_collection or "stays"
    result = _parse_airbnb_dossier(
        {"path": str(dossier_path)},
        allowed_roots=config.read_roots,
        fixed_fields=parser_cfg.fixed_fields,
        target_collection_schema=config.collections[target_collection],
    )
    return result.payload


def _compose_with_validation(
    *,
    field_name: str,
    parsed_value: str,
    body_excerpt: str,
    frontmatter: dict[str, Any],
    voice_doc_text: str,
    config: SiteConfig,
    source_text: str,
) -> tuple[str, float, int]:
    """Compose one field, validating after each attempt, retrying on
    violations.

    Returns (final_text, total_cost_usd, total_calls). Raises ValueError
    if all retries exhausted.
    """
    total_cost = 0.0
    total_calls = 0
    previous_attempt: str | None = None
    violations: list[dict[str, Any]] | None = None
    voice_rules = config.voice_validation

    for attempt in range(_VOICE_VALIDATION_RETRIES + 1):
        compose_result: ComposeResult = compose_field(
            field_name=field_name,
            parsed_value=parsed_value,
            body_excerpt=body_excerpt,
            frontmatter=frontmatter,
            voice_doc_text=voice_doc_text,
            domain=config.domain,
            previous_attempt=previous_attempt,
            violations=violations,
        )
        total_cost += compose_result.cost_usd
        total_calls += 1
        candidate = compose_result.text

        # Validate against mechanical voice rules.
        if voice_rules is None or not voice_rules.enabled:
            # No validation configured — accept first compose.
            return candidate, total_cost, total_calls

        v_records = _validate_text(candidate, source_text, voice_rules)
        if not v_records:
            logger.info(
                "  %s: validation passed on attempt %d/%d",
                field_name, attempt + 1, _VOICE_VALIDATION_RETRIES + 1,
            )
            return candidate, total_cost, total_calls

        violations = [_violation_to_dict(v) for v in v_records]
        previous_attempt = candidate
        logger.info(
            "  %s: attempt %d/%d failed validation (%d violation(s)), retrying",
            field_name, attempt + 1, _VOICE_VALIDATION_RETRIES + 1,
            len(violations),
        )

    raise ValueError(
        f"{field_name}: exhausted {_VOICE_VALIDATION_RETRIES + 1} attempts "
        f"without passing voice validation. Final violations: "
        f"{[v['rule'] + ':' + v['match'] for v in (violations or [])]}"
    )


def _score_with_retry(
    *,
    description: str,
    persona: str,
    body_excerpt: str,
    frontmatter: dict[str, Any],
    voice_doc_text: str,
    config: SiteConfig,
    source_text: str,
) -> tuple[str, int, float, int]:
    """Score description editorially; retry once if score < target.

    Returns (final_description, final_score, total_cost_usd, total_calls).
    The description may be replaced if the retry produces a better one;
    if the retry score is worse, we keep the original.
    """
    total_cost = 0.0
    total_calls = 0
    current = description
    final_score = 0

    for attempt in range(_EDITORIAL_SCORE_RETRIES + 1):
        try:
            score = _score_editorial_quality(
                text=current,
                source_text=source_text,
            )
        except AnthropicClientError as e:
            logger.warning(
                "  description: editorial scorer call failed: %s. "
                "Skipping score-based retry.", e,
            )
            return current, 0, total_cost, total_calls

        total_cost += score.cost_usd
        final_score = score.score
        logger.info(
            "  description: editorial score %d/10 on attempt %d/%d "
            "(%d violation(s))",
            score.score, attempt + 1, _EDITORIAL_SCORE_RETRIES + 1,
            len(score.violations),
        )
        if score.score >= _EDITORIAL_SCORE_TARGET and not score.violations:
            return current, score.score, total_cost, total_calls

        if attempt >= _EDITORIAL_SCORE_RETRIES:
            # Out of retries; accept the current draft even if score is low.
            return current, score.score, total_cost, total_calls

        # Retry: feed the score violations to the composer as feedback.
        retry_violations = [
            {
                "rule": v.category,
                "match": v.passage,
                "position": 0,
                "explanation": v.explanation,
            }
            for v in score.violations
        ]
        compose_result = compose_field(
            field_name="description",
            parsed_value="",  # we're improving on `current`, not the parser's raw
            body_excerpt=body_excerpt,
            frontmatter=frontmatter,
            voice_doc_text=voice_doc_text,
            domain=config.domain,
            previous_attempt=current,
            violations=retry_violations,
        )
        total_cost += compose_result.cost_usd
        total_calls += 1
        # Also re-validate the new candidate against mechanical rules
        # before we adopt it.
        voice_rules = config.voice_validation
        if voice_rules and voice_rules.enabled:
            v_records = _validate_text(
                compose_result.text, source_text, voice_rules,
            )
            if v_records:
                logger.info(
                    "  description: score-retry produced text that fails "
                    "mechanical validation; keeping previous attempt.",
                )
                # Don't adopt the retry; loop will exit on next iteration.
                continue
        current = compose_result.text

    return current, final_score, total_cost, total_calls


def process_one_dossier(
    *,
    config: SiteConfig,
    emdash: EmdashClient,
    dossier_path: Path,
    voice_doc_text: str,
    dry_run: bool,
) -> DossierResult:
    """Run the full pipeline for one dossier. Catches all exceptions
    and returns a DossierResult regardless of outcome — the batch
    runner should never crash on one bad dossier.
    """
    started = time.monotonic()
    result = DossierResult(dossier_filename=dossier_path.name)
    try:
        # 1. Parse.
        payload = _parse_dossier(config, dossier_path)
        parsed_data: dict[str, Any] = dict(payload["data"])
        frontmatter: dict[str, Any] = dict(payload.get("frontmatter") or {})
        body_excerpt: str = str(payload.get("body_excerpt") or "")
        source_text = f"{frontmatter}\n{body_excerpt}"
        logger.info(
            "  parsed: title=%r village=%r",
            parsed_data.get("title"), parsed_data.get("village"),
        )

        # 2. Compose + validate persona.
        persona_text, persona_cost, persona_calls = _compose_with_validation(
            field_name="persona",
            parsed_value=parsed_data.get("persona", ""),
            body_excerpt=body_excerpt,
            frontmatter=frontmatter,
            voice_doc_text=voice_doc_text,
            config=config,
            source_text=source_text,
        )
        result.cost_usd += persona_cost
        result.compose_calls += persona_calls

        # 3. Compose + validate description.
        desc_text, desc_cost, desc_calls = _compose_with_validation(
            field_name="description",
            parsed_value=parsed_data.get("description", ""),
            body_excerpt=body_excerpt,
            frontmatter=frontmatter,
            voice_doc_text=voice_doc_text,
            config=config,
            source_text=source_text,
        )
        result.cost_usd += desc_cost
        result.compose_calls += desc_calls

        # 4. Score description, retry once if low.
        final_desc, final_score, score_cost, score_calls = _score_with_retry(
            description=desc_text,
            persona=persona_text,
            body_excerpt=body_excerpt,
            frontmatter=frontmatter,
            voice_doc_text=voice_doc_text,
            config=config,
            source_text=source_text,
        )
        result.cost_usd += score_cost
        result.compose_calls += score_calls
        result.editorial_score = final_score

        # 5. Assemble final data and publish (or dry-run report).
        final_data = dict(parsed_data)
        final_data["description"] = final_desc
        final_data["persona"] = persona_text

        if dry_run:
            logger.info("  DRY RUN: would publish to EmDash (skipped)")
            result.final_description = final_desc
            result.final_persona = persona_text
            result.skipped = True
            return result

        from betty_claw.tools.emdash_writes import _validate_data_for_collection
        # Schema-validate before the write; the EmDash schema rejects
        # malformed data anyway but we want the failure to be on our
        # side of the network.
        validated = _validate_data_for_collection(
            config.collections["stays"],
            final_data,
            "process_pending_stays",
            partial=False,
        )
        publish_result = _emdash_create_content_draft(
            {"collection": "stays", "data": validated},
            client=emdash,
            collection_schema=config.collections["stays"],
        )
        response = publish_result.payload.get("data") or {}
        draft_id = (
            response.get("id")
            if isinstance(response, dict)
            else None
        ) or "(unknown)"

        result.draft_id = draft_id
        result.final_description = final_desc
        result.final_persona = persona_text
        result.success = True
        return result

    except Exception as e:  # broad: per-dossier failures shouldn't kill batch
        logger.exception("  pipeline failed: %s", e)
        result.failed = True
        result.error_summary = f"{type(e).__name__}: {e}"
        return result
    finally:
        result.duration_s = time.monotonic() - started


# ---------------------------------------------------------------------------
# Batch runner + reporting
# ---------------------------------------------------------------------------

def run_batch(
    *,
    site_id: str,
    limit: int | None,
    dossier_filename: str | None,
    dry_run: bool,
) -> BatchResult:
    """Process all (or limit) pending dossiers for site_id."""
    started = time.monotonic()
    config = load_site_config(site_id)
    emdash = EmdashClient(
        token=config.emdash.token,
        url=config.emdash.mcp_url,
    )

    voice_doc_path = config.voice_doc_full_path
    if voice_doc_path is None or not voice_doc_path.exists():
        raise FileNotFoundError(
            f"Voice doc not found at {voice_doc_path}. Site "
            f"{site_id!r} cannot run the pipeline without a voice doc."
        )
    voice_doc_text = voice_doc_path.read_text(encoding="utf-8")
    logger.info("Loaded voice doc: %d bytes", len(voice_doc_text))

    pending = list_pending_dossiers(config, emdash)
    logger.info("Found %d pending dossier(s) for site %r", len(pending), site_id)

    if dossier_filename:
        pending = [p for p in pending if p.name == dossier_filename]
        if not pending:
            logger.warning(
                "No pending dossier matches filename %r", dossier_filename,
            )
    if limit is not None:
        pending = pending[:limit]
        logger.info("Limited to first %d dossier(s)", limit)

    batch = BatchResult()
    for i, dossier_path in enumerate(pending, start=1):
        logger.info(
            "[%d/%d] processing %s", i, len(pending), dossier_path.name,
        )
        result = process_one_dossier(
            config=config,
            emdash=emdash,
            dossier_path=dossier_path,
            voice_doc_text=voice_doc_text,
            dry_run=dry_run,
        )
        batch.results.append(result)
        batch.total_cost_usd += result.cost_usd
        batch.total_compose_calls += result.compose_calls
        status = (
            "✓ published"
            if result.success
            else "○ dry-run-skipped"
            if result.skipped
            else "✗ FAILED"
        )
        logger.info(
            "[%d/%d] %s: %s (draft_id=%s, score=%s, cost=$%.4f, %.1fs)",
            i, len(pending), result.dossier_filename, status,
            result.draft_id or "-",
            result.editorial_score if result.editorial_score is not None else "-",
            result.cost_usd, result.duration_s,
        )

    batch.duration_s = time.monotonic() - started
    return batch


def print_summary(batch: BatchResult) -> None:
    """Render the end-of-run report to stdout."""
    print()
    print("=" * 72)
    print(f"BATCH COMPLETE — {batch.duration_s:.1f}s total")
    print("=" * 72)
    print(f"  succeeded     : {len(batch.succeeded)}")
    print(f"  failed        : {len(batch.failed)}")
    print(f"  skipped       : {len(batch.skipped)}")
    print(f"  total cost    : ${batch.total_cost_usd:.4f}")
    print(f"  compose calls : {batch.total_compose_calls}")
    print()
    if batch.succeeded:
        print("Published drafts:")
        for r in batch.succeeded:
            print(
                f"  ✓ {r.dossier_filename:48s} "
                f"draft_id={r.draft_id} score={r.editorial_score}/10"
            )
    if batch.failed:
        print()
        print("Failures (require operator review):")
        for r in batch.failed:
            print(f"  ✗ {r.dossier_filename:48s} {r.error_summary}")
    if batch.skipped:
        print()
        print(f"Skipped (dry run or filter): {len(batch.skipped)} dossier(s)")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process pending Airbnb dossiers into Stays drafts via "
            "Claude-driven content composition. Phase 3.0 pipeline."
        ),
    )
    parser.add_argument(
        "--site", required=True,
        help="Site ID slug (e.g., 'travelpec').",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap on number of dossiers to process this run.",
    )
    parser.add_argument(
        "--dossier-filename", default=None,
        help="Process only the dossier with this exact filename.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Run the full compose+validate+score pipeline but do not "
            "write to EmDash. Costs the same Anthropic spend but does "
            "not produce drafts."
        ),
    )
    args = parser.parse_args()

    try:
        batch = run_batch(
            site_id=args.site,
            limit=args.limit,
            dossier_filename=args.dossier_filename,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by operator.", file=sys.stderr)
        return 130
    except Exception as e:
        logger.exception("Batch failed before any dossier processing: %s", e)
        return 1

    print_summary(batch)
    return 0 if not batch.failed else 1


if __name__ == "__main__":
    sys.exit(main())
