"""L7 — accounts, roles and admin powers. Try to break in, and fail.

Every check below should come back refused.

    python tests/security_demo.py       (API must be running)
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
    print(f"  {'PASS' if ok else 'FAIL'}  {what:<54} {got}  (want {want})")


def head(token):
    return {"Authorization": f"Bearer {token}"}


def signup(local):
    username = f"{local}{uuid.uuid4().hex[:6]}"
    reply = httpx.post(f"{API}/auth/signup",
                       json={"username": username, "password": "password123",
                             "full_name": local})
    return username, reply.json()["access_token"]


admin = httpx.post(f"{API}/auth/login",
                   json={"username": "admin",
                         "password": "admin-dev-password"}).json()["access_token"]
admin_id = httpx.get(f"{API}/auth/me", headers=head(admin)).json()["id"]

alice_user, alice = signup("alice")
bob_user, bob = signup("bob")
alice_id = httpx.get(f"{API}/auth/me", headers=head(alice)).json()["id"]
bob_id = httpx.get(f"{API}/auth/me", headers=head(bob)).json()["id"]

print("\nSIGN-UP RULES")
check("username too short (2 chars)",
      httpx.post(f"{API}/auth/signup",
                 json={"username": "hk", "password": "password123",
                       "full_name": "Hacker"}).status_code, 400)
check("password of 4 characters",
      httpx.post(f"{API}/auth/signup",
                 json={"username": f"x{uuid.uuid4().hex[:6]}",
                       "password": "1234", "full_name": "X"}).status_code, 400)
check("signing up with a username already taken",
      httpx.post(f"{API}/auth/signup",
                 json={"username": alice_user, "password": "password123",
                       "full_name": "Impostor"}).status_code, 409)
check("new account defaults to member",
      httpx.get(f"{API}/auth/me", headers=head(alice)).json()["role"], "member")

print("\nLOGIN")
check("right username, wrong password",
      httpx.post(f"{API}/auth/login",
                 json={"username": alice_user, "password": "nope"}).status_code, 401)
check("SQL injection in the login form",
      httpx.post(f"{API}/auth/login",
                 json={"username": "admin' OR '1'='1",
                       "password": "x"}).status_code, 401)
check("token hand-edited to claim role=admin",
      httpx.get(f"{API}/auth/me", headers=head(
          "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
          ".eyJzdWIiOiI1MDEiLCJyb2xlIjoiYWRtaW4iLCJleHAiOjk5OTk5OTk5OTl9"
          ".ZmFrZXNpZw")).status_code, 401)
check("no token at all", httpx.get(f"{API}/auth/me").status_code, 401)

start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
    hour=8, minute=0, second=0, microsecond=0)
with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='sec-test'")
    conn.commit()
    room_id = conn.execute("SELECT id FROM rooms WHERE code='H304'").fetchone()[0]
booking = {"room_id": room_id, "starts_at": start.isoformat(),
           "ends_at": (start + timedelta(hours=1)).isoformat(),
           "party_size": 5, "purpose": "sec-test"}

print("\nWHO MAY DO WHAT")
made = httpx.post(f"{API}/bookings", json=booking, headers=head(alice))
check("alice books a room", made.status_code, 201)
booking_id = made.json()["id"]
check("bob cancels alice's booking",
      httpx.delete(f"{API}/bookings/{booking_id}", headers=head(bob)).status_code, 403)
check("member edits a room",
      httpx.patch(f"{API}/rooms/{room_id}", json={"capacity": 1},
                  headers=head(alice)).status_code, 403)
check("member lists all accounts",
      httpx.get(f"{API}/admin/users", headers=head(alice)).status_code, 403)

print("\nADMIN POWERS")
check("admin promotes bob to moderator",
      httpx.patch(f"{API}/admin/users/{bob_id}", json={"role": "moderator"},
                  headers=head(admin)).status_code, 200)
check("bob (now moderator) cancels alice's booking",
      httpx.delete(f"{API}/bookings/{booking_id}", headers=head(bob)).status_code, 204)
check("moderator still cannot manage accounts",
      httpx.get(f"{API}/admin/users", headers=head(bob)).status_code, 403)
check("admin invents a role that doesn't exist",
      httpx.patch(f"{API}/admin/users/{bob_id}", json={"role": "wizard"},
                  headers=head(admin)).status_code, 400)

print("\nLOCKOUT GUARDS")
check("admin demotes THEMSELVES",
      httpx.patch(f"{API}/admin/users/{admin_id}", json={"role": "member"},
                  headers=head(admin)).status_code, 403)
check("admin deletes their OWN account",
      httpx.delete(f"{API}/admin/users/{admin_id}",
                   headers=head(admin)).status_code, 403)

print("\nDEACTIVATION TAKES EFFECT IMMEDIATELY")
check("admin deactivates alice",
      httpx.patch(f"{API}/admin/users/{alice_id}", json={"is_active": False},
                  headers=head(admin)).status_code, 200)
check("alice's EXISTING token now rejected",
      httpx.get(f"{API}/auth/me", headers=head(alice)).status_code, 403)
check("alice cannot log in again",
      httpx.post(f"{API}/auth/login",
                 json={"username": alice_user, "password": "password123"}).status_code, 403)
check("admin reactivates alice",
      httpx.patch(f"{API}/admin/users/{alice_id}", json={"is_active": True},
                  headers=head(admin)).status_code, 200)
check("alice works again with her ORIGINAL token",
      httpx.get(f"{API}/auth/me", headers=head(alice)).status_code, 200)

print("\nDELETION")
carol_user, carol = signup("carol")
carol_id = httpx.get(f"{API}/auth/me", headers=head(carol)).json()["id"]
httpx.post(f"{API}/bookings", headers=head(carol), json={
    **booking, "starts_at": (start + timedelta(hours=3)).isoformat(),
    "ends_at": (start + timedelta(hours=4)).isoformat()})
check("hard-delete someone who has bookings",
      httpx.delete(f"{API}/admin/users/{carol_id}?hard=true",
                   headers=head(admin)).status_code, 409)
erased = httpx.delete(f"{API}/admin/users/{carol_id}", headers=head(admin))
check("anonymise instead", erased.status_code, 200)
check("carol can no longer log in",
      httpx.post(f"{API}/auth/login",
                 json={"username": carol_user, "password": "password123"}).status_code, 401)

with connect() as conn:
    row = conn.execute("SELECT username, full_name, is_active FROM users "
                       "WHERE id=%s", (carol_id,)).fetchone()
    kept = conn.execute("SELECT count(*) FROM bookings WHERE user_id=%s",
                        (carol_id,)).fetchone()[0]
print(f"\n  her row is now: {row[0]}  |  {row[1]}  |  active={row[2]}")
print(f"  booking history kept: {kept} row(s) — the room WAS booked, that's a fact")

dave_user, dave = signup("dave")
dave_id = httpx.get(f"{API}/auth/me", headers=head(dave)).json()["id"]
check("hard-delete someone with NO bookings",
      httpx.delete(f"{API}/admin/users/{dave_id}?hard=true",
                   headers=head(admin)).status_code, 200)

with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='sec-test'")
    conn.execute("DELETE FROM bookings WHERE user_id IN "
                 "(SELECT id FROM users WHERE username ~ '^(alice|bob|carol|dave|x)' "
                 " OR username LIKE 'deleted-%%')")
    conn.execute("DELETE FROM users WHERE username ~ '^(alice|bob|carol|dave|x)' "
                 "OR username LIKE 'deleted-%%'")
    conn.commit()

print(f"\n{sum(results)}/{len(results)} checks passed")
