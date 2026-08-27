# Notes

## What I built

A FastAPI service with three routes: `POST /lookup` (SQLite first, vPIC on a miss, then store), `POST /remove`, and `GET /export` (parquet of live cache rows, filename stamped with a UTC timestamp so repeat exports don't overwrite each other on disk). Lookup and remove take `{"vin": "..."}` so a vehicle identifier is not a query string that would land in access logs and browser history. Export takes nothing.

This is a cache, not a vehicle warehouse. JSON, SQLite, and parquet share one vocabulary — `vin`, `make`, `model`, `model_year`, `body_class` — so there is one schema to keep honest instead of translating display labels (`"Input VIN Requested"`, `"Cached Result?"`) on every boundary. `cached` answers "did this request hit SQLite?"; that is request-scoped, so it is never stored. `cached_at` exists only so the cache can expire and evict, so it is never exported. Missing vPIC values are empty strings, not NULL, so every representation stays the same type.

VINs are 17 alphanumeric characters, uppercased before they become a cache key, because mixed-case would split the cache. I do not also enforce a check digit or ban `I`/`O`/`Q` — whether a VIN is *real* is vPIC's call, and two independent validity rules would eventually disagree. Invalid VIN is 422 (the request is wrong). vPIC failure is 502 and nothing is written (the upstream is wrong; fail closed so a garbage decode cannot sit in the cache). A remove miss is 200 with `deleted: false`, because that boolean is the result, not an HTTP 404.

I call vPIC `DecodeVinValues` because it returns a flat `Results[0]` I can map 1:1 onto those four fields. I discard the rest of the payload so the table and the parquet file stay the same shape as the API. vPIC returns HTTP 200 even for a well-formed but undecodable VIN (blank make/model/year/body class); I treat that as a 502 rather than caching empties.

Entries expire after 7 days. VIN attributes almost never change, so the TTL is a bound on a bad cache entry, not a freshness strategy. `/lookup` deletes an expired row for that VIN before re-fetching, so a miss always means it is safe to write a fresh row. That reactive path is not enough on its own: a VIN looked up once and never revisited sits past TTL, and TTL does not cap how many distinct VINs arrive in one window.

This is a cache, so it needs a ceiling. A database is allowed to grow with the business; a cache that can grow without bound is just an unbounded table that `/export` loads into memory. An `asyncio` task in the same lifespan as the DB engine and httpx client closes both gaps — purge expired, then if the table is still over 10,000 rows, delete the oldest by `cached_at` — without a scheduler process. Oldest-first is the right eviction here because a cache hit does not refresh `cached_at` (the decode does not get more correct by being read). The entries that have sat the longest are also the closest to TTL. 10,000 is large enough that a normal run never hits it, and small enough that the SQLite file and an in-memory parquet export stay cheap. In production I would set the cap to expected distinct VINs in one TTL window, not to a magic number. The cap is enforced on that timer, not on every write, so `/lookup` stays get-or-upsert.

`GET /` is a static page over the same three routes so a walkthrough does not need Postman. It adds no API and no columns. OpenAPI is unchanged.

Two behaviors that are easy to claim and hard to watch — concurrent first-miss without a lock, and a size cap that is not LRU and not enforced on write — are narrated live by `scripts/demo_concurrency_and_cache_cap.py`. The reactive TTL path (hit inside the window, miss after expiry, delete-before-vPIC) is `scripts/demo_ttl.py`. They need different server env vars, so they are separate scripts; run commands are in each file's docstring and in README.md.

## Tradeoffs

Decisions I had latitude on — not the ones that were already fixed (three routes, SQLite, 17-alphanumeric VINs).

| Decision | Why I chose it | What it costs |
|---|---|---|
| Snake_case JSON (`vin`, `cached`, `deleted`) instead of display labels (`"Input VIN Requested"`, `"Cached Result?"`, `"Cache Delete Success?"`) | One vocabulary across JSON, SQL, and parquet — no per-format translation layer to keep in sync | A reviewer maps the labels to keys once |
| `POST` with a JSON body for lookup/remove, not `GET ?vin=` | A VIN in a query string lands in access logs and browser history | Less convenient than a pasteable URL — the demo page covers that |
| Validation is "17 alphanumeric" only — no check digit, `I`/`O`/`Q` accepted | Whether a VIN is *real* is vPIC's job. I didn't want the request validator and the client independently deciding validity and disagreeing | A nonsense-but-well-formed VIN still round-trips to vPIC |
| Cache only the four decode fields, not the raw vPIC payload | Small table; parquet matches the API 1:1 | Can't debug a bad decode from the original vPIC blob |
| Delete an expired row before calling vPIC, not after | Never serve a stale decode | A vPIC outage right after expiry drops last-known-good — correctness over availability |
| All-four-fields-empty decode is a 502, not vPIC's `ErrorCode` | vPIC returns HTTP 200 for garbage VINs with every mapped field blank. Confirmed live: `AAAAAAAAAAAAAAAAA` → 200, `ErrorCode: "1,7,400"`, all four empty. A signal I could verify, not a comma-separated taxonomy I couldn't fully confirm | Narrower than `ErrorCode` — a decode with *some* fields populated but a bad check digit still caches |
| Hard cap of 10,000 rows; oldest `cached_at` evicted first | This is a cache, not a database. TTL bounds how stale a row can be, not how many rows exist — a burst of distinct VINs in one window would otherwise grow forever, and `/export` already loads the live table into memory. 10,000 is generous for a modest cache and cheap to export; oldest-first matches a hit path that does not bump `cached_at` | A hot VIN cached early can still be evicted before a cold VIN cached later. Production would size the cap to expected distinct VINs in one TTL window |
| Cap and TTL enforced on a timer, not on every `/lookup` write | One in-process task for both concerns; the hot path stays get-or-upsert | The table can transiently exceed 10,000 rows between ticks — bounded by one interval of distinct VINs, not unbounded |
| No per-VIN lock on cache-miss | Duplicate vPIC on concurrent first-miss is wasteful, not incorrect (second write overwrites the first with the same data). A lock table keyed by every VIN grows for the life of the process | Thundering herd on the first concurrent miss — I'd add single-flight before real traffic |
| SQLite WAL + `busy_timeout=5000` | Fixes a `database is locked` failure I could reproduce in one process (two overlapping writes) | Still one file, one writer at a time — does not help across replicas |
| SQLAlchemy 2 async over raw `aiosqlite` | Needed async session lifecycle for concurrent requests anyway; one declarative model is the Postgres migration path | Heavier than raw SQL for a single table |
| `GET /export` purges expired rows as a side effect | Export should match lookup's idea of "live"; same expiry predicate | Breaks HTTP's "GET is safe" contract — fine with no caching proxy in front of this API |
| Export filename is `vin_cache_<UTC timestamp>.parquet`, not fixed | A fixed name meant every export collided and silently overwrote the last on disk (`curl -O -J` always overwrites) | Second-precision — two exports inside the same second still collide, and I'm fine with that, not chasing microsecond uniqueness for a human clicking "export" |

## Production

I'd run one container, one uvicorn worker, SQLite on a volume. There is no Dockerfile in the repo; the image would be `python:3.11-slim` plus uvicorn. Multiple workers on the same SQLite file is the line where I'd move to Postgres, not add more SQLite cleverness. SQLite is fine for a single instance and a modest cache.

### If this had to handle real traffic

In rough priority order, sorted by throughput bottleneck, not by risk exposure — under a risk framing, auth (#6) would move much higher, since it's a cost/abuse risk at any traffic level, not just at scale:

1. **Move the cache to Postgres.** A SQLite file pins the app to a single writer/instance; Postgres unblocks horizontal scaling.
2. **Collapse concurrent misses.** A per-VIN single-flight — an in-process `asyncio.Lock`, or a Postgres advisory lock across replicas — so N simultaneous first-lookups of the same VIN produce one vPIC call, not N.
3. **Retry with backoff + a circuit breaker around vPIC.** Each `/lookup` is its own coroutine, so one slow response doesn't block others directly — but a burst of concurrently slow or down vPIC responses exhausts httpx's connection pool, backing up every new vPIC call behind it.
4. **Stream `/export`.** It currently loads every live row into memory before responding — fine at demo scale, not once the cache is large.
5. **Structured logging + a cache-hit-ratio metric.** The maintenance sweep already logs purge/evict counts, but there's no per-request hit/miss visibility and nothing you could graph — that's the real gap.
6. **Auth or rate limiting in front of `/lookup`.** As it stands this is a free, unauthenticated proxy to a third-party API.
