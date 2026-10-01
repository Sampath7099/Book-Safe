"""L3 — do all three correct strategies actually hold up?

Same test as L2: ten people, one room, one slot, all at once. But the
database's safety rule is switched OFF the whole time, so nothing is
covering for the code. Each strategy has to be right on its own.

    python tests/strategy_demo.py
"""

import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.booking import (  # noqa: E402
    book_naive,
    book_optimistic,
    book_safe,
    book_serializable,
)
from app.db import connect  # noqa: E402

PEOPLE = 10
ROOM = "H304"
PAUSE = 0.05

START = (datetime.now(timezone.utc) + timedelta(days=4)).replace(
    hour=15, minute=0, second=0, microsecond=0
)
END = START + timedelta(hours=2)

ADD_RULE = """
    ALTER TABLE bookings ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        room_id                             WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status = 'confirmed')
"""

COUNT_OVERLAPS = """
    SELECT count(*)
    FROM bookings a
    JOIN bookings b ON a.room_id = b.room_id
                   AND a.id < b.id
                   AND a.status = 'confirmed'
                   AND b.status = 'confirmed'
                   AND tstzrange(a.starts_at, a.ends_at, '[)')
                    && tstzrange(b.starts_at, b.ends_at, '[)')
    WHERE a.room_id = %s
"""


def sql(statement, args=None):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(statement, args)
        result = cur.fetchone() if cur.description else None
        conn.commit()
        return result[0] if result else None


def race(book, room_id):
    """Ten threads, ten connections, all released at the same instant."""
    gate = threading.Barrier(PEOPLE)
    wins = []
    guard = threading.Lock()

    def attempt(user_id):
        with connect() as conn:
            conn.autocommit = True
            gate.wait()
            ok, _, _ = book(conn, room_id, user_id, START, END, 10, pause=PAUSE)
        with guard:
            wins.append(ok)

    threads = [threading.Thread(target=attempt, args=(i + 1,)) for i in range(PEOPLE)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(wins), time.perf_counter() - started


if __name__ == "__main__":
    room_id = sql("SELECT id FROM rooms WHERE code = %s", (ROOM,))
    clear = lambda: sql(
        "DELETE FROM bookings WHERE room_id=%s AND starts_at=%s", (room_id, START)
    )

    strategies = [
        ("naive        (broken)", book_naive),
        ("pessimistic  (L2)", book_safe),
        ("optimistic   (version)", book_optimistic),
        ("serializable (Postgres)", book_serializable),
    ]

    print(f"{PEOPLE} people racing for {ROOM}, safety rule OFF\n")
    print(f"{'strategy':<26}{'booked':>8}{'overlaps':>10}{'seconds':>10}")
    print("-" * 54)

    try:
        clear()
        sql("ALTER TABLE bookings DROP CONSTRAINT no_overlapping_bookings")

        for name, book in strategies:
            clear()
            won, seconds = race(book, room_id)
            clashes = sql(COUNT_OVERLAPS, (room_id,))
            flag = "  <-- BROKEN" if clashes else ""
            print(f"{name:<26}{won:>8}{clashes:>10}{seconds:>10.2f}{flag}")

    finally:
        clear()
        sql(ADD_RULE)
        print("\n(safety rule restored)")
