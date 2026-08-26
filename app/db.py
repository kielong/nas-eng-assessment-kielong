from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Select, Text, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
    cached_at: Mapped[str] = mapped_column(Text, nullable=False)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def database_url(path: str) -> str:
    return f"sqlite+aiosqlite:///{Path(path).resolve()}"


def create_engine_and_sessionmaker(path: str):
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
) -> None:
    now = now or datetime.now(timezone.utc)
    rows = await list_all(session)
    for row in rows:
        if is_expired(row.cached_at, ttl_seconds, now=now):
            await session.delete(row)
    await session.commit()


async def list_all(session: AsyncSession) -> list[VinCache]:
    stmt: Select[tuple[VinCache]] = select(VinCache).order_by(VinCache.vin)
    result = await session.execute(stmt)
    return list(result.scalars().all())
