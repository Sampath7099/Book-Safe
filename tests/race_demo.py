"""L2 — watch the double-booking bug happen, then watch it get fixed.

Ten people try to book the same room, at the same time, at the same moment.

    ACT 1  broken code, database rule ON   -> nobody double-books, but it's ugly
    ACT 2  broken code, database rule OFF  -> REAL double-bookings, no warning
    ACT 3  safe code,   database rule OFF  -> exactly one winner, no drama

Act 2 is the important one. It shows what your code guarantees on its own,
with nothing covering for it.

    python tests/race_demo.py
"""

import pathlib
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.booking import book_naive, book_safe  # noqa: E402
from app.db import connect  # noqa: E402

PEOPLE = 10
ROOM = "H303"
PAUSE = 0.05  # widen the race window so the bug shows up every time

# everyone wants exactly this slot
START = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
    hour=10, minute=0, second=0, microsecond=0
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
    """Run one statement and return the first value, if there is one."""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(statement, args)
        result = cur.fetchone() if cur.description else None
        conn.commit()
        return result[0] if result else None


def race(book, room_id):
    """Ten threads, each its own connection, all released at the same instant."""
    gate = threading.Barrier(PEOPLE)
    results = []
    guard = threading.Lock()

    def attempt(user_id):
        with connect() as conn:
            conn.autocommit = True
            gate.wait()  # nobody moves until all ten are ready
            ok, message, _ = book(conn, room_id, user_id, START, END, 10, pause=PAUSE)
        with guard:
            results.append((ok, message))

    threads = [threading.Thread(target=attempt, args=(i + 1,)) for i in range(PEOPLE)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def report(title, results, room_id):
    booked = sql(
        "SELECT count(*) FROM bookings WHERE room_id=%s AND starts_at=%s "
        "AND status='confirmed'",
        (room_id, START),
    )
    clashes = sql(COUNT_OVERLAPS, (room_id,))

    print(f"\n{title}")
    print("-" * len(title))
    for ok, message in sorted(results, key=lambda r: not r[0]):
        print(f"   {'OK  ' if ok else 'no  '} {message}")
    print(f"\n   bookings now in that slot : {booked}")
    print(f"   overlapping pairs found   : {clashes}", end="  ")
    print("<-- DOUBLE BOOKED" if clashes else "(clean)")


if __name__ == "__main__":
    room_id = sql("SELECT id FROM rooms WHERE code = %s", (ROOM,))
    clear = lambda: sql(
        "DELETE FROM bookings WHERE room_id=%s AND starts_at=%s", (room_id, START)
    )

    print(f"{PEOPLE} people racing for {ROOM} on {START:%d %b} at "
          f"{START:%H:%M}-{END:%H:%M}")

    try:
        # ACT 1 -- broken code, but the database rule is still guarding us
        clear()
        report("ACT 1  book_naive, database rule ON", race(book_naive, room_id), room_id)

        # ACT 2 -- take the safety net away and see what the code alone does
        clear()
        sql("ALTER TABLE bookings DROP CONSTRAINT no_overlapping_bookings")
        report("ACT 2  book_naive, database rule OFF", race(book_naive, room_id), room_id)

        # ACT 3 -- same missing safety net, but the code locks the room first
        clear()
        report("ACT 3  book_safe,  database rule OFF", race(book_safe, room_id), room_id)

    finally:
        # always put the safety net back, whatever happened above
        clear()
        sql(ADD_RULE)
        print("\n(database rule restored)")
