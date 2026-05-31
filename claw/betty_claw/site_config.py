"""
Site config loader for the OpenClaw MCP server (Phase 4.7 Pattern B).

Every website Betty operates on is defined by a YAML config at
~/.betty/sites/{site_id}.yaml. The MCP server discovers sites at startup,
loads/validates configs, and resolves per-site paths, tokens, allow-lists,
branch policies, collection schemas, and parser configs for every tool call.

This is the Pattern B multi-site implementation locked on 2026-05-31. Every
tool exposed by the MCP server takes `site` as its first parameter; the server
uses this loader to translate the slug into concrete config and pass the
relevant pieces (allow-list roots, fixed parser fields, EmDash URL+token, etc.)
to the underlying betty_claw tool function.

The "site a day" goal — Peter wants to spin up new directory sites quickly —
forces this module to be the *only* code path operators touch when adding a
site. Site config schema lives in ~/.betty/sites/_README.md.

Loading is cached by site_id since configs are immutable for the life of the
MCP server subprocess (a config change requires a Hermes restart).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Operators can override via BETTY_SITES_DIR env var (set in
# ~/.hermes/config.yaml mcp_servers.betty.env). Default keeps configs out of
# the betty-claw repo since they include local-machine paths.
def _default_sites_dir() -> Path:
    override = os.environ.get("BETTY_SITES_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.betty/sites").expanduser()


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SitePaths:
    """Filesystem roots for one site.

    - astro    : Astro project source. Write-allowed root for content writes
                 and the git push origin.
    - docs     : Drive-synced site metadata (BRIEF, voice doc, UI notes).
                 Read only — Betty never writes here.
    - research : Local source-data tree (research dossiers). Read only.
    """
    astro: Path
    docs: Path
    research: Path


@dataclass(frozen=True)
class SiteEmdash:
    """EmDash CMS config for one site.

    The token itself is read from the env var named in `token_env` so secrets
    stay out of the YAML on disk. Calling `.token` raises if the env var is
    not set — this surfaces missing-credential failures at the first tool call
    rather than silently 401'ing into EmDash.
    """
    mcp_url: str
    token_env: str

    @property
    def token(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise ValueError(
                f"EmDash token env var {self.token_env!r} is not set in the "
                f"MCP server process. Add it to the env block under this "
                f"site's entry in ~/.hermes/config.yaml mcp_servers.betty.env, "
                f"or to ~/.hermes/.env if Hermes loads dotenv at startup."
            )
        return token


@dataclass(frozen=True)
class SiteGit:
    """Git policy for one site.

    - protected_branches : branches the git_push tool refuses to write to.
    - working_branch     : the only branch git_push will target.
    - deploy_command     : optional shell string Betty can invoke after a
                           successful push (Phase 2+; ignored in Phase 0).
    """
    repo_url: str
    protected_branches: tuple[str, ...]
    working_branch: str
    deploy_command: str | None = None


@dataclass(frozen=True)
class SiteCollection:
    """Schema for one EmDash collection.

    - fields   : field_name -> type ('text' | 'number' | 'boolean' | 'datetime')
    - required : tuple of field names that must be present in a content draft
                 for emdash_create_content_draft to accept it.
    """
    slug: str
    fields: dict[str, str]
    required: tuple[str, ...]


@dataclass(frozen=True)
class SiteParser:
    """Site-specific parser config.

    - target_collection : which EmDash collection this parser's output feeds.
    - fixed_fields      : values that flow into every parse result regardless
                          of source data. Used to enforce site invariants like
                          provider='airbnb' or is_advertised=0.
    """
    enabled: bool
    target_collection: str | None = None
    fixed_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteConfig:
    """Validated site configuration."""
    id: str
    domain: str
    status: str
    paths: SitePaths
    emdash: SiteEmdash
    git: SiteGit
    collections: dict[str, SiteCollection]
    hard_rules: tuple[str, ...]
    voice_doc_path: str
    parsers: dict[str, SiteParser]

    # ---- derived helpers -------------------------------------------------

    @property
    def voice_doc_full_path(self) -> Path:
        """Absolute path to the voice calibration doc on disk."""
        return self.paths.docs / self.voice_doc_path

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Path roots allowed for read operations on this site.

        Used by tools (file reads, dossier parsing) to validate that input
        paths sit under a site-blessed root.
        """
        return (self.paths.astro, self.paths.docs, self.paths.research)

    @property
    def write_roots(self) -> tuple[Path, ...]:
        """Path roots allowed for write operations on this site.

        Tighter than read_roots: only the Astro project is writable. docs/
        is operator-curated; research/ is read-only source data.
        """
        return (self.paths.astro,)

    def parser(self, name: str) -> SiteParser:
        """Return the parser config for `name`, raising if not configured.

        Tools that need parser config (e.g., parse_airbnb_dossier needs
        airbnb_dossier) call this rather than dict-accessing parsers directly,
        so the failure mode for "this site doesn't support that parser" is a
        single clear ValueError instead of a KeyError surfaced through MCP.
        """
        if name not in self.parsers:
            raise ValueError(
                f"Site {self.id!r} has no parser config for {name!r}. "
                f"Available parsers: {sorted(self.parsers.keys())}. "
                f"Add a `parsers.{name}` block to "
                f"~/.betty/sites/{self.id}.yaml or use a different site."
            )
        parser = self.parsers[name]
        if not parser.enabled:
            raise ValueError(
                f"Site {self.id!r} has parser {name!r} explicitly disabled "
                f"(parsers.{name}.enabled: false). Cannot invoke."
            )
        return parser


