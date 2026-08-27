#!/usr/bin/env python3
"""Live demo: concurrent duplicate lookups, and cache size-cap eviction.

Demonstrates two behaviors that are otherwise only documented in prose
(NOTES.md's tradeoffs table):

1. No per-VIN lock on cache-miss: two concurrent lookups of the same
   never-before-cached VIN both independently call vPIC.
2. Size-cap eviction: once the cache exceeds CACHE_MAX_ROWS, the oldest
   rows (by cached_at) are evicted by the background maintenance task in
   app/main.py (_cache_maintenance_loop) -- not by anything this script
   does directly.

This talks to a REAL, already-running server over HTTP. It does not start
one itself, so it matches exactly what you'd see watching the server's own
uvicorn logs during a live walkthrough. Start the server like this first:

    CACHE_MAX_ROWS=3 CACHE_SWEEP_INTERVAL_SECONDS=5 uvicorn app.main:app --reload

then, in a second terminal (same virtualenv as the server):

    python scripts/demo_concurrency_and_cache_cap.py

DEMO_BASE_URL (default http://127.0.0.1:8000) points this script at a
non-default host/port if needed.
"""

import asyncio
import io
import os
import sys
import time

import httpx
import pyarrow.parquet as pq

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")

SAMPLE_VINS = [
    "1HGCM82633A004352",
    "5YJ3E1EA6PF384836",
    "1FTFW1ET9DFC10312",
    "1C4RJFBG2FC625797",
    "5FNRL6H79NB021411",
    "1HD1KBM15FB620271",
    "1XPWD40X1ED215307",
]
CONCURRENCY_DEMO_VIN = SAMPLE_VINS[0]

# In production, CACHE_MAX_ROWS should be set much larger than this -- e.g.
# 100_000, or whatever comfortably covers the number of distinct VINs
# expected within one CACHE_TTL_SECONDS window. This value exists only so
# the eviction behavior is visible after a handful of requests in a live
# demo, not because it's a realistic production number.
RECOMMENDED_DEMO_CACHE_MAX_ROWS = 3
RECOMMENDED_DEMO_SWEEP_INTERVAL_SECONDS = 5

START_SERVER_HINT = (
    f"  CACHE_MAX_ROWS={RECOMMENDED_DEMO_CACHE_MAX_ROWS} "
    f"CACHE_SWEEP_INTERVAL_SECONDS={RECOMMENDED_DEMO_SWEEP_INTERVAL_SECONDS} "
    "uvicorn app.main:app --reload"
)


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def check_server_reachable(client: httpx.AsyncClient) -> None:
    try:
        await client.get("/", timeout=5.0)
    except httpx.ConnectError:
        print(f"Could not reach {BASE_URL}.")
        print()
        print("Start the server first -- with a small cache cap so the size-cap")
        print("demo is actually watchable -- then re-run this script:")
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


async def export_vins(client: httpx.AsyncClient) -> list[str]:
    response = await client.get("/export", timeout=10.0)
    response.raise_for_status()
    table = pq.read_table(io.BytesIO(response.content))
    return table.column("vin").to_pylist()


async def demo_concurrent_duplicate_lookup(client: httpx.AsyncClient) -> None:
    banner("Demo 1: concurrent duplicate lookup (no per-VIN lock)")
    print(f"VIN under test: {CONCURRENCY_DEMO_VIN}")

    print()
    print("Step 1 -- remove it first, so we start from a guaranteed cache miss.")
    deleted = await remove(client, CONCURRENCY_DEMO_VIN)
    print(f"  POST /remove -> deleted={deleted}")

    print()
    print("Step 2 -- fire TWO POST /lookup calls for that SAME VIN at the same")
    print("time (asyncio.gather), timing each one.")
    (result_a, elapsed_a), (result_b, elapsed_b) = await asyncio.gather(
        lookup(client, CONCURRENCY_DEMO_VIN),
        lookup(client, CONCURRENCY_DEMO_VIN),
    )
    print(f"  Request A: cached={result_a['cached']}  ({elapsed_a * 1000:.0f} ms)")
    print(f"  Request B: cached={result_b['cached']}  ({elapsed_b * 1000:.0f} ms)")

    print()
    if not result_a["cached"] and not result_b["cached"]:
        print("Both requests report cached=False, and both took a similar,")
        print("network-scale amount of time. That means BOTH independently")
        print("called vPIC over the network -- there is no lock serializing")
        print("concurrent misses for the same VIN. This is a documented,")
        print("deliberate tradeoff (see NOTES.md, 'No per-VIN lock on")
        print("cache-miss'): duplicate vPIC calls are possible on the very")
        print("first concurrent miss -- not a correctness bug, since the")
        print("second write just overwrites the first with identical data.")
    else:
        print("One request won the race and returned cached=True -- the")
        print("other's vPIC call finished, and its write committed, before")
        print("this request's own cache check ran. Re-run this script; the")
        print("miss-check is a sub-millisecond SQLite read racing a real")
        print("network call, so BOTH reporting cached=False is the far more")
        print("common outcome.")

    print()
    print("Step 3 -- look it up again, sequentially, to confirm normal")
    print("behavior resumes once the VIN is actually cached.")
    result_c, elapsed_c = await lookup(client, CONCURRENCY_DEMO_VIN)
    print(f"  POST /lookup -> cached={result_c['cached']}  ({elapsed_c * 1000:.0f} ms)")
    print("  (much faster than either Step 2 request -- served from SQLite, not vPIC)")


