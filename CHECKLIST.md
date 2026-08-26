# Implementation checklist

This file is the execution record for the VIN decoder: what was built, in what order, and when each piece was marked done. The design decisions themselves — including the alternatives considered and why each one lost — live in [`.cursor/plans/vin_decoder_design.plan.md`](.cursor/plans/vin_decoder_design.plan.md). The original problem is in [ASSIGNMENT.md](ASSIGNMENT.md). Interview-facing tradeoffs and the production discussion live in [NOTES.md](NOTES.md).

We used AI as a coding assistant, not as the source of the design. Order of work:

1. Read the assignment and the vPIC API (DecodeVinValues, not the nested DecodeVin payload).
2. Write a small design: three routes, one SQLite table, only the fields we return.
3. Stop and decide cache expiration with a human in the loop (lazy TTL + delete-on-read, `CACHE_TTL_SECONDS` default 7 days) before picking an approach.
4. Lock the JSON / SQL / parquet schema so they share names. Implement only against that.
5. Build in the phases below. A later phase does not start until the earlier one is done.
6. A demo UI is a later phase so a presentation does not need Postman. It is specified here and built in Phase 6.
7. Phase 6 was the end of the original submission. Phase 7 is a self-review pass: read the finished implementation back against the assignment, find what it got wrong, fix only what's in scope.

Do not invent extra routes, extra columns, or a background worker unless a later phase says so.

## Locked decisions (before Phase 1)

These were decided up front so implementation could stay small.

| Topic | Decision | Why |
|---|---|---|
| Routes | `POST /lookup`, `POST /remove`, `GET /export` | Assignment says lookup/remove **contain** `vin` → JSON body. Export has no input. |
| JSON keys | `vin`, `make`, `model`, `model_year`, `body_class`, plus computed `cached` / `deleted` | Same names as the table and parquet. Assignment labels ("Input VIN Requested", "Cached Result?") are those fields, not wire keys. |
| Table | One `vin_cache` table, VIN as PK | Assignment is a cache, not a vehicle warehouse. |
| Stored columns | Four decode fields + `cached_at` | `cached` is request-scoped. Raw vPIC JSON is not stored. |
| VIN rules | 17 alphanumeric, uppercase before any cache key | Assignment does not require a check digit. |
| vPIC | `GET .../DecodeVinValues/{vin}?format=json` | Flat object; map `Make` / `Model` / `ModelYear` / `BodyClass` only. |
| TTL | Lazy, delete-on-read; env `CACHE_TTL_SECONDS`, default `604800` | VIN attributes almost never change. TTL bounds size and ages out a bad decode. No worker. |
| Errors | Invalid VIN **422**; vPIC failure **502**; remove miss **200** + `deleted: false` | The boolean on remove is the result, not an HTTP 404. |
| Export | Purge expired, then parquet of live rows only | Columns match lookup minus `cached`. Empty cache still yields a valid file. |

Expired-then-vPIC-fails: we delete the stale row first, then call vPIC. A 502 leaves the VIN uncached. That is intentional (no stale reads), and it is called out in NOTES.

---

## Phase 1 — Scaffold

**Goal.** Empty app that can be installed on another machine. No business logic yet.

**Context.** FastAPI is required. Layout stays flat so every file can be walked through in a review. Dependencies live in `requirements.txt` (runtime) and `requirements-dev.txt` (tests); a clean-checkout venv only needs `pip install -r requirements.txt` (plus `-r requirements-dev.txt` for tests) — no editable install of the local `app` package is documented or required to run the app. That gap is exactly what made bare `pytest` fail before Phase 7 fixed it (see below).

**Do**

- [x] `app/` package: `main.py`, `settings.py`, `schemas.py`, `db.py`, `vpic.py`, `routes.py`
- [x] `data/` for SQLite (`data/cache.db` gitignored)
- [x] `pyproject.toml` (Python 3.11+, FastAPI, uvicorn, httpx, SQLAlchemy asyncio, aiosqlite, pyarrow)
- [x] `requirements.txt` / `requirements-dev.txt` with pinned versions
- [x] `.gitignore` for `.venv`, `__pycache__`, `data/*.db`