# ---------------------------------------------------------------------------
# YAML → dataclass parsing
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"in_progress", "live", "archived"}
_VALID_FIELD_TYPES = {"text", "number", "boolean", "datetime"}


def _require_keys(raw: dict, required: set[str], context: str) -> None:
    """Raise a clear error if any required keys are missing from `raw`."""
    missing = sorted(required - set(raw.keys()))
    if missing:
        raise ValueError(
            f"Site config {context}: missing required keys {missing}. "
            f"See ~/.betty/sites/_README.md for the schema."
        )


def _parse_paths(raw: dict, context: str) -> SitePaths:
    _require_keys(raw, {"astro", "docs", "research"}, context)
    return SitePaths(
        astro=Path(raw["astro"]).expanduser(),
        docs=Path(raw["docs"]).expanduser(),
        research=Path(raw["research"]).expanduser(),
    )


def _parse_emdash(raw: dict, context: str) -> SiteEmdash:
    _require_keys(raw, {"mcp_url", "token_env"}, context)
    return SiteEmdash(
        mcp_url=str(raw["mcp_url"]),
        token_env=str(raw["token_env"]),
    )


def _parse_git(raw: dict, context: str) -> SiteGit:
    _require_keys(raw, {"repo_url", "protected_branches", "working_branch"}, context)
    protected = raw["protected_branches"]
    if not isinstance(protected, list) or not all(isinstance(b, str) for b in protected):
        raise ValueError(
            f"Site config {context}: `git.protected_branches` must be a "
            f"list of strings, got {protected!r}"
        )
    return SiteGit(
        repo_url=str(raw["repo_url"]),
        protected_branches=tuple(protected),
        working_branch=str(raw["working_branch"]),
        deploy_command=(str(raw["deploy_command"])
                        if raw.get("deploy_command") else None),
    )


def _parse_collection(slug: str, raw: dict, site_context: str) -> SiteCollection:
    context = f"{site_context}.collections.{slug}"
    _require_keys(raw, {"fields", "required"}, context)
    fields = raw["fields"]
    if not isinstance(fields, dict):
        raise ValueError(f"{context}.fields must be a dict")
    for fname, ftype in fields.items():
        if ftype not in _VALID_FIELD_TYPES:
            raise ValueError(
                f"{context}.fields.{fname}: type {ftype!r} not in "
                f"{sorted(_VALID_FIELD_TYPES)}"
            )
    required = raw["required"]
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise ValueError(f"{context}.required must be a list of strings")
    # Every `required` field must exist in `fields`.
    for r in required:
        if r not in fields:
            raise ValueError(
                f"{context}: required field {r!r} not declared in fields"
            )
    return SiteCollection(
        slug=slug,
        fields=dict(fields),
        required=tuple(required),
    )


