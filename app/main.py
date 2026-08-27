import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import db
from app.routes import router
from app.settings import Settings, get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


async def _cache_maintenance_loop(session_factory, settings: Settings) -> None:
    """Purge expired rows and enforce the row cap on a timer.

    Runs once immediately (covering whatever accumulated since the last
    process lifetime), then every cache_sweep_interval_seconds. This is the
    active counterpart to /lookup's and /export's reactive cleanup: without
    it, a row for a VIN nobody looks up again just sits past its TTL, and
    TTL alone never caps absolute row count within one TTL window.
    """
    while True:
        try:
            async with session_factory() as session:
                purged = await db.purge_expired(session, settings.cache_ttl_seconds)
                evicted = await db.enforce_size_cap(session, settings.cache_max_rows)
            if purged or evicted:
                logger.info(
                    "cache maintenance: purged %d expired row(s), evicted %d over the %d-row cap",
                    purged,
                    evicted,
                    settings.cache_max_rows,
                )
        except Exception:
            logger.exception("cache maintenance sweep failed; will retry next interval")
        await asyncio.sleep(settings.cache_sweep_interval_seconds)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        db_path = Path(settings.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        engine, session_factory = db.create_engine_and_sessionmaker(str(db_path))
        async with engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all)

        http_client = httpx.AsyncClient(timeout=settings.vpic_timeout_seconds)
        maintenance_task = asyncio.create_task(_cache_maintenance_loop(session_factory, settings))
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.http_client = http_client
        try:
            yield
        finally:
            # CancelledError isn't an Exception subclass, so the loop's own
            # `except Exception` doesn't swallow it -- cancel() here always
            # produces a real CancelledError on the await below, expected
            # and discarded, not a sign the loop failed.
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
            await http_client.aclose()
            await engine.dispose()

    application = FastAPI(title="VIN Decoder", lifespan=lifespan)
    application.include_router(router)

    @application.get("/", include_in_schema=False)
    async def demo_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return application


app = create_app()
