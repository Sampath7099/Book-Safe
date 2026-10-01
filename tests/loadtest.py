"""L6 — the proof. Everything up to now has been a claim.

Three phases:

  1  500 simultaneous HTTP bookings spread over 112 room-hours.
     The real production path: web server, connection pool, tokens, locking.

  2  500 simultaneous HTTP bookings for ONE room-hour.
     Maximum contention. Exactly one may win.

  3  The same load run against each concurrency strategy, measured.

Correctness is never judged by what the code prints. After every run we go
back to the database and ask IT whether any two bookings overlap.

    python tests/loadtest.py            (the API must be running)
"""

import asyncio
import pathlib
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import booking as booking_module  # noqa: E402
from app.auth import make_token  # noqa: E402
from app.booking import book_optimistic, book_safe, book_serializable  # noqa: E402
from app.db import DATABASE_URL, connect  # noqa: E402

API = "http://127.0.0.1:8000"
REQUESTS = 500
HOURS = list(range(9, 17))  # 8 one-hour windows per room
POOL_SIZE = 20  # must match app/main.py

DAY = (datetime.now(timezone.utc) + timedelta(days=6)).replace(
    hour=0, minute=0, second=0, microsecond=0
)

OVERLAP_AUDIT = """
    SELECT count(*)
    FROM bookings a
    JOIN bookings b ON a.room_id = b.room_id
                   AND a.id < b.id
                   AND a.status = 'confirmed'
                   AND b.status = 'confirmed'
                   AND tstzrange(a.starts_at, a.ends_at, '[)')
                    && tstzrange(b.starts_at, b.ends_at, '[)')
    WHERE a.starts_at >= %s AND a.starts_at < %s
"""


def sql(statement, args=None):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(statement, args)
        result = cur.fetchone() if cur.description else None
        conn.commit()
        return result[0] if result else None


def clear():
    sql("DELETE FROM bookings WHERE starts_at >= %s AND starts_at < %s",
        (DAY, DAY + timedelta(days=1)))


def audit():
    """Ask the database, not the code, whether anything went wrong."""
    return sql(OVERLAP_AUDIT, (DAY, DAY + timedelta(days=1)))


def booked():
    return sql(
        "SELECT count(*) FROM bookings WHERE status='confirmed' "
        "AND starts_at >= %s AND starts_at < %s",
        (DAY, DAY + timedelta(days=1)),
    )


# ---------------------------------------------------------------------------
#  build the workload
# ---------------------------------------------------------------------------

def load_rooms():
    with connect() as conn:
        return conn.execute("SELECT id FROM rooms ORDER BY id").fetchall()


def load_members(n):
    with connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM users WHERE role='member' ORDER BY id LIMIT %s", (n,)
            ).fetchall()
        ]


ROOM_IDS = [r[0] for r in load_rooms()]
MEMBERS = load_members(REQUESTS)
SLOTS = [(r, h) for r in ROOM_IDS for h in HOURS]  # 14 x 8 = 112


def build_workload(single_slot=False):
    """Decide up front who asks for what, so the expected answer is knowable."""
    random.seed(42)  # same workload every run, so results are comparable
    if single_slot:
        chosen = [SLOTS[0]] * REQUESTS
    else:
        chosen = [random.choice(SLOTS) for _ in range(REQUESTS)]
    work = []
    for i, (room_id, hour) in enumerate(chosen):
        work.append({
            "user_id": MEMBERS[i % len(MEMBERS)],
            "room_id": room_id,
            "starts_at": DAY + timedelta(hours=hour),
            "ends_at": DAY + timedelta(hours=hour + 1),
        })
    return work, len(set(chosen))


# ---------------------------------------------------------------------------
#  phase 1 & 2 — through the real HTTP API
# ---------------------------------------------------------------------------