**Done when.** `pip install -r requirements.txt` in a fresh venv installs enough to import the package. DB file is not in git.

---

## Phase 2 — Persistence and vPIC

**Goal.** The cache and the NHTSA client, with no HTTP routes yet (routes consume these in Phase 3).

**Context.** Store only what lookup returns, plus `cached_at` for TTL. SQLAlchemy 2.0 async + aiosqlite. Shared `httpx.AsyncClient` will be created in app lifespan in Phase 3; the client function itself takes an `httpx.AsyncClient`.

**Do**

- [x] Model `vin_cache`: `vin` PK, `make`, `model`, `model_year`, `body_class`, `cached_at` (UTC ISO-8601 text)
- [x] Empty vPIC fields stored as `""`, not NULL
- [x] `get_live`: missing or age ≥ TTL → delete row, return miss
- [x] `upsert` with a new `cached_at`
- [x] `delete_vin` (true if a row existed, expired or not)
- [x] `purge_expired` then `list_all` for export
- [x] `decode_vin`: DecodeVinValues, no `modelyear` query param, ~10s timeout
- [x] `VpicError` on non-200, timeout, invalid JSON, or empty `Results`

**Done when.** A row can be written, read as live, treated as expired, and deleted. A decode maps four fields and fails closed.

---

## Phase 3 — API routes

**Goal.** The three assignment routes, using Phase 2 only.

**Context.** Pydantic `VinRequest` normalizes VIN to uppercase, then enforces `^[A-Z0-9]{17}$`. `cached` is never written to SQLite. Parquet via pyarrow (not pandas). App factory + lifespan: create tables, httpx client, engine.

**Do**

- [x] `POST /lookup` → live hit `{ ..., "cached": true }`; miss/expiry → vPIC → upsert → `{ ..., "cached": false }`; vPIC failure → 502
- [x] `POST /remove` → `{ "vin", "deleted" }`, HTTP 200 either way
- [x] `GET /export` → purge expired, parquet attachment `vin_cache.parquet` (`application/vnd.apache.parquet`)
- [x] Empty / all-expired export is a valid empty table with the five string columns

**Done when.** Lookup miss then hit does not call vPIC twice. Export does not include `cached` or `cached_at`. Invalid VIN is 422.

---

## Phase 4 — Tests

**Goal.** Prove the locked behavior without hitting NHTSA.

**Context.** `respx` mocks DecodeVinValues. Each test gets a temp SQLite file. Assignment sample VINs are used where a real-looking value helps.

**Do**

- [x] Reject short, long, and non-alphanumeric VINs (422)
- [x] Lowercase VIN is stored/returned uppercase
- [x] Cache miss then hit: one vPIC call, second response `cached: true`
- [x] Row older than TTL is a miss and triggers vPIC
- [x] Expired row + vPIC 500 → 502 and the VIN is gone from SQLite
- [x] vPIC 500 or empty `Results` → 502, nothing cached
- [x] Remove hit then miss (`deleted` true then false)
- [x] Empty export is valid parquet with the five columns
- [x] Export includes live rows and drops expired ones from the file and the DB

**Done when.** `pytest` is green offline.

---

## Phase 5 — Docs

**Goal.** A reviewer can run the service from a clean checkout and talk through the design.

**Context.** `README.md` is the project README (run, try sample VINs, API). The original challenge text must not disappear when that README is rewritten.

**Do**

- [x] `README.md`: `pip install -r requirements.txt`, uvicorn, PyCharm interpreter note, `/docs` and curl for sample VINs
- [x] `ASSIGNMENT.md`: original problem statement
- [x] `NOTES.md`: schema, TTL, vPIC, tradeoffs table, production sketch
- [x] This checklist as the phase record tied to the design plan

**Done when.** Someone else can install, look up a sample VIN, and find the assignment + the "why" without reading the chat history.

---

## Phase 6 — Demo UI

