"""L3 — does a stricter isolation level fix the bug on its own?

Two people book the same room at the same moment. The code is the naive
check-then-book, with no locks and no version column. The ONLY thing we change
between runs is Postgres's isolation level.

The safety rule is switched off, so nothing is covering for the code.

Expect:
    READ COMMITTED    both book      -> broken
    REPEATABLE READ   both book      -> STILL broken (this is the surprise)
    SERIALIZABLE      one is killed  -> fixed

    python tests/isolation_demo.py
"""

import pathlib
import sys
import threading
from datetime import datetime, timedelta, timezone

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

ROOM = "H302"
START = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
    hour=11, minute=0, second=0, microsecond=0
)
END = START + timedelta(hours=2)

LEVELS = [
    ("READ COMMITTED", psycopg.IsolationLevel.READ_COMMITTED),
    ("REPEATABLE READ", psycopg.IsolationLevel.REPEATABLE_READ),
    ("SERIALIZABLE", psycopg.IsolationLevel.SERIALIZABLE),
]

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


def two_people_book(level, room_id):
    """Both look, then both write. The barrier guarantees they interleave."""
    gate = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(user_id):
        result = ""
        with connect() as conn:
            conn.autocommit = True
            conn.isolation_level = level
            try:
                with conn.transaction():
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT 1 FROM bookings
                        WHERE room_id=%s AND status='confirmed'
                          AND tstzrange(starts_at, ends_at, '[)')
                           && tstzrange(%s, %s, '[)')
                        """,
                        (room_id, START, END),
                    )
                    free = cur.fetchone() is None

                    gate.wait()  # neither writes until both have looked

                    if free:
                        cur.execute(
                            """
                            INSERT INTO bookings
                                (room_id, user_id, starts_at, ends_at, party_size)
                            VALUES (%s, %s, %s, %s, 10)
                            """,
                            (room_id, user_id, START, END),
                        )
                        result = "booked it"
                    else:
                        result = "saw it was taken"
            except psycopg.errors.SerializationFailure:
                result = "KILLED by Postgres (error 40001)"
        with guard:
            outcomes.append(f"person {user_id}: {result}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(outcomes)


if __name__ == "__main__":
    room_id = sql("SELECT id FROM rooms WHERE code = %s", (ROOM,))
    clear = lambda: sql(
        "DELETE FROM bookings WHERE room_id=%s AND starts_at=%s", (room_id, START)
    )

    print(f"Two people booking {ROOM} on {START:%d %b} at {START:%H:%M}, "
          f"identical code, safety rule OFF\n")

    try:
        clear()
        sql("ALTER TABLE bookings DROP CONSTRAINT no_overlapping_bookings")

        for name, level in LEVELS:
            clear()
            outcomes = two_people_book(level, room_id)
            clashes = sql(COUNT_OVERLAPS, (room_id,))

            print(f"{name}")
            for line in outcomes:
                print(f"    {line}")
            verdict = "DOUBLE BOOKED" if clashes else "safe"
            print(f"    -> overlaps: {clashes}   {verdict}\n")

    finally:
        clear()
        sql(ADD_RULE)
        print("(safety rule restored)")
