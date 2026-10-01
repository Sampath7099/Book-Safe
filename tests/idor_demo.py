"""L12 — try to read/cancel someone else's booking and escalate a role.
Same idea as race_demo.py: try the bad thing, check it gets refused.

    python tests/idor_demo.py       (API must be running)
"""

import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

API = "http://127.0.0.1:8000"
results = []


def check(what, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {what:<58} {got}  (want {want})")


def head(token):
    return {"Authorization": f"Bearer {token}"}


def signup(local):
    username = f"{local}{uuid.uuid4().hex[:6]}"
    reply = httpx.post(f"{API}/auth/signup",
                       json={"username": username, "password": "password123",
                             "full_name": local})
    return username, reply.json()["access_token"]


alice_user, alice = signup("alice")
bob_user, bob = signup("bob")

start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
    hour=10, minute=0, second=0, microsecond=0)
with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='idor-test'")
    conn.commit()
    room_id = conn.execute("SELECT id FROM rooms WHERE code='H303'").fetchone()[0]

made = httpx.post(f"{API}/bookings", headers=head(alice), json={
    "room_id": room_id, "starts_at": start.isoformat(),
    "ends_at": (start + timedelta(hours=1)).isoformat(),
    "party_size": 4, "purpose": "idor-test",
})
booking_id = made.json()["id"]

print("\nIDOR — reading and mutating someone else's booking")
check("bob reads alice's booking by guessing the id",
      httpx.get(f"{API}/bookings/{booking_id}", headers=head(bob)).status_code, 404)
check("an anonymous caller reads it at all",
      httpx.get(f"{API}/bookings/{booking_id}").status_code, 401)
check("bob cancels alice's booking",
      httpx.delete(f"{API}/bookings/{booking_id}", headers=head(bob)).status_code, 403)
check("alice can still read her own booking",
      httpx.get(f"{API}/bookings/{booking_id}", headers=head(alice)).status_code, 200)

print("\nROLE ESCALATION")
check("bob self-promotes to admin via a crafted PATCH",
      httpx.patch(f"{API}/admin/users/1", json={"role": "admin"},
                  headers=head(bob)).status_code, 403)
smuggled = httpx.post(f"{API}/auth/signup", json={
    "username": f"eve{uuid.uuid4().hex[:6]}", "password": "password123",
    "full_name": "Eve", "role": "admin",  # not a real field -- should be ignored
})
eve_token = smuggled.json()["access_token"]
check("role smuggled into the signup body is ignored",
      httpx.get(f"{API}/auth/me", headers=head(eve_token)).json()["role"], "member")

httpx.delete(f"{API}/bookings/{booking_id}", headers=head(alice))
with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='idor-test'")
    conn.execute("DELETE FROM users WHERE username IN (%s, %s) OR username LIKE 'eve%%'",
                 (alice_user, bob_user))
    conn.commit()

print(f"\n{sum(results)}/{len(results)} checks passed")
