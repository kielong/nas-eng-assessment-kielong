import io
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, vpic
from app.schemas import LookupResponse, RemoveResponse, VinRequest
from app.settings import Settings

router = APIRouter()

# Passed explicitly to pa.Table.from_pylist below rather than inferred: an
# empty list with no schema produces a zero-column table, which would break
# an export of an empty cache (it must still have these five typed columns).
EXPORT_SCHEMA = pa.schema(
    [
        ("vin", pa.string()),
        ("make", pa.string()),
        ("model", pa.string()),
        ("model_year", pa.string()),
        ("body_class", pa.string()),
    ]
)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def _export_filename(now: datetime | None = None) -> str:
    # Colon-free, sortable UTC timestamp: repeat exports otherwise all
    # suggest "vin_cache.parquet" and silently overwrite each other on disk
    # (curl -O -J always overwrites; some browsers do too). Not colon-safe
    # ISO-8601 (":" is illegal in a Windows filename) -- deliberately a
    # separate, filename-specific format from db.utcnow_iso().
    now = now or datetime.now(timezone.utc)
    return f"vin_cache_{now.strftime('%Y%m%dT%H%M%SZ')}.parquet"


def _lookup_response(row: db.VinCache, *, cached: bool) -> LookupResponse:
    return LookupResponse(
        vin=row.vin,
        make=row.make,
        model=row.model,
        model_year=row.model_year,
        body_class=row.body_class,
        cached=cached,
    )


@router.post("/lookup", response_model=LookupResponse)
async def lookup(
    body: VinRequest,
    session: AsyncSession = Depends(get_session),
    client: httpx.AsyncClient = Depends(get_http_client),
    settings: Settings = Depends(get_app_settings),
) -> LookupResponse:
    row = await db.get_live(session, body.vin, settings.cache_ttl_seconds)
    if row is not None:
        return _lookup_response(row, cached=True)

    try:
        decoded = await vpic.decode_vin(client, settings.vpic_base_url, body.vin)
    except vpic.VpicError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    row = await db.upsert(
        session,
        vin=body.vin,
        make=decoded.make,
        model=decoded.model,
        model_year=decoded.model_year,
        body_class=decoded.body_class,
    )
    return _lookup_response(row, cached=False)


@router.post("/remove", response_model=RemoveResponse)
async def remove(
    body: VinRequest,
    session: AsyncSession = Depends(get_session),
) -> RemoveResponse:
    deleted = await db.delete_vin(session, body.vin)
    return RemoveResponse(vin=body.vin, deleted=deleted)


@router.get("/export")
async def export_cache(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    # Purges expired rows, but deliberately does not enforce CACHE_MAX_ROWS --
    # that's the periodic background sweep's job (app/main.py). An export can
    # therefore show more rows than the cap between sweep ticks; see
    # NOTES.md's tradeoffs table ("cap and TTL enforced on a timer").
    await db.purge_expired(session, settings.cache_ttl_seconds)
    rows = await db.list_all(session)
    table = pa.Table.from_pylist(
        [
            {
                "vin": row.vin,
                "make": row.make,
                "model": row.model,
                "model_year": row.model_year,
                "body_class": row.body_class,
            }
            for row in rows
        ],
        schema=EXPORT_SCHEMA,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": f'attachment; filename="{_export_filename()}"'},
    )
