import os
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
DEFAULT_DATABASE_PATH = "data/cache.db"
DEFAULT_VPIC_BASE_URL = "https://vpic.nhtsa.dot.gov/api"
DEFAULT_VPIC_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Settings:
    cache_ttl_seconds: int
    database_path: str
    vpic_base_url: str
    vpic_timeout_seconds: float = DEFAULT_VPIC_TIMEOUT_SECONDS


def get_settings() -> Settings:
    return Settings(
        cache_ttl_seconds=int(os.environ.get("CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))),
        database_path=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        vpic_base_url=os.environ.get("VPIC_BASE_URL", DEFAULT_VPIC_BASE_URL).rstrip("/"),
        vpic_timeout_seconds=float(
            os.environ.get("VPIC_TIMEOUT_SECONDS", str(DEFAULT_VPIC_TIMEOUT_SECONDS))
        ),
    )
