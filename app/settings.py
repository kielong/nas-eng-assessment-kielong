import os
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
DEFAULT_DATABASE_PATH = "data/cache.db"
DEFAULT_VPIC_BASE_URL = "https://vpic.nhtsa.dot.gov/api"
DEFAULT_VPIC_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_SWEEP_INTERVAL_SECONDS = 60 * 60  # 1 hour
DEFAULT_CACHE_MAX_ROWS = 10_000


@dataclass(frozen=True)
class Settings:
    cache_ttl_seconds: int
    database_path: str
    vpic_base_url: str
    vpic_timeout_seconds: float = DEFAULT_VPIC_TIMEOUT_SECONDS
    cache_sweep_interval_seconds: int = DEFAULT_CACHE_SWEEP_INTERVAL_SECONDS
    cache_max_rows: int = DEFAULT_CACHE_MAX_ROWS


def _positive_int(name: str, raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")
    return value


def _positive_float(name: str, raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value}")
    return value


def get_settings() -> Settings:
    # Non-numeric garbage already fails fast via int()/float() below. These
    # helpers catch the other operator-error case: a value that parses fine
    # but is semantically nonsense -- e.g. CACHE_SWEEP_INTERVAL_SECONDS=0
    # would make the background maintenance loop busy-loop against the DB
    # instead of sleeping between ticks.
    return Settings(
        cache_ttl_seconds=_positive_int(
            "CACHE_TTL_SECONDS", os.environ.get("CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))
        ),
        database_path=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        vpic_base_url=os.environ.get("VPIC_BASE_URL", DEFAULT_VPIC_BASE_URL).rstrip("/"),
        vpic_timeout_seconds=_positive_float(
            "VPIC_TIMEOUT_SECONDS",
            os.environ.get("VPIC_TIMEOUT_SECONDS", str(DEFAULT_VPIC_TIMEOUT_SECONDS)),
        ),
        cache_sweep_interval_seconds=_positive_int(
            "CACHE_SWEEP_INTERVAL_SECONDS",
            os.environ.get(
                "CACHE_SWEEP_INTERVAL_SECONDS", str(DEFAULT_CACHE_SWEEP_INTERVAL_SECONDS)
            ),
        ),
        cache_max_rows=_positive_int(
            "CACHE_MAX_ROWS", os.environ.get("CACHE_MAX_ROWS", str(DEFAULT_CACHE_MAX_ROWS))
        ),
    )
