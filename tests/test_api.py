import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pyarrow.parquet as pq
import pytest
import respx
from fastapi.testclient import TestClient

from app.db import (
    Base,
    VinCache,
    create_engine_and_sessionmaker,
    enforce_size_cap,
    is_expired,
    list_all,
    purge_expired,
)
from app.main import create_app

SAMPLE_VIN = "1HGCM82633A004352"
SAMPLE_VIN_LOWER = "1hgcm82633a004352"
OTHER_VIN = "5YJ3E1EA6PF384836"
TTL_SECONDS = 7 * 24 * 60 * 60
VPIC_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues"

HONDA = {
    "Make": "HONDA",
    "Model": "Accord",
    "ModelYear": "2003",
    "BodyClass": "Sedan/Saloon",
}
TESLA = {
    "Make": "TESLA",
    "Model": "Model 3",
    "ModelYear": "2023",
    "BodyClass": "Sedan/Saloon",
}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "cache.db"


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("CACHE_TTL_SECONDS", str(TTL_SECONDS))
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


def mock_decode(vin: str, fields: dict, status_code: int = 200) -> None:
    respx.get(f"{VPIC_BASE}/{vin}").mock(
        return_value=httpx.Response(status_code, json={"Results": [fields]})
    )


def seed_row(db_file, vin: str, cached_at: str, fields: dict | None = None) -> None:
    fields = fields or HONDA
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        INSERT INTO vin_cache (vin, make, model, model_year, body_class, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            vin,
            fields["Make"],
            fields["Model"],
            fields["ModelYear"],
            fields["BodyClass"],
            cached_at,
        ),
    )
    conn.commit()
    conn.close()


def vins_in_db(db_file) -> set[str]:
    conn = sqlite3.connect(db_file)
    rows = {row[0] for row in conn.execute("SELECT vin FROM vin_cache")}
    conn.close()
    return rows


def lookup_payload(vin: str = SAMPLE_VIN) -> dict:
    return {
        "vin": vin,
        "make": "HONDA",
        "model": "Accord",
        "model_year": "2003",
        "body_class": "Sedan/Saloon",
    }


class TestValidation:
    def test_rejects_short_vin(self, client):
        response = client.post("/lookup", json={"vin": "SHORT"})
        assert response.status_code == 422

    def test_rejects_long_vin(self, client):
        response = client.post("/lookup", json={"vin": SAMPLE_VIN + "X"})
        assert response.status_code == 422

    def test_rejects_non_alphanumeric_vin(self, client):
        response = client.post("/remove", json={"vin": "1HGCM82633A00435!"})
        assert response.status_code == 422


class TestLookup:
    @respx.mock
    def test_cache_miss_then_hit(self, client):
        mock_decode(SAMPLE_VIN, HONDA)

        miss = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert miss.status_code == 200
        assert miss.json() == {**lookup_payload(), "cached": False}
        assert respx.calls.call_count == 1

        hit = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert hit.status_code == 200
        assert hit.json() == {**lookup_payload(), "cached": True}
        assert respx.calls.call_count == 1

    @respx.mock
    def test_normalizes_vin_to_uppercase(self, client):
        mock_decode(SAMPLE_VIN, HONDA)

        response = client.post("/lookup", json={"vin": SAMPLE_VIN_LOWER})
        assert response.status_code == 200
        assert response.json()["vin"] == SAMPLE_VIN

    @respx.mock
    def test_expired_row_is_a_miss(self, client, db_path):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        seed_row(db_path, SAMPLE_VIN, old)
        mock_decode(SAMPLE_VIN, HONDA)

        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 200
        assert response.json()["cached"] is False
        assert respx.calls.call_count == 1

    @respx.mock
    def test_expired_row_then_vpic_failure_leaves_vin_uncached(self, client, db_path):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        seed_row(db_path, SAMPLE_VIN, old)
        respx.get(f"{VPIC_BASE}/{SAMPLE_VIN}").mock(return_value=httpx.Response(500, text="nope"))

        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()

    @respx.mock
    def test_vpic_http_error_returns_502(self, client, db_path):
        respx.get(f"{VPIC_BASE}/{SAMPLE_VIN}").mock(return_value=httpx.Response(500, text="nope"))
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()

    @respx.mock
    def test_vpic_empty_results_returns_502(self, client, db_path):
        respx.get(f"{VPIC_BASE}/{SAMPLE_VIN}").mock(
            return_value=httpx.Response(200, json={"Results": []})
        )
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()

    @respx.mock
    def test_vpic_invalid_json_returns_502(self, client, db_path):
        respx.get(f"{VPIC_BASE}/{SAMPLE_VIN}").mock(return_value=httpx.Response(200, text="not-json"))
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()

    @respx.mock
    def test_vpic_timeout_returns_502(self, client, db_path):
        respx.get(f"{VPIC_BASE}/{SAMPLE_VIN}").mock(side_effect=httpx.TimeoutException("timed out"))
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()

    @respx.mock
    def test_partial_missing_vpic_fields_are_stored_as_empty_strings(self, client, db_path):
        mock_decode(
            SAMPLE_VIN,
            {"Make": "HONDA", "Model": "Accord", "ModelYear": "2003", "BodyClass": None},
        )
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 200
        assert response.json() == {
            "vin": SAMPLE_VIN,
            "make": "HONDA",
            "model": "Accord",
            "model_year": "2003",
            "body_class": "",
            "cached": False,
        }
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT make, model, model_year, body_class FROM vin_cache WHERE vin = ?",
            (SAMPLE_VIN,),
        ).fetchone()
        conn.close()
        assert row == ("HONDA", "Accord", "2003", "")

    @respx.mock
    def test_all_fields_empty_is_treated_as_undecodable(self, client, db_path):
        # This is real vPIC behavior for a well-formed-but-garbage VIN: HTTP 200,
        # Results[0] present, every target field blank. Confirmed against the
        # live API (AAAAAAAAAAAAAAAAA) rather than assumed.
        mock_decode(
            SAMPLE_VIN,
            {"Make": None, "Model": None, "ModelYear": None, "BodyClass": None},
        )
        response = client.post("/lookup", json={"vin": SAMPLE_VIN})
        assert response.status_code == 502
        assert vins_in_db(db_path) == set()