async def wait_for_eviction(client: httpx.AsyncClient, timeout: float) -> list[str]:
    """Wait out a full timeout window, always -- not just until the count first
    changes. A sweep tick firing mid-write (while lookups are still landing)
    can evict, then have more rows land afterward, settling on a count that's
    lower but not yet fully converged to the cap. Waiting a fixed window that
    comfortably covers a full sweep interval *after* all writes are done
    guarantees at least one clean tick sees the final, settled row set.
    """
    start = time.monotonic()
    vins = await export_vins(client)
    last_printed_count = None
    last_printed_at = -10.0
    while True:
        elapsed = time.monotonic() - start
        if len(vins) != last_printed_count or elapsed - last_printed_at >= 3.0:
            print(f"  ...{len(vins)} row(s) after {elapsed:.1f}s...")
            last_printed_count = len(vins)
            last_printed_at = elapsed
        if elapsed >= timeout:
            return vins
        await asyncio.sleep(1.0)
        vins = await export_vins(client)


async def demo_cache_size_cap(client: httpx.AsyncClient) -> None:
    banner("Demo 2: cache size cap (eviction)")
    print(
        f"This demo expects the server running with a small CACHE_MAX_ROWS "
        f"(recommended: {RECOMMENDED_DEMO_CACHE_MAX_ROWS}) -- see the top of"
    )
    print(
        "this script for the exact command. The production default (10,000)"
    )
    print(
        "can't be practically demoed live; this value exists purely to make"
    )
    print("eviction visible within a handful of requests.")

    print()
    print(f"Step 1 -- remove all {len(SAMPLE_VINS)} sample VINs, for a clean, repeatable start.")
    for vin in SAMPLE_VINS:
        await remove(client, vin)
    print("  done.")

    print()
    print(
        f"Step 2 -- look up all {len(SAMPLE_VINS)} sample VINs, one at a time, spaced"
    )
    print(
        "a second apart so each gets a distinct cached_at second (eviction"
    )
    print("order is 'oldest cached_at first').")
    for i, vin in enumerate(SAMPLE_VINS):
        result, elapsed = await lookup(client, vin)
        print(
            f"  [{i + 1}/{len(SAMPLE_VINS)}] {vin} -> "
            f"{result['make']} {result['model']} ({result['model_year']}), "
            f"cached={result['cached']}  ({elapsed * 1000:.0f} ms)"
        )
        if i < len(SAMPLE_VINS) - 1:
            await asyncio.sleep(1.1)

    print()
    right_after = await export_vins(client)
    print(f"Step 3 -- GET /export right now shows {len(right_after)} row(s): {right_after}")

    print()
    print("Step 4 -- waiting for the background maintenance task to run its")
    print("next sweep and enforce the row cap (the same _cache_maintenance_loop")
    print("from app/main.py that also purges expired rows). Waiting a full")
    print("window here on purpose, even if the count drops early -- a tick that")
    print("fires mid-lookup can evict before the last writes land, so we wait")
    print("out a window that guarantees one clean tick after all writes finish.")
    wait_seconds = RECOMMENDED_DEMO_SWEEP_INTERVAL_SECONDS * 2 + 5
    after = await wait_for_eviction(client, timeout=wait_seconds)

    print()
    if len(after) < len(SAMPLE_VINS):
        survived = set(after)
        evicted = [vin for vin in SAMPLE_VINS if vin not in survived]
        print(f"GET /export now shows {len(after)} row(s): {after}")
        print()
        print(f"Evicted (looked up earliest, {len(evicted)} row(s)): {evicted}")
        print(f"Survived (looked up most recently, {len(after)} row(s)): {list(after)}")
        print()
        print("The oldest rows by cached_at were evicted automatically -- no")
        print("/lookup, /remove, or /export call caused it directly. The")
        print("periodic background sweep did this on its own timer.")
    else:
        print(f"Row count is still {len(after)} -- no eviction observed within the timeout.")
        print()
        print("Make sure the server was started with a small CACHE_MAX_ROWS and")
        print("CACHE_SWEEP_INTERVAL_SECONDS:")
        print()
        print(START_SERVER_HINT)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        await check_server_reachable(client)
        try:
            await demo_concurrent_duplicate_lookup(client)
            await demo_cache_size_cap(client)
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
