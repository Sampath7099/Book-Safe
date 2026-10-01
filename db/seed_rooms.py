"""Put the 14 real rooms into the database.

These are facts about a physical building, so they're typed out by hand.
There's no formula linking a room's code to its capacity — reality is
arbitrary, so we store the facts.

    python db/seed_rooms.py
"""

import pathlib
import sys

# lets this script find app/db.py, since it lives in a different folder
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

# code, floor, capacity
ROOMS = [
    ("H101", 1, 40),
    ("H102", 1, 60),
    ("H103", 1, 80),
    ("H104", 1, 100),
    ("H105", 1, 150),
    ("H201", 2, 40),
    ("H202", 2, 60),
    ("H203", 2, 80),
    ("H204", 2, 100),
    ("H205", 2, 150),
    ("H301", 3, 40),
    ("H302", 3, 60),
    ("H303", 3, 80),
    ("H304", 3, 100),
]

with connect() as conn:
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO rooms (code, floor, capacity)
        VALUES (%s, %s, %s)
        ON CONFLICT (code) DO NOTHING
        """,
        ROOMS,
    )
    conn.commit()

    cur.execute("SELECT count(*), sum(capacity) FROM rooms")
    count, seats = cur.fetchone()

print(f"{count} rooms, {seats} seats total")