class TestRemove:
    @respx.mock
    def test_remove_hit_and_miss(self, client):
        mock_decode(SAMPLE_VIN, HONDA)
        client.post("/lookup", json={"vin": SAMPLE_VIN})

        deleted = client.post("/remove", json={"vin": SAMPLE_VIN})
        assert deleted.status_code == 200
        assert deleted.json() == {"vin": SAMPLE_VIN, "deleted": True}

        missing = client.post("/remove", json={"vin": SAMPLE_VIN})
        assert missing.status_code == 200
        assert missing.json() == {"vin": SAMPLE_VIN, "deleted": False}


class TestExport:
    def test_empty_cache_returns_empty_parquet(self, client):
        response = client.get("/export")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apache.parquet"
        assert "vin_cache.parquet" in response.headers["content-disposition"]

        import io

        table = pq.read_table(io.BytesIO(response.content))
        assert table.column_names == ["vin", "make", "model", "model_year", "body_class"]
        assert table.num_rows == 0

    @respx.mock
    def test_export_contains_live_rows_and_drops_expired(self, client, db_path):
        mock_decode(SAMPLE_VIN, HONDA)
        mock_decode(OTHER_VIN, TESLA)
        client.post("/lookup", json={"vin": SAMPLE_VIN})
        client.post("/lookup", json={"vin": OTHER_VIN})

        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        seed_row(db_path, "1FTFW1ET9DFC10312", old)

        import io

        response = client.get("/export")
        table = pq.read_table(io.BytesIO(response.content))
        vins = set(table.column("vin").to_pylist())
        assert vins == {SAMPLE_VIN, OTHER_VIN}
        assert vins_in_db(db_path) == {SAMPLE_VIN, OTHER_VIN}

    def test_export_drops_row_at_exact_ttl(self, client, db_path):
        at_ttl = (datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS)).replace(
            microsecond=0
        ).isoformat()
        seed_row(db_path, SAMPLE_VIN, at_ttl)

        import io

        response = client.get("/export")
        table = pq.read_table(io.BytesIO(response.content))
        assert table.column("vin").to_pylist() == []
        assert vins_in_db(db_path) == set()


