"""Fill the database with fake users and two years of past bookings.

Why bother? Because an index only matters when there's a lot of data. With 14
rooms and 3 bookings, Postgres reads everything no matter what, and measuring
query speed teaches you nothing.

Two things to know:
  - All of it is in the PAST, so it never clashes with the tests in L2/L3.
  - Postgres generates the rows itself, from one instruction. They never exist
    in Python. That's why 196k rows take 11 seconds instead of several minutes.

    python db/seed_history.py
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.db import connect  # noqa: E402

N_USERS = 500
DAYS_BACK = 730
FILL_RATE = 0.80  # book 80% of the hourly slots, leave 20% as gaps

with connect() as conn:
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM rooms")
    if cur.fetchone()[0] == 0:
        sys.exit("No rooms yet — run db/seed_rooms.py first.")

    print(f"adding {N_USERS} users...")
    # Ordinary members. They all share one password hash: bcrypt is
    # deliberately slow (~100ms), so hashing 500 of them separately would take
    # a minute for no benefit in test data. Every test account's password is
    # "password123".
    shared_hash = hash_password("password123")
    cur.execute(
        """
        INSERT INTO users (username, password_hash, full_name, role)
        SELECT '21b' || lpad(i::text, 4, '0'),
               %s,
               'Test User ' || i,
               'member'
        FROM generate_series(1, %s) AS i
        ON CONFLICT (username) DO NOTHING
        """,
        (shared_hash, N_USERS),
    )

    # Only pick from member ids, not any user -- otherwise the admin
    # account could end up "owning" random fake bookings.
    cur.execute("SELECT array_agg(id) FROM users WHERE role = 'member'")
    member_ids = cur.fetchone()[0]

    print(f"generating bookings over the last {DAYS_BACK} days...")
    started = time.perf_counter()
    cur.execute(
        """
        INSERT INTO bookings
            (room_id, user_id, starts_at, ends_at, party_size, purpose)
        SELECT r.id,
               %(member_ids)s[1 + floor(random() * array_length(%(member_ids)s, 1))::int],
               hour,
               hour + INTERVAL '1 hour',
               1 + floor(random() * r.capacity)::int,
               (ARRAY['lecture','seminar','club meeting',
                      'lab session','exam','workshop'])[1 + floor(random()*6)::int]
        FROM rooms r
        CROSS JOIN generate_series(
            date_trunc('hour', now()) - (%(days_back)s || ' days')::interval,
            date_trunc('hour', now()) - INTERVAL '1 day',
            INTERVAL '1 hour'
        ) AS hour
        WHERE random() < %(fill_rate)s
        """,
        {"member_ids": member_ids, "days_back": DAYS_BACK, "fill_rate": FILL_RATE},
    )
    added = cur.rowcount
    elapsed = time.perf_counter() - started

    # Tell Postgres to re-measure the table, so it plans queries sensibly.
    cur.execute("ANALYZE bookings")
    conn.commit()

print(f"added {added:,} bookings in {elapsed:.1f}s")