def _parse_parser(name: str, raw: dict, site_context: str) -> SiteParser:
    context = f"{site_context}.parsers.{name}"
    _require_keys(raw, {"enabled"}, context)
    return SiteParser(
        enabled=bool(raw["enabled"]),
        target_collection=(str(raw["target_collection"])
                           if raw.get("target_collection") else None),
        fixed_fields=dict(raw.get("fixed_fields") or {}),
    )


def _parse_site_config(raw: dict, source_path: Path) -> SiteConfig:
    """Validate and convert a raw YAML dict into a SiteConfig dataclass.

    Raises ValueError with a path-prefixed message on any validation
    failure so operators can pinpoint the broken field in the YAML.
    """
    context = source_path.name

    _require_keys(
        raw,
        {"id", "domain", "status", "paths", "emdash", "git",
         "collections", "hard_rules", "voice_doc_path", "parsers"},
        context,
    )

    site_id = str(raw["id"])
    # The filename must match the id so `list_sites()` and tool routing stay
    # consistent. Catch the mismatch at load time rather than letting it
    # confuse runtime tool dispatch.
    expected_stem = source_path.stem
    if site_id != expected_stem:
        raise ValueError(
            f"{context}: id={site_id!r} does not match filename stem "
            f"{expected_stem!r}. Rename the file or fix the id field."
        )

    status = str(raw["status"])
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"{context}: status={status!r} not in {sorted(_VALID_STATUSES)}"
        )

    paths = _parse_paths(raw["paths"], f"{context}.paths")
    emdash = _parse_emdash(raw["emdash"], f"{context}.emdash")
    git = _parse_git(raw["git"], f"{context}.git")

    collections_raw = raw["collections"]
    if not isinstance(collections_raw, dict):
        raise ValueError(f"{context}.collections must be a dict")
    collections = {
        slug: _parse_collection(slug, body, context)
        for slug, body in collections_raw.items()
    }

    hard_rules_raw = raw["hard_rules"]
    if not isinstance(hard_rules_raw, list) or not all(
        isinstance(r, str) for r in hard_rules_raw
    ):
        raise ValueError(f"{context}.hard_rules must be a list of strings")

    parsers_raw = raw["parsers"]
    if not isinstance(parsers_raw, dict):
        raise ValueError(f"{context}.parsers must be a dict")
    parsers = {
        name: _parse_parser(name, body, context)
        for name, body in parsers_raw.items()
    }

    return SiteConfig(
        id=site_id,
        domain=str(raw["domain"]),
        status=status,
        paths=paths,
        emdash=emdash,
        git=git,
        collections=collections,
        hard_rules=tuple(hard_rules_raw),
        voice_doc_path=str(raw["voice_doc_path"]),
        parsers=parsers,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@lru_cache(maxsize=32)
def load_site_config(site_id: str, sites_dir: Path | None = None) -> SiteConfig:
    """Load and validate a site config by ID. Cached for the process lifetime.

    Cache invalidation is intentional: site configs are immutable for the
    duration of the MCP server subprocess. Editing a config requires a
    Hermes restart, which respawns the server and clears the cache.

    Raises ValueError on any of: site not found, malformed YAML, missing
    required fields, type mismatches.
    """
    sites_dir = sites_dir or _default_sites_dir()
    config_path = sites_dir / f"{site_id}.yaml"
    if not config_path.exists():
        available = list_available_sites(sites_dir)
        raise ValueError(
            f"Site config not found: {config_path}. "
            f"Available sites: {available}. "
            f"BETTY_SITES_DIR={sites_dir}"
        )

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(
            f"Site config {config_path} is not valid YAML: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"Site config {config_path}: top level must be a mapping, "
            f"got {type(raw).__name__}"
        )

    return _parse_site_config(raw, config_path)


def list_available_sites(sites_dir: Path | None = None) -> list[str]:
    """Return the list of site IDs available in the sites directory.

    Files starting with underscore (_README.md, _template.yaml) are filtered
    out so internal docs don't show up as fake sites.

    Returns a sorted list for deterministic output to Hermes.
    """
    sites_dir = sites_dir or _default_sites_dir()
    if not sites_dir.exists():
        return []
    return sorted(
        p.stem for p in sites_dir.glob("*.yaml")
        if not p.stem.startswith("_")
    )


def site_summary(site: SiteConfig) -> dict[str, Any]:
    """Return a JSON-safe summary of a SiteConfig for the list_sites tool.

    Strips paths and credentials; surfaces only what Hermes needs to decide
    which site to operate on. Hermes can call detailed read tools to learn
    more about any individual site.
    """
    return {
        "id": site.id,
        "domain": site.domain,
        "status": site.status,
        "collections": sorted(site.collections.keys()),
        "parsers_available": sorted(
            name for name, p in site.parsers.items() if p.enabled
        ),
        "hard_rule_count": len(site.hard_rules),
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Self-test the site config loader against a synthetic config.

    Verifies: schema validation passes, missing keys raise, status enum
    enforced, id/filename mismatch caught, malformed YAML surfaces clearly,
    list_available_sites filters underscore-prefixed files.
    """
    import shutil
    import tempfile

    print("site_config.py self-test\n")

    # Use a tempdir so we don't depend on ~/.betty/sites/ on this machine.
    with tempfile.TemporaryDirectory() as td:
        sites_dir = Path(td)

        # Clear the cache between test runs since lru_cache persists across calls.
        load_site_config.cache_clear()

        # ---- valid config -------------------------------------------------
        valid_yaml = """
id: sampletown
domain: sampletown.example
status: in_progress
paths:
  astro: /tmp/sampletown/astro
  docs: /tmp/sampletown/docs
  research: /tmp/sampletown/research
emdash:
  mcp_url: https://sampletown.example/_emdash/api/mcp
  token_env: SAMPLETOWN_EMDASH_TOKEN
git:
  repo_url: github.com:test/sampletown.git
  protected_branches: [main]
  working_branch: dev
  deploy_command: "pnpm deploy"
collections:
  stays:
    required: [title]
    fields:
      title: text
      capacity: number
hard_rules:
  - "Never reveal internal flags."
voice_doc_path: voice.md
parsers:
  airbnb_dossier:
    enabled: true
    target_collection: stays
    fixed_fields:
      provider: airbnb
"""
        (sites_dir / "sampletown.yaml").write_text(valid_yaml)
        config = load_site_config("sampletown", sites_dir=sites_dir)
        assert config.id == "sampletown"
        assert config.domain == "sampletown.example"
        assert config.status == "in_progress"
        assert config.paths.astro == Path("/tmp/sampletown/astro")
        assert config.git.protected_branches == ("main",)
        assert config.git.working_branch == "dev"
        assert config.git.deploy_command == "pnpm deploy"
        assert "stays" in config.collections
        assert config.collections["stays"].required == ("title",)
        assert config.parser("airbnb_dossier").fixed_fields == {"provider": "airbnb"}
        assert config.read_roots == (
            Path("/tmp/sampletown/astro"),
            Path("/tmp/sampletown/docs"),
            Path("/tmp/sampletown/research"),
        )
        assert config.write_roots == (Path("/tmp/sampletown/astro"),)
        print("  [ok] valid config parses end-to-end")

        # ---- list_available_sites filters underscores ---------------------
        (sites_dir / "_template.yaml").write_text("noop")
        (sites_dir / "_README.md").write_text("noop")
        (sites_dir / "lingerieshoppe.yaml").write_text(
            valid_yaml.replace("sampletown", "lingerieshoppe")
        )
        load_site_config.cache_clear()
        available = list_available_sites(sites_dir)
        assert available == ["lingerieshoppe", "sampletown"], available
        print("  [ok] list_available_sites filters underscore files")

        # ---- missing keys ------------------------------------------------
        bad_path = sites_dir / "bad_missing.yaml"
        bad_path.write_text("id: bad_missing\ndomain: example.com\n")
        load_site_config.cache_clear()
        try:
            load_site_config("bad_missing", sites_dir=sites_dir)
        except ValueError as e:
            assert "missing required keys" in str(e)
            print("  [ok] missing required keys caught")
        else:
            raise AssertionError("missing-keys config should error")

        # ---- id / filename mismatch -------------------------------------
        mismatch_yaml = valid_yaml.replace("id: sampletown", "id: mismatch")
        (sites_dir / "wrongstem.yaml").write_text(mismatch_yaml)
        load_site_config.cache_clear()
        try:
            load_site_config("wrongstem", sites_dir=sites_dir)
        except ValueError as e:
            assert "does not match filename stem" in str(e)
            print("  [ok] id/filename mismatch caught")
        else:
            raise AssertionError("id/filename mismatch should error")

        # ---- bad status enum --------------------------------------------
        bad_status_yaml = valid_yaml.replace("status: in_progress", "status: foo")
        (sites_dir / "badstatus.yaml").write_text(
            bad_status_yaml.replace("sampletown", "badstatus")
        )
        load_site_config.cache_clear()
        try:
            load_site_config("badstatus", sites_dir=sites_dir)
        except ValueError as e:
            assert "status=" in str(e)
            print("  [ok] invalid status enum caught")
        else:
            raise AssertionError("bad status should error")

        # ---- bad field type ---------------------------------------------
        bad_type_yaml = valid_yaml.replace("capacity: number", "capacity: blob")
        (sites_dir / "badtype.yaml").write_text(
            bad_type_yaml.replace("sampletown", "badtype")
        )
        load_site_config.cache_clear()
        try:
            load_site_config("badtype", sites_dir=sites_dir)
        except ValueError as e:
            assert "type 'blob'" in str(e)
            print("  [ok] invalid field type caught")
        else:
            raise AssertionError("bad field type should error")

        # ---- site not found ---------------------------------------------
        load_site_config.cache_clear()
        try:
            load_site_config("nope", sites_dir=sites_dir)
        except ValueError as e:
            assert "not found" in str(e)
            print("  [ok] missing site surfaces clearly")
        else:
            raise AssertionError("missing site should error")

        # ---- malformed YAML ---------------------------------------------
        (sites_dir / "garbage.yaml").write_text(
            "id: garbage\ndomain: x\n  bad: indent"
        )
        load_site_config.cache_clear()
        try:
            load_site_config("garbage", sites_dir=sites_dir)
        except ValueError as e:
            assert "not valid YAML" in str(e) or "missing required keys" in str(e)
            print("  [ok] malformed YAML surfaces clearly")
        else:
            raise AssertionError("garbage YAML should error")

        # ---- parser() helper -------------------------------------------
        load_site_config.cache_clear()
        config = load_site_config("sampletown", sites_dir=sites_dir)
        try:
            config.parser("nonexistent_parser")
        except ValueError as e:
            assert "no parser config" in str(e)
            print("  [ok] parser() helper rejects unknown parsers")
        else:
            raise AssertionError("unknown parser should error")

        # ---- token env var missing -------------------------------------
        os.environ.pop("SAMPLETOWN_EMDASH_TOKEN", None)
        try:
            _ = config.emdash.token
        except ValueError as e:
            assert "is not set" in str(e)
            print("  [ok] missing EmDash token env var caught at access time")
        else:
            raise AssertionError("missing token should error")

        # ---- site_summary ----------------------------------------------
        summary = site_summary(config)
        assert summary["id"] == "sampletown"
        assert summary["status"] == "in_progress"
        assert summary["parsers_available"] == ["airbnb_dossier"]
        assert summary["collections"] == ["stays"]
        assert "token_env" not in summary  # credentials never surface
        assert "paths" not in summary       # paths never surface
        print("  [ok] site_summary strips paths and credentials")

    print("\nsite_config.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
