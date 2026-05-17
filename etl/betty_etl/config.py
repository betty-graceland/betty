"""
Betty ETL configuration.

Loads Postgres credentials from /Users/betty/code/betty/docker/.env
(three .parent calls up from this file) and exposes typed config
objects for the chunker, embedder, and database layer.

Environment variables expected in docker/.env:
    POSTGRES_USER       (required)
    POSTGRES_PASSWORD   (required)
    POSTGRES_DB         (required)
    POSTGRES_HOST       (optional, defaults to localhost)
    POSTGRES_PORT       (optional, defaults to 5433)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Walk: config.py -> betty_etl/ -> etl/ -> betty/ (repo root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKER_ENV = PROJECT_ROOT / "docker" / ".env"
# test_data is at etl/test_data, sibling to the betty_etl/ package
TEST_DATA_DIR = Path(__file__).resolve().parent.parent / "test_data"

if not DOCKER_ENV.exists():
    raise FileNotFoundError(
        f"Expected Postgres credentials at {DOCKER_ENV} — "
        f"check that docker/.env exists at the repo root."
    )

load_dotenv(DOCKER_ENV)


@dataclass(frozen=True)
class ChunkConfig:
    chunk_size: int = 900
    chunk_overlap: int = 120


@dataclass(frozen=True)
class EmbedConfig:
    model_name: str = "nomic-ai/nomic-embed-text-v1.5"
    expected_dim: int = 768
    device: str = "mps"
    batch_size: int = 32
    max_seq_length: int = 8192  # Nomic supports long context
    normalize: bool = True
    document_prompt_name: str = "search_document"  # Nomic-specific


@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    pool_min_size: int = 2
    pool_max_size: int = 8
    pool_timeout: float = 30.0

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


def _load_db_config() -> DBConfig:
    """Build DBConfig from environment variables loaded from docker/.env."""
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise ValueError(
            f"Missing required env vars in {DOCKER_ENV}: {', '.join(missing)}"
        )

    return DBConfig(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5433")),
        database=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


CHUNK = ChunkConfig()
EMBED = EmbedConfig()
DB = _load_db_config()
