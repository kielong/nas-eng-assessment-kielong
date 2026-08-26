import io
from collections.abc import AsyncIterator

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


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


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
    await db.purge_expired(session, settings.cache_ttl_seconds)
    rows = await db.list_all(session)
    table = pa.table(
        {
            "vin": pa.array([row.vin for row in rows], type=pa.string()),
            "make": pa.array([row.make for row in rows], type=pa.string()),
            "model": pa.array([row.model for row in rows], type=pa.string()),
            "model_year": pa.array([row.model_year for row in rows], type=pa.string()),
            "body_class": pa.array([row.body_class for row in rows], type=pa.string()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": 'attachment; filename="vin_cache.parquet"'},
    )