**Goal.** A page this app serves so a live walkthrough does not need Postman or curl.

**Context.** The assignment allows extra functionality. The backend is already the product; the UI only calls Phase 3 routes. No new API, no new table columns, no auth.

**Plan**

- [x] Serve a single page from the FastAPI app (static HTML or a template; keep it small)
- [x] VIN text field
- [x] Clickable list of the assignment sample VINs that fills the field
- [x] Lookup: show `make`, `model`, `model_year`, `body_class`, and whether it was cached
- [x] Remove: show whether a row was deleted
- [x] Export: download the parquet file
- [x] Surface 422 / 502 in the page so a bad VIN or a vPIC outage is visible in the demo

**Done when.** With the server running, a reviewer can exercise lookup (miss then hit), remove, and export from a browser.

**Out of scope for this phase.** SPA framework, login, editing cached fields, TTL admin UI.

---

## Phase 7 — Hardening (self-review pass)

**Goal.** Phases 1–6 were built on Cursor as the original submission. This phase is a second pass — reviewing that implementation against the assignment and fixing what it missed — done with Claude. It closes three correctness/robustness gaps found in `/lookup`, the SQLite layer, and the test-packaging setup. It does not add scope: no new routes, columns, or dependencies, and no production/multi-replica concerns (those are deliberately deferred — see NOTES.md, "If this had to handle real traffic").

**Context.** Three gaps were in scope because they affect the app *as delivered*, not just at hypothetical future scale:

1. vPIC returns HTTP 200 with `Results[0]` present even for a well-formed but undecodable VIN — confirmed against the live API (`AAAAAAAAAAAAAAAAA` → 200, `Make`/`Model`/`ModelYear`/`BodyClass` all blank, `ErrorCode: "1,7,400"`). The original implementation had no way to tell that apart from a real decode, so a garbage VIN would cache as a false "hit" with empty fields forever (until TTL).
2. SQLite's default rollback-journal mode raises `database is locked` under concurrent writers even within a single process — e.g. two overlapping requests from the demo page. Nothing in the original implementation configured around this.
3. Bare `pytest` (the command the README documents) failed with `ModuleNotFoundError: No module named 'app'` on a genuinely clean, isolated venv. `pyproject.toml` declares `app` as an installable package, but nothing in the documented setup (`pip install -r requirements.txt` / `requirements-dev.txt`) installs it that way, and pytest's own import machinery only puts `tests/` on `sys.path`, not the repo root. Reproduced by hand in an isolated venv before fixing, to confirm the real cause rather than guessing.

A per-VIN request lock (to collapse concurrent duplicate vPIC calls) and vPIC's own `ErrorCode` taxonomy were both considered and deliberately **not** implemented here — the former is a production-scale concern with its own tradeoff (unbounded lock-table growth), and the latter is a semi-documented, comma-separated field I could not fully verify. Both are named explicitly in NOTES.md rather than guessed at in code.

**Do**

- [x] `decode_vin` treats a decode where all four fields (`make`, `model`, `model_year`, `body_class`) come back empty as `VpicError` — 502, nothing written to the cache
- [x] SQLite connections set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on connect
- [x] `pyproject.toml`: `pythonpath = ["."]` under `[tool.pytest.ini_options]`, so bare `pytest` resolves `app` without requiring an editable install
- [x] `.idea/` untracked and gitignored

**Tests added**

- [x] Some vPIC fields missing (not all) → still a 200, stored/returned as empty strings for just those fields
- [x] All four vPIC fields empty → 502, nothing written to SQLite
- [x] A fresh SQLite connection reports `journal_mode=wal` and `busy_timeout=5000`

**Verified manually.** The `pythonpath` fix was confirmed by reproducing `ModuleNotFoundError` in a properly isolated fresh venv (`pip install -r requirements-dev.txt` only, no editable install), then re-running `pytest` against that same venv after the fix.

**Done when.** `pytest` is still green offline — including a bare `pytest` invocation with no editable install — and all three fixes are demonstrable without any new routes, columns, or dependencies.
