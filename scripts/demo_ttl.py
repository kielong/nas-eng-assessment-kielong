#!/usr/bin/env python3
"""Live demo: cache TTL -- a hit within the window, a miss after expiry.

Demonstrates the reactive TTL path that is otherwise only documented in
prose (NOTES.md's tradeoffs table):

1. Within CACHE_TTL_SECONDS, a second lookup is a cache hit. VIN
   attributes almost never change, so the TTL is a bound on a bad cache
   entry, not a freshness strategy.
2. After CACHE_TTL_SECONDS, /lookup treats the row as a miss: get_live
   deletes the expired row BEFORE calling vPIC, then writes a fresh
   row. Fail-closed: if vPIC were down at that moment, the 502 would
   leave the VIN uncached -- correctness over availability, not
   last-known-good.

This talks to a REAL, already-running server over HTTP. It does not start
one itself. Start the server like this first (dedicated SQLite file;
short TTL so expiry is watchable live; no --reload):

    DATABASE_PATH=data/demo.db CACHE_TTL_SECONDS=8 uvicorn app.main:app

then, in a second terminal (same virtualenv as the server):

    python scripts/demo_ttl.py

Do not combine this with the cache-cap demo's CACHE_MAX_ROWS=3 on the
same process: a short TTL during that script's wait would expire early
rows and look like size-cap eviction. Use
scripts/demo_concurrency_and_cache_cap.py against its own server command.

CACHE_SWEEP_INTERVAL_SECONDS can stay at the production default. This
script is the /lookup reactive path (get_live deletes the expired row),
not the background sweep. The production default TTL is 7 days
(CACHE_TTL_SECONDS=604800); 8 seconds exists only so expiry is visible
in a live walkthrough.

DEMO_BASE_URL (default http://127.0.0.1:8000) points this script at a
non-default host/port if needed.
"""

import asyncio
import os
import sys
import time

import httpx

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")

# Assignment sample VIN -- known-decodable, so Phase 7's all-empty-fields
# check will not 502 and starve the demo of a row to expire.
DEMO_VIN = "1HGCM82633A004352"

# Production default is 7 days. This value exists only so expiry is
# watchable after one lookup in a live demo.
RECOMMENDED_DEMO_TTL_SECONDS = 8
RECOMMENDED_DEMO_DATABASE_PATH = "data/demo.db"

# cached_at is second-precision. Waiting TTL + 2s after the miss write
# lands puts us past expiry even if the write was at the start of a second.
WAIT_PAST_TTL_SECONDS = RECOMMENDED_DEMO_TTL_SECONDS + 2

START_SERVER_HINT = (
    f"  DATABASE_PATH={RECOMMENDED_DEMO_DATABASE_PATH} "
    f"CACHE_TTL_SECONDS={RECOMMENDED_DEMO_TTL_SECONDS} "
    "uvicorn app.main:app"
)

WAIT_TALKING_POINTS = [
    "TTL is a bound on a bad cache entry, not a freshness strategy -- "
    "VIN attributes almost never change.",
    "When this wait ends, /lookup will delete the expired row BEFORE "
    "calling vPIC. If vPIC were down, that 502 would leave the VIN "
    "uncached -- correctness over availability (NOTES.md).",
    "The production default is 7 days (CACHE_TTL_SECONDS=604800). "
    "8 seconds exists only so this is watchable live.",
]


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def check_server_reachable(client: httpx.AsyncClient) -> None:
    try:
        await client.get("/", timeout=5.0)
    except httpx.TransportError:
        # Covers both "nothing is listening" (ConnectError) and "something is
        # listening but not responding" (ConnectTimeout) -- siblings in
        # httpx's exception tree, not parent/child, so ConnectError alone
        # would miss a hung server and fall through to a raw traceback.
        print(f"Could not reach {BASE_URL}.")
        print()
        print("Start the server first -- dedicated DB file, short TTL,")
        print("no --reload -- then re-run this script:")
        print()
        print(START_SERVER_HINT)
        sys.exit(1)


async def lookup(client: httpx.AsyncClient, vin: str) -> tuple[dict, float]:
    start = time.monotonic()
    response = await client.post("/lookup", json={"vin": vin}, timeout=30.0)
    elapsed = time.monotonic() - start
    response.raise_for_status()
    return response.json(), elapsed


async def remove(client: httpx.AsyncClient, vin: str) -> bool:
    response = await client.post("/remove", json={"vin": vin}, timeout=10.0)
    response.raise_for_status()
    return bool(response.json()["deleted"])