class TestDemoUi:
    def test_serves_demo_page_with_sample_vins(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "1HGCM82633A004352" in response.text
        assert "5YJ3E1EA6PF384836" in response.text


class TestSqliteConcurrencySettings:
    def test_wal_mode_and_busy_timeout_are_set_on_connect(self, client, db_path):
        conn = sqlite3.connect(db_path)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.close()
        assert journal_mode.lower() == "wal"
        assert busy_timeout == 5000


class TestSizeCap:
    @pytest.mark.asyncio
    async def test_enforce_size_cap_evicts_oldest_rows_by_cached_at(self, tmp_path):
        engine, session_factory = create_engine_and_sessionmaker(str(tmp_path / "cache.db"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        base = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        vins = ["1HGCM82633A004352", "5YJ3E1EA6PF384836", "1FTFW1ET9DFC10312"]
        async with session_factory() as session:
            for i, vin in enumerate(vins):
                # oldest first: vins[0] is the oldest, vins[-1] the newest
                session.add(
                    VinCache(
                        vin=vin,
                        make="HONDA",
                        model="Accord",
                        model_year="2003",
                        body_class="Sedan/Saloon",
                        cached_at=(base + timedelta(minutes=i)).isoformat(),
                    )
                )
            await session.commit()

            evicted = await enforce_size_cap(session, max_rows=2)
            rows = await list_all(session)

        await engine.dispose()
        assert evicted == 1
        assert {row.vin for row in rows} == {vins[1], vins[2]}

    @pytest.mark.asyncio
    async def test_enforce_size_cap_is_a_noop_under_the_cap(self, tmp_path):
        engine, session_factory = create_engine_and_sessionmaker(str(tmp_path / "cache.db"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add(
                VinCache(
                    vin=SAMPLE_VIN,
                    make="HONDA",
                    model="Accord",
                    model_year="2003",
                    body_class="Sedan/Saloon",
                    cached_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            await session.commit()
            evicted = await enforce_size_cap(session, max_rows=10)

        await engine.dispose()
        assert evicted == 0


class TestBackgroundMaintenance:
    def test_periodic_sweep_purges_expired_and_enforces_size_cap(self, db_path, monkeypatch):
        # Seed data directly, before the app (and its background sweep) starts,
        # so the very first sweep tick has something to act on.
        monkeypatch.setenv("DATABASE_PATH", str(db_path))
        monkeypatch.setenv("CACHE_TTL_SECONDS", str(TTL_SECONDS))
        monkeypatch.setenv("CACHE_SWEEP_INTERVAL_SECONDS", "3600")
        monkeypatch.setenv("CACHE_MAX_ROWS", "2")

        # Table doesn't exist yet on a fresh db_path — the app normally creates it
        # in its lifespan. Create it directly here so we can seed before startup.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE vin_cache (
              vin TEXT PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL,
              model_year TEXT NOT NULL, body_class TEXT NOT NULL, cached_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        seed_row(db_path, SAMPLE_VIN, old)  # expired: should be purged

        base = datetime.now(timezone.utc)
        live_vins = ["1FTFW1ET9DFC10312", "1C4RJFBG2FC625797", "5FNRL6H79NB021411"]
        for i, vin in enumerate(live_vins):
            # oldest first: over the cap of 2, so live_vins[0] should be evicted
            seed_row(db_path, vin, (base - timedelta(minutes=3 - i)).isoformat())

        application = create_app()
        with TestClient(application):
            deadline = time.monotonic() + 2.0
            expected = set(live_vins[1:])
            while time.monotonic() < deadline:
                if vins_in_db(db_path) == expected:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"background sweep did not converge; vins={vins_in_db(db_path)}")


class TestTtlPredicate:
    def test_age_at_ttl_is_expired_age_under_ttl_is_live(self):
        now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        at_ttl = (now - timedelta(seconds=TTL_SECONDS)).isoformat()
        under_ttl = (now - timedelta(seconds=TTL_SECONDS - 1)).isoformat()
        assert is_expired(at_ttl, TTL_SECONDS, now=now) is True
        assert is_expired(under_ttl, TTL_SECONDS, now=now) is False

    @pytest.mark.asyncio
    async def test_purge_expired_uses_the_same_predicate(self, tmp_path):
        engine, session_factory = create_engine_and_sessionmaker(str(tmp_path / "cache.db"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        at_ttl = (now - timedelta(seconds=TTL_SECONDS)).isoformat()
        under_ttl = (now - timedelta(seconds=TTL_SECONDS - 1)).isoformat()
        async with session_factory() as session:
            session.add(
                VinCache(
                    vin=SAMPLE_VIN,
                    make="HONDA",
                    model="Accord",
                    model_year="2003",
                    body_class="Sedan/Saloon",
                    cached_at=at_ttl,
                )
            )
            session.add(
                VinCache(
                    vin=OTHER_VIN,
                    make="TESLA",
                    model="Model 3",
                    model_year="2023",
                    body_class="Sedan/Saloon",
                    cached_at=under_ttl,
                )
            )
            await session.commit()
            await purge_expired(session, TTL_SECONDS, now=now)
            rows = await list_all(session)

        await engine.dispose()
        assert {row.vin for row in rows} == {OTHER_VIN}