async def fire_http(work):
    """500 requests in flight at once, through the actual web server."""
    limits = httpx.Limits(max_connections=REQUESTS, max_keepalive_connections=REQUESTS)
    async with httpx.AsyncClient(base_url=API, timeout=120, limits=limits) as client:
        ready = asyncio.Event()

        async def one(job):
            token = make_token(job["user_id"], "member")
            body = {
                "room_id": job["room_id"],
                "starts_at": job["starts_at"].isoformat(),
                "ends_at": job["ends_at"].isoformat(),
                "party_size": 5,
                "purpose": "loadtest",
            }
            await ready.wait()  # everyone waits, then all go together
            started = time.perf_counter()
            try:
                reply = await client.post(
                    "/bookings", json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                return reply.status_code, time.perf_counter() - started
            except Exception as exc:
                return type(exc).__name__, time.perf_counter() - started

        tasks = [asyncio.create_task(one(job)) for job in work]
        await asyncio.sleep(0.5)  # let all 500 tasks reach the gate
        wall = time.perf_counter()
        ready.set()
        results = await asyncio.gather(*tasks)
        return results, time.perf_counter() - wall


def percentile(values, p):
    values = sorted(values)
    return values[min(int(len(values) * p / 100), len(values) - 1)]


def http_phase(title, single_slot):
    clear()
    work, distinct = build_workload(single_slot)
    results, wall = asyncio.run(fire_http(work))

    codes = [c for c, _ in results]
    latencies = [t for _, t in results]
    created = codes.count(201)
    conflicts = codes.count(409)
    other = [c for c in codes if c not in (201, 409)]

    print(f"\n{title}")
    print("=" * len(title))
    print(f"  requests fired          : {REQUESTS} at once")
    print(f"  distinct slots asked for: {distinct}")
    print(f"  201 Created             : {created}")
    print(f"  409 Conflict            : {conflicts}")
    print(f"  anything else           : {other if other else 'none'}")
    print(f"  wall clock              : {wall:.2f}s  "
          f"({REQUESTS / wall:.0f} req/s)")
    print(f"  latency p50 / p95 / max : {percentile(latencies,50)*1000:.0f}ms / "
          f"{percentile(latencies,95)*1000:.0f}ms / {max(latencies)*1000:.0f}ms")
    print()
    print(f"  >> DATABASE AUDIT <<")
    print(f"  confirmed bookings      : {booked()}   (expected {distinct})")
    clashes = audit()
    print(f"  overlapping pairs       : {clashes}   "
          f"{'<-- FAILED' if clashes else '<-- ZERO'}")

    ok = clashes == 0 and created == distinct and not other
    print(f"  verdict                 : {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
#  phase 3 — the three strategies, same load, measured
# ---------------------------------------------------------------------------

def direct_phase():
    """Compare strategies without HTTP in the way.

    Same connection budget as the API (20), so the comparison is fair. Calling
    the functions directly removes web-server noise, which is the point: this
    measures DATABASE concurrency behaviour, not HTTP throughput.
    """
    from psycopg_pool import ConnectionPool

    print("\nPHASE 3 — strategy comparison")
    print("=" * 29)
    print(f"  {REQUESTS} bookings over {len(SLOTS)} room-hours, "
          f"{POOL_SIZE} database connections\n")
    print(f"  {'strategy':<16}{'booked':>8}{'overlaps':>10}"
          f"{'retries':>9}{'seconds':>9}{'req/s':>8}")
    print("  " + "-" * 58)

    work, distinct = build_workload(single_slot=False)
    rows = []

    for name, book in [("pessimistic", book_safe),
                       ("optimistic", book_optimistic),
                       ("serializable", book_serializable)]:
        clear()
        booking_module.reset_retries()
        wins = []
        guard = threading.Lock()
        pool = ConnectionPool(DATABASE_URL, min_size=POOL_SIZE,
                              max_size=POOL_SIZE, open=True)
        gate = threading.Barrier(REQUESTS)

        def attempt(job):
            gate.wait()
            with pool.connection() as conn:
                ok, _, _ = book(conn, job["room_id"], job["user_id"],
                                job["starts_at"], job["ends_at"], 5, "loadtest")
            with guard:
                wins.append(ok)

        threads = [threading.Thread(target=attempt, args=(j,)) for j in work]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
        pool.close()

        won, clashes, retried = sum(wins), audit(), booking_module.retries
        rows.append((name, won, clashes, retried, elapsed))
        print(f"  {name:<16}{won:>8}{clashes:>10}{retried:>9}"
              f"{elapsed:>9.2f}{REQUESTS/elapsed:>8.0f}")

    print(f"\n  every strategy should show {distinct} booked and 0 overlaps")
    return all(c == 0 and w == distinct for _, w, c, _, _ in rows)


if __name__ == "__main__":
    print(f"BookSafe load test — {len(ROOM_IDS)} rooms x {len(HOURS)} hours "
          f"= {len(SLOTS)} bookable room-hours on {DAY:%d %b %Y}")
    print(f"API connection pool: {POOL_SIZE}")

    passed = [
        http_phase("PHASE 1 — 500 concurrent HTTP requests, spread over 112 slots",
                   single_slot=False),
        http_phase("PHASE 2 — 500 concurrent HTTP requests, ALL for one slot",
                   single_slot=True),
        direct_phase(),
    ]

    clear()
    print(f"\n{'ALL PHASES PASSED' if all(passed) else 'SOMETHING FAILED'}")
