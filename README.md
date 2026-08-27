# VIN Decoder

A small [FastAPI](https://fastapi.tiangolo.com) service that decodes Vehicle Identification Numbers via the [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/) and caches results in SQLite.

The original coding challenge is in [ASSIGNMENT.md](ASSIGNMENT.md). Implementation phases are in [CHECKLIST.md](CHECKLIST.md). Design notes are in [NOTES.md](NOTES.md).

Three routes:

| Route | Method | Description |
|---|---|---|
| `/lookup` | POST | Return a cached decode, or fetch from vPIC, store, and return |
| `/remove` | POST | Delete a VIN from the cache |
| `/export` | GET | Download live cache rows as a parquet file |

## Running locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

For tests:

```bash
pip install -r requirements-dev.txt
pytest
```

If you use PyCharm, point the project interpreter at this repo's `.venv` (Settings → Python Interpreter → Add Interpreter → Existing → `.venv/bin/python`). A different venv will look empty until you run `pip install -r requirements.txt` in *that* environment.

The SQLite file is created at `data/cache.db`. Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_TTL_SECONDS` | `604800` (7 days) | How long a cached decode is considered live |
| `CACHE_SWEEP_INTERVAL_SECONDS` | `3600` (1 hour) | How often a background task purges expired rows and enforces the row cap |
| `CACHE_MAX_ROWS` | `10000` | Row cap enforced by the same background task; oldest rows evicted first |
| `DATABASE_PATH` | `data/cache.db` | SQLite file path |
| `VPIC_BASE_URL` | `https://vpic.nhtsa.dot.gov/api` | vPIC API root |
| `VPIC_TIMEOUT_SECONDS` | `10` | HTTP timeout for vPIC |

## Trying the sample VINs

Start the app, then open http://127.0.0.1:8000/. The page has a VIN field, the sample VINs below as clickable chips, and Lookup / Remove / Export. The first lookup of a VIN should show it was fetched from vPIC; the second lookup of the same VIN should show a cached result. Invalid VINs and vPIC failures show on the page as HTTP 422 / 502.

OpenAPI is still at http://127.0.0.1:8000/docs if you want the raw JSON.

From a terminal:

```bash
# miss, then hit
curl -s -X POST http://127.0.0.1:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'

curl -s -X POST http://127.0.0.1:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'

# other sample VINs
for vin in \
  5YJ3E1EA6PF384836 \
  1FTFW1ET9DFC10312 \
  1C4RJFBG2FC625797 \
  5FNRL6H79NB021411 \
  1HD1KBM15FB620271 \
  1XPWD40X1ED215307
do
  curl -s -X POST http://127.0.0.1:8000/lookup \
    -H 'Content-Type: application/json' \
    -d "{\"vin\": \"$vin\"}"
  echo
done

curl -s -O -J http://127.0.0.1:8000/export
```

## API

VIN values must be exactly 17 alphanumeric characters. They are normalized to uppercase.

### `POST /lookup`

```bash
curl -s -X POST http://127.0.0.1:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'
```

```json
{
  "vin": "1HGCM82633A004352",
  "make": "HONDA",
  "model": "Accord",
  "model_year": "2003",
  "body_class": "Sedan/Saloon",
  "cached": false
}
```

`cached` is `true` only when an unexpired row was already in SQLite.

### `POST /remove`

```bash
curl -s -X POST http://127.0.0.1:8000/remove \
  -H 'Content-Type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'
```

```json
{"vin": "1HGCM82633A004352", "deleted": true}
```

`deleted` is `false` if the VIN was not in the cache. HTTP 200 either way.

### `GET /export`

```bash
curl -s -O -J http://127.0.0.1:8000/export
```

Downloads `vin_cache.parquet` with columns `vin`, `make`, `model`, `model_year`, `body_class`. Expired rows are purged first.

## Sample VINs

`1HGCM82633A004352`
`5YJ3E1EA6PF384836`
`1FTFW1ET9DFC10312`
`1C4RJFBG2FC625797`
`5FNRL6H79NB021411`
`1HD1KBM15FB620271`
`1XPWD40X1ED215307`
