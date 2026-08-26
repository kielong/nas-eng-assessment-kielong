from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import db
from app.routes import router
from app.settings import get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.http_client = http_client
        try:
            yield
        finally:
            await http_client.aclose()
            await engine.dispose()

    application = FastAPI(title="VIN Decoder", lifespan=lifespan)
    application.include_router(router)

    @application.get("/", include_in_schema=False)
    async def demo_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return application


app = create_app()
