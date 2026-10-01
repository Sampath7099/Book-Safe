"""L4 — does the no-double-booking guarantee survive over HTTP?

L2 and L3 proved the booking code is safe when called directly from Python.
But now there's a web server in between, with a pool of shared connections and
requests arriving in any order. A guarantee you haven't re-tested through the
real front door is a guess.

Fires 50 simultaneous HTTP requests for the same room and time.

    python tests/api_race.py        (the server must be running)
"""

import pathlib
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

API = "http://127.0.0.1:8000"
REQUESTS = 50
ROOM = "H203"

START = (datetime.now(timezone.utc) + timedelta(days=6)).replace(
    hour=16, minute=0, second=0, microsecond=0
)
END = START + timedelta(hours=2)


def sql(statement, args=None):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(statement, args)
        result = cur.fetchone() if cur.description else None
        conn.commit()
        return result[0] if result else None


def signup():
    username = f"race{uuid.uuid4().hex[:8]}"
    reply = httpx.post(f"{API}/auth/signup", json={
        "username": username, "password": "password123", "full_name": "Racer",
    })
    return username, reply.json()["access_token"]


room_id = sql("SELECT id FROM rooms WHERE code = %s", (ROOM,))
sql("DELETE FROM bookings WHERE room_id=%s AND starts_at=%s", (room_id, START))

# 50 real accounts, not one -- a signed-in user has to pass the rate limiter
# on /bookings (20/min each), and the point here is 50 different PEOPLE
# racing for one room, not one person retrying 50 times.
usernames, tokens = zip(*(signup() for _ in range(REQUESTS)))

gate = threading.Barrier(REQUESTS)
codes = []
guard = threading.Lock()


def attempt(token):
    body = {
        "room_id": room_id,
        "starts_at": START.isoformat(),
        "ends_at": END.isoformat(),
        "party_size": 10,
        "purpose": "api-race",
    }
    gate.wait()  # all 50 released at once
    try:
        code = httpx.post(
            f"{API}/bookings", json=body, timeout=30,
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
    except Exception as exc:
        code = type(exc).__name__
    with guard:
        codes.append(code)


threads = [threading.Thread(target=attempt, args=(token,)) for token in tokens]
for t in threads:
    t.start()
for t in threads:
    t.join()

created = sum(1 for c in codes if c == 201)
conflict = sum(1 for c in codes if c == 409)
other = [c for c in codes if c not in (201, 409)]

rows = sql(
    "SELECT count(*) FROM bookings WHERE room_id=%s AND starts_at=%s "
    "AND status='confirmed'",
    (room_id, START),
)
overlaps = sql(
    """
    SELECT count(*) FROM bookings a
    JOIN bookings b ON a.room_id=b.room_id AND a.id<b.id
                   AND a.status='confirmed' AND b.status='confirmed'
                   AND tstzrange(a.starts_at,a.ends_at,'[)')
                    && tstzrange(b.starts_at,b.ends_at,'[)')
    WHERE a.room_id=%s
    """,
    (room_id,),
)

print(f"{REQUESTS} simultaneous POST /bookings for {ROOM} "
      f"on {START:%d %b} at {START:%H:%M}\n")
print(f"  201 Created   : {created}")
print(f"  409 Conflict  : {conflict}")
print(f"  anything else : {other if other else 'none'}")
print(f"\n  bookings in the database : {rows}")
print(f"  overlapping pairs        : {overlaps}", end="  ")
print("<-- FAILED" if overlaps else "(clean)")

sql("DELETE FROM bookings WHERE room_id=%s AND starts_at=%s", (room_id, START))
sql("DELETE FROM users WHERE username = ANY(%s)", (list(usernames),))
