# Notes

## Schema

JSON, SQLite, and parquet share the same field names: `vin`, `make`, `model`, `model_year`, `body_class`.

The assignment listed display labels such as "Input VIN Requested" and "Cached Result?". Those are the same fields, written as snake_case so there is one schema to remember.

- `cached` is computed on lookup (live hit vs miss). It is not a column.
- `cached_at` is stored only so we can expire rows. It is omitted from API responses and parquet.
- Missing vPIC values are stored as empty strings, not NULL, so JSON and parquet stay uniform.

## Cache and TTL

Expiration is lazy: `CACHE_TTL_SECONDS` defaults to 7 days.

- On lookup, an expired row is deleted and treated as a miss.
- `/export` deletes expired rows, then dumps whatever is left.
- There is no background worker. Unused expired rows sit in the file until that VIN is looked up or export runs.

VIN attributes almost never change. The TTL is there to bound cache size and let a bad vPIC response age out, not because the decode goes stale.

## vPIC

`DecodeVinValues` is the endpoint because it returns a flat object. We keep only the four fields the assignment asks for.

- HTTP errors, timeouts, and empty `Results` are a **502**. Nothing is written.
- If a row is expired we delete it *before* calling vPIC. A subsequent 502 leaves that VIN uncached rather than serving stale data.

## Tradeoffs

| Choice | Why | Cost |
|---|---|---|
| Snake_case JSON instead of assignment labels | One schema across JSON, SQL, and parquet | Reviewers have to map "Cached Result?" → `cached` |
| SQLite file cache | Matches the assignment, zero ops for a demo | Not safe for multiple app replicas; writes serialize |
| Lazy TTL, no worker | Simple to explain and test | Expired rows linger until lookup or export |
| Delete expired row before vPIC | Never return stale data | A vPIC outage after expiry drops the last known decode |
| POST body for lookup/remove | Assignment says the request "contains" `vin` | Less convenient than `GET /lookup?vin=` for a browser |
| No VIN check digit | Assignment only requires 17 alphanumeric characters | Garbage VINs still hit vPIC and may cache empty fields |
| Cache only the four decode fields | Small table, parquet matches the API | Cannot debug from the raw vPIC payload |

## Demo UI

`GET /` serves a single static page that calls the three existing routes. No extra API, no extra columns. It exists so a walkthrough does not need Postman or curl. OpenAPI (`/docs`) is unchanged.

## Production

This would run as a container with the SQLite file on a volume. SQLite is fine for a single instance and a modest cache.

More traffic or multiple replicas would want Postgres (or Redis) and a lock around the miss-then-write path so two lookups of the same VIN do not both hit vPIC.
