"""L14 — chat works between two authorized parties and rejects a third.

    python tests/chat_demo.py       (API must be running, Redis too)
"""

import asyncio
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"


def signup(local):
    username = f"{local}{uuid.uuid4().hex[:6]}"
    reply = httpx.post(f"{API}/auth/signup",
                       json={"username": username, "password": "password123",
                             "full_name": local})
    return reply.json()["access_token"]


async def main():
    results = []

    def check(what, got, want):
        ok = got == want
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {what:<50} {got}  (want {want})")

    alice = signup("alice")
    bob = signup("bob")     # bystander -- no relationship to the booking

    start = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
        hour=11, minute=0, second=0, microsecond=0)
    with connect() as conn:
        room_id = conn.execute("SELECT id FROM rooms WHERE code='H201'").fetchone()[0]
        conn.execute("DELETE FROM messages WHERE booking_id IN "
                     "(SELECT id FROM bookings WHERE purpose='chat-test')")
        conn.execute("DELETE FROM bookings WHERE purpose='chat-test'")
        conn.commit()

    made = httpx.post(f"{API}/bookings",
                      headers={"Authorization": f"Bearer {alice}"},
                      json={"room_id": room_id, "starts_at": start.isoformat(),
                            "ends_at": (start + timedelta(hours=1)).isoformat(),
                            "party_size": 3, "purpose": "chat-test"})
    booking_id = made.json()["id"]

    # alice (owner) sends, admin (staff) receives on the same booking
    admin = httpx.post(f"{API}/auth/login",
                       json={"username": "admin",
                             "password": "admin-dev-password"}).json()["access_token"]

    # token goes through as a websocket subprotocol, not a query string
    url = f"{WS}/ws/bookings/{booking_id}"
    async with websockets.connect(url, subprotocols=[alice]) as a, \
              websockets.connect(url, subprotocols=[admin]) as m:
        await a.send("hello, is this room double-booked?")
        msg = await asyncio.wait_for(m.recv(), timeout=5)
        check("moderator receives alice's message", "hello" in msg, True)

    # rejection happens before accept(), so the client sees a failed
    # handshake instead of a connect-then-close
    try:
        async with websockets.connect(url, subprotocols=[bob]):
            check("unrelated bystander gets in", True, False)
    except websockets.exceptions.InvalidStatus as e:
        check("unrelated bystander rejected at handshake",
              e.response.status_code, 403)

    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE booking_id=%s", (booking_id,))
        conn.execute("DELETE FROM bookings WHERE purpose='chat-test'")
        conn.commit()

    print(f"\n{sum(results)}/{len(results)} checks passed")


asyncio.run(main())