async def wait_for_ttl(timeout: float) -> None:
    start = time.monotonic()
    last_printed_at = -10.0
    talking_index = 0
    while True:
        elapsed = time.monotonic() - start
        remaining = timeout - elapsed
        if elapsed - last_printed_at >= 3.0:
            print(f"  ...{remaining:.1f}s left until expiry is observable...")
            print(f"     {WAIT_TALKING_POINTS[talking_index % len(WAIT_TALKING_POINTS)]}")
            talking_index += 1
            last_printed_at = elapsed
        if elapsed >= timeout:
            return
        await asyncio.sleep(1.0)


async def demo_ttl(client: httpx.AsyncClient) -> None:
    banner("Demo: cache TTL (hit inside the window, miss after expiry)")
    print(f"VIN under test: {DEMO_VIN}")
    print(
        f"This demo expects the server running with CACHE_TTL_SECONDS="
        f"{RECOMMENDED_DEMO_TTL_SECONDS} -- see the top of this script"
    )
    print("for the exact command. The production default (7 days) cannot")
    print("be waited out in a live walkthrough.")

    print()
    print("Step 1 -- remove it first, so we start from a guaranteed cache miss.")
    deleted = await remove(client, DEMO_VIN)
    print(f"  POST /remove -> deleted={deleted}")

    print()
    print("Step 2 -- look it up. This calls vPIC and writes a fresh row.")
    miss, miss_elapsed = await lookup(client, DEMO_VIN)
    print(
        f"  POST /lookup -> cached={miss['cached']}  "
        f"{miss['make']} {miss['model']} ({miss['model_year']})  "
        f"({miss_elapsed * 1000:.0f} ms)"
    )

    print()
    print("Step 3 -- look it up again immediately. VIN attributes have not")
    print("changed; the row is well inside TTL. This should be a cache hit.")
    hit, hit_elapsed = await lookup(client, DEMO_VIN)
    print(f"  POST /lookup -> cached={hit['cached']}  ({hit_elapsed * 1000:.0f} ms)")
    if hit["cached"] and hit_elapsed < miss_elapsed:
        print("  Served from SQLite, not vPIC -- TTL is not a refresh clock.")
    elif hit["cached"]:
        print("  cached=True, as expected inside the TTL window.")
    else:
        print("  cached=False -- the server's CACHE_TTL_SECONDS is probably")
        print("  shorter than this script expected, or the row was not stored.")
        print("  Start the server with:")
        print()
        print(START_SERVER_HINT)
        sys.exit(1)

    print()
    print(
        f"Step 4 -- waiting {WAIT_PAST_TTL_SECONDS}s "
        f"(CACHE_TTL_SECONDS={RECOMMENDED_DEMO_TTL_SECONDS} plus 2s of slack"
    )
    print("for second-precision cached_at). Nothing calls /lookup, /remove,")
    print("or /export during this wait -- we want get_live's delete-on-read")
    print("to be what expires the row, not export's purge-expired side effect")
    print("and not the background sweep.")
    await wait_for_ttl(WAIT_PAST_TTL_SECONDS)

    print()
    print("Step 5 -- look it up again. The row is now past TTL, so this is a")
    print("miss: get_live deletes the expired row, then vPIC is called, then")
    print("a fresh row is written. Watch the server's uvicorn logs for a new")
    print("DecodeVinValues request.")
    expired, expired_elapsed = await lookup(client, DEMO_VIN)
    print(
        f"  POST /lookup -> cached={expired['cached']}  "
        f"({expired_elapsed * 1000:.0f} ms)"
    )

    print()
    if not expired["cached"] and expired_elapsed >= hit_elapsed:
        print("cached=False and the request took network-scale time -- vPIC")
        print("ran again. The expired row was deleted before that call")
        print("(app/db.py get_live), so a vPIC failure here would have been")
        print("a 502 with nothing left in the cache. We are not taking vPIC")
        print("down live; that fail-closed path is the test")
        print("'test_expired_row_then_vpic_failure_leaves_vin_uncached'.")
    elif not expired["cached"]:
        print("cached=False, so expiry was treated as a miss. Timing did not")
        print("clearly separate from the Step 3 hit -- vPIC may have been")
        print("unusually fast. The cached flag is the signal that matters.")
    else:
        print("cached=True -- the row was still live. The server is probably")
        print("still on the 7-day default TTL. Restart it with:")
        print()
        print(START_SERVER_HINT)
        sys.exit(1)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        await check_server_reachable(client)
        try:
            await demo_ttl(client)
        except httpx.HTTPError as exc:
            print()
            print(f"A request to {BASE_URL} failed: {exc}")
            print("This is most likely a transient vPIC/network issue -- check the")
            print("server's own logs, and try running this script again.")
            sys.exit(1)
    print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
