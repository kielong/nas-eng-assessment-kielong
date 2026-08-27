# Notes

## Schema design

One schema, three representations. JSON responses, the SQLite table, and the parquet export all use the same field names — `vin`, `make`, `model`, `model_year`, `body_class` — instead of a different vocabulary per format. `cached` is computed at request time and never stored. `cached_at` exists purely for the cache's own bookkeeping and is dropped from every external representation. Missing vPIC values are stored as empty strings, not NULL, so every representation stays uniformly typed — no format-specific null-handling to write or test.

## Cache and TTL

Two mechanisms, not one:

- **Reactive, per-VIN cleanup.** `/lookup` deletes an expired row for that VIN before re-fetching. `/remove` deletes unconditionally. `/export` sweeps every expired row before dumping the rest.
- **Active, periodic maintenance.** A background `asyncio` task, started and cancelled in the same app lifespan that owns the DB engine and httpx client, runs once immediately at startup and then every `CACHE_SWEEP_INTERVAL_SECONDS` (default 1 hour). Each tick purges every expired row and evicts the oldest rows by `cached_at` if the table exceeds `CACHE_MAX_ROWS` (default 10,000).

The reactive path alone leaves two gaps: a VIN looked up once and never revisited sits past its TTL until something happens to touch it, and TTL bounds *staleness*, not *size* — a burst of lookups for distinct VINs within one TTL window can still grow the table with no ceiling. The periodic task closes both on the same timer, without adding a scheduler process — it's an `asyncio` task inside the existing app, not new infrastructure.

## vPIC

`DecodeVinValues` is the endpoint because it returns a flat object; I keep only the four fields the assignment asks for and discard the rest.

- HTTP errors, timeouts, and empty `Results` are a **502**. Nothing is written.
- An expired row is deleted *before* calling vPIC, not after — a subsequent 502 leaves that VIN uncached rather than serving a stale decode.
- vPIC returns HTTP **200** even for a well-formed but undecodable VIN — it just leaves every mapped field blank. I confirmed this directly against the live API rather than assuming it: `AAAAAAAAAAAAAAAAA` comes back 200 with `Make`/`Model`/`ModelYear`/`BodyClass` all empty and `ErrorCode: "1,7,400"`; a real sample VIN comes back with those fields populated and `ErrorCode: "0"`. I treat an all-four-empty decode as a failure — 502, nothing cached — using that verified signal instead of parsing vPIC's own `ErrorCode`, a comma-separated, semi-documented taxonomy I couldn't fully confirm.

## Concurrency

SQLite connections set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on connect. Without it, two overlapping writes from a single process — two browser tabs hitting `/lookup` at once, say — raise `database is locked` instead of one simply waiting. This only helps within one process; it doesn't make SQLite safe across multiple app replicas (see Production).

I deliberately did not add a per-VIN lock to collapse concurrent duplicate vPIC calls. Two simultaneous first-lookups of the same VIN each miss and each call vPIC — wasteful, not incorrect, since the second write just overwrites the first with identical data. An `asyncio.Lock` keyed by VIN would fix that, but a lock table keyed by every VIN ever looked up grows without bound for the life of the process — a tradeoff of its own that I didn't want to take on silently.

## Tradeoffs

These are the decisions where I had real latitude — not the ones the assignment already made for me (three routes, SQLite, 17-alphanumeric VINs).

| Decision | Why I chose it | What it costs |
|---|---|---|
| Delete an expired row before calling vPIC, not after | Never serve a stale decode | A vPIC outage right after expiry drops the last-known-good value instead of degrading gracefully — I chose correctness over availability |
| One periodic task enforces both TTL and the row cap, on an interval — not instantly on every write | Reuses a single timer for both concerns instead of adding cap-checking to the hot `/lookup` write path | Between ticks, the table can transiently exceed `CACHE_MAX_ROWS` — bounded by how many distinct VINs get written in one interval, not unbounded |
| No per-VIN lock on cache-miss | Simpler code, no lock-table lifecycle to reason about, at demo scale | Concurrent first-lookups of the same VIN double-call vPIC (thundering herd) — I'd add one before this saw real traffic |
| Treat an all-fields-empty vPIC decode as a failure, not vPIC's `ErrorCode` | A signal I could verify directly against the live API, not a taxonomy I'd be trusting blindly | Narrower than `ErrorCode` — a decode with *some* fields populated but a bad check digit still caches |
| SQLite in WAL mode + a busy timeout | Removes a `database is locked` failure I could reproduce, not a hypothetical one | Still one file, one writer at a time — doesn't help across multiple replicas |
| SQLAlchemy async ORM over raw `aiosqlite` calls | One declarative model instead of hand-written DDL and row-mapping; the async session machinery I needed anyway for concurrent requests | Heavier dependency and more indirection than raw SQL for what's ultimately one table |
| `GET /export` purges expired rows as a side effect | Export should reflect the live cache; reusing the existing expiry predicate keeps parquet and lookup semantics identical | Technically breaks HTTP's "GET is safe" contract — acceptable because nothing like a caching proxy sits in front of this API today |
| One shared field-name vocabulary across JSON, SQL, and parquet, instead of mirroring the assignment's display labels | No per-format translation layer to keep in sync or test separately | A reviewer has to map "Cached Result?" to `cached` once — a small, one-time cost for a permanently smaller schema surface |
| `POST` with a JSON body for lookup/remove, not `GET ?vin=` | Keeps a per-vehicle identifier out of URLs — no VIN in server access logs or browser history | Less convenient than a bare, pasteable URL — worked around with the demo page |
| Validation enforces only "17 alphanumeric," not real-VIN rules (no check digit, `I`/`O`/`Q` accepted) | One source of truth for whether a VIN is *real*: vPIC. I didn't want the request validator and the vPIC client independently deciding VIN validity and risking disagreement | A syntactically valid but nonsense VIN still round-trips to vPIC before the all-empty-fields check catches it |
| Cache only the four decode fields, not the raw vPIC payload | Small table; parquet export matches the API 1:1 with no extra mapping | Can't debug a bad decode from the original vPIC response — only from what was chosen to keep |

## Demo UI

`GET /` serves a single static page that calls the three existing routes. No extra API, no extra columns. It exists so a walkthrough does not need Postman or curl. OpenAPI (`/docs`) is unchanged.

## Production

This would run as a container with the SQLite file on a volume. SQLite is fine for a single instance and a modest cache.

### If this had to handle real traffic

In rough priority order:

1. **Move the cache to Postgres.** A SQLite file pins the app to a single writer/instance; Postgres unblocks horizontal scaling.
2. **Collapse concurrent misses.** A per-VIN single-flight — an in-process `asyncio.Lock`, or a Postgres advisory lock across replicas — so N simultaneous first-lookups of the same VIN produce one vPIC call, not N.
3. **Retry with backoff + a circuit breaker around vPIC.** One slow or down NHTSA response shouldn't cascade to every idle request.
4. **Stream `/export`.** It currently loads every live row into memory before responding — fine at demo scale, not once the cache is large.
5. **Structured logging + a cache-hit-ratio metric.** No way today to observe whether the cache is doing its job.
6. **Auth or rate limiting in front of `/lookup`.** As it stands this is a free, unauthenticated proxy to a third-party API.
