from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Select, Text, delete, event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class VinCache(Base):
    __tablename__ = "vin_cache"

    vin: Mapped[str] = mapped_column(Text, primary_key=True)
    make: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    model_year: Mapped[str] = mapped_column(Text, nullable=False)
    body_class: Mapped[str] = mapped_column(Text, nullable=False)
    # Indexed: purge_expired and enforce_size_cap both filter/sort by this on
    # every maintenance sweep, and it's the only non-PK column either queries.
    cached_at: Mapped[str] = mapped_column(Text, nullable=False, index=True)


def _format_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def utcnow_iso() -> str:
    return _format_iso(datetime.now(timezone.utc))


def database_url(path: str) -> str:
    return f"sqlite+aiosqlite:///{Path(path).resolve()}"


def create_engine_and_sessionmaker(
    path: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url(path))

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        # Default rollback-journal SQLite raises "database is locked" under
        # concurrent writers even within one process (e.g. two overlapping
        # requests). WAL + a busy timeout make that a wait, not an error.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


def _parse_cached_at(value: str) -> datetime:
    cached_at = datetime.fromisoformat(value)
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    return cached_at


def is_expired(cached_at: str, ttl_seconds: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now - _parse_cached_at(cached_at) >= timedelta(seconds=ttl_seconds)


async def get_live(session: AsyncSession, vin: str, ttl_seconds: int) -> VinCache | None:
    row = await session.get(VinCache, vin)
    if row is None:
        return None
    if is_expired(row.cached_at, ttl_seconds):
        # Side effect despite the name: an expired row is deleted here, not
        # just ignored, so a miss always means "safe to write a fresh row."
        await session.delete(row)
        await session.commit()
        return None
    return row


async def upsert(
    session: AsyncSession,
    *,
    vin: str,
    make: str,
    model: str,
    model_year: str,
    body_class: str,
) -> VinCache:
    row = VinCache(
        vin=vin,
        make=make,
        model=model,
        model_year=model_year,
        body_class=body_class,
        cached_at=utcnow_iso(),
    )
    row = await session.merge(row)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_vin(session: AsyncSession, vin: str) -> bool:
    row = await session.get(VinCache, vin)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def purge_expired(
    session: AsyncSession, ttl_seconds: int, now: datetime | None = None
) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = _format_iso(now - timedelta(seconds=ttl_seconds))
    # cached_at is always this module's own aware, UTC, second-precision
    # ISO-8601 output (utcnow_iso/_format_iso), which sorts and compares
    # correctly as a plain string -- enforce_size_cap already relies on the
    # same property for its ORDER BY. That lets this run as one DELETE
    # instead of fetching every row into Python to filter and delete
    # one at a time, which matters once the table is at production scale.
    result = await session.execute(delete(VinCache).where(VinCache.cached_at <= cutoff))
    await session.commit()
    return result.rowcount or 0


async def enforce_size_cap(session: AsyncSession, max_rows: int) -> int:
    """Evict the oldest rows (by cached_at) once the table exceeds max_rows.

    TTL alone bounds staleness, not row count: a burst of lookups for
    distinct VINs within one TTL window still grows the table without a
    ceiling. This is the size-side complement to purge_expired.
    """
    total = await session.scalar(select(func.count()).select_from(VinCache))
    overflow = (total or 0) - max_rows
    if overflow <= 0:
        return 0
    oldest_vins = select(VinCache.vin).order_by(VinCache.cached_at.asc()).limit(overflow)
    result = await session.execute(delete(VinCache).where(VinCache.vin.in_(oldest_vins)))
    await session.commit()
    return result.rowcount or 0


async def list_all(session: AsyncSession) -> list[VinCache]:
    stmt: Select[tuple[VinCache]] = select(VinCache).order_by(VinCache.vin)
    result = await session.execute(stmt)
    return list(result.scalars().all())
