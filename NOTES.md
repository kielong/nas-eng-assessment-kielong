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
- vPIC returns HTTP **200** even for a well-formed but undecodable VIN — it just leaves every field blank inside `Results[0]`. Confirmed against the live API: `AAAAAAAAAAAAAAAAA` comes back 200 with `Make`/`Model`/`ModelYear`/`BodyClass` all empty and `ErrorCode: "1,7,400"`. If all four target fields come back empty, we raise the same 502 as a transport failure and cache nothing — otherwise a garbage VIN would sit in the cache as a false "hit" until TTL. We check this by our own four-empty-fields signal, not vPIC's `ErrorCode`, because that field is a semi-documented, comma-separated code list I could not fully verify the semantics of — a real sample VIN (`1HGCM82633A004352`) comes back with `ErrorCode: "0"`, so at least the happy path is trustworthy, but I didn't want to hardcode an interpretation of the unhappy path I wasn't sure of.

## Concurrency

SQLite connections set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on connect. Without this, two overlapping writes from a single process (e.g. two browser tabs hitting `/lookup` at once) raise `database is locked` rather than one waiting briefly for the other. This only helps within one process — it does not make SQLite safe across multiple app replicas (see Production).

## Tradeoffs

| Choice | Why | Real cost |
|---|---|---|
| Delete expired row before calling vPIC | Never serve stale data | A vPIC outage right after expiry drops the last-known-good decode instead of degrading gracefully — I chose correctness over availability |
| No per-VIN lock on cache-miss | Single-instance demo scope; a lock keyed by every VIN ever looked up is an unbounded-memory tradeoff of its own | Concurrent first-lookups of the same VIN cause duplicate vPIC calls (thundering herd) — I'd add an in-process `asyncio.Lock` (or a DB-level advisory lock across replicas) before this saw real traffic |
| vPIC decode treated as a failure only when all four fields are empty | Verified against the live API rather than trusting vPIC's own `ErrorCode` taxonomy, which is comma-separated and only semi-documented | Won't catch every vPIC-flagged problem (e.g. a decode with *some* fields populated but a bad check digit still caches) — narrower but something I could actually confirm |
| SQLite in WAL mode + busy timeout | Removes a `database is locked` failure I could reproduce (two concurrent writers, one process), not a hypothetical | Still one file, one writer at a time — doesn't help across multiple app replicas, which needs Postgres |
| `GET /export` purges expired rows as a side effect | Export should reflect the live cache; reusing the existing expiry predicate keeps parquet and lookup semantics identical | Technically violates HTTP's "GET is safe" contract — a caching proxy or prefetcher in front of this could trigger unintended deletes. Acceptable because nothing like that sits in front of this API today |
| Snake_case JSON instead of assignment labels | One schema across JSON, SQL, and parquet | Reviewers have to map "Cached Result?" → `cached` |
| SQLite file cache | Matches the assignment, zero ops for a demo | Not safe for multiple app replicas; writes still serialize within the one file |
| POST body for lookup/remove | Assignment says the request "contains" `vin` | Less convenient than `GET /lookup?vin=` for a browser |
| No VIN check digit, and `I`/`O`/`Q` accepted | Assignment only requires 17 alphanumeric characters — real VINs never use those three letters, but the spec doesn't say to enforce that | A syntactically valid VIN that's semantically garbage still round-trips to vPIC on every miss |
| Cache only the four decode fields | Small table, parquet matches the API | Cannot debug from the raw vPIC payload |

## Demo UI

`GET /` serves a single static page that calls the three existing routes. No extra API, no extra columns. It exists so a walkthrough does not need Postman or curl. OpenAPI (`/docs`) is unchanged.

## Production

This would run as a container with the SQLite file on a volume. SQLite is fine for a single instance and a modest cache.

### If this had to handle real traffic

In rough priority order:

1. **Move the cache to Postgres.** A SQLite file pins the app to a single writer/instance; Postgres unblocks horizontal scaling.
2. **Collapse concurrent misses.** A per-VIN single-flight — an in-process `asyncio.Lock` keyed by VIN, or a Postgres advisory lock across replicas — so N simultaneous first-lookups of the same VIN produce one vPIC call, not N.
3. **Retry with backoff + a circuit breaker around vPIC.** Right now one slow or down NHTSA response just times out per-request; a breaker would stop hammering a dependency that's already failing.
4. **Stream `/export`.** It currently loads every live row into memory and builds the whole parquet buffer before responding. Fine at demo scale, not once the cache is large.
5. **Structured logging + a cache-hit-ratio metric.** There's no way today to observe whether the cache is actually doing its job, or how often vPIC is being hit.
6. **Auth or rate limiting in front of `/lookup`.** As it stands this is a free, unauthenticated proxy to a third-party API — anyone can use it to indirectly hammer NHTSA through this service.
