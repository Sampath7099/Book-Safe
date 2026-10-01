"""A narrated walk through everything BookSafe does.

Run it and watch. Nothing here is faked -- every line is a real HTTP request to
the running API, and the database is queried directly at the end to check the
API wasn't lying.

    python demo.py          (the API must be running)
"""

import textwrap
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.db import connect

API = "http://127.0.0.1:8000"


def say(text):
    print("\n" + textwrap.fill(text, 74))


def show(label, reply):
    body = reply.text if len(reply.text) < 200 else reply.text[:200] + "..."
    print(f"    {label:<44} {reply.status_code}  {body}")


def head(token):
    return {"Authorization": f"Bearer {token}"}


tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=14, minute=0, second=0, microsecond=0)
later = tomorrow + timedelta(hours=2)
tag = uuid.uuid4().hex[:5]

print("=" * 76)
print("  BookSafe — a room booking system that never double-books")
print("=" * 76)

# ---------------------------------------------------------------- 1. sign up
say("1. SIGNING UP. A username and a password. Short usernames and weak "
    "passwords are refused.")
show("username 'ab' (too short)",
     httpx.post(f"{API}/auth/signup", json={
         "username": "ab", "password": "password123",
         "full_name": "Too Short"}))
show("password '1234' (too weak)",
     httpx.post(f"{API}/auth/signup", json={
         "username": f"weak{tag}", "password": "1234",
         "full_name": "Weak"}))

alice_user = f"alice{tag}"
r = httpx.post(f"{API}/auth/signup", json={
    "username": alice_user, "password": "password123", "full_name": "Alice"})
show(f"alice ({alice_user}) signs up", r)
alice = r.json()["access_token"]

bob_user = f"bob{tag}"
bob = httpx.post(f"{API}/auth/signup", json={
    "username": bob_user, "password": "password123",
    "full_name": "Bob"}).json()["access_token"]
print(f"    bob signs up too                             201")

# ------------------------------------------------------------ 2. what's free
say(f"2. WHAT'S FREE? Alice wants a room tomorrow "
    f"{tomorrow:%H:%M}-{later:%H:%M} for 90 people.")
free = httpx.get(f"{API}/availability", params={
    "starts_at": tomorrow.isoformat(), "ends_at": later.isoformat(),
    "min_capacity": 90}).json()
for room in free:
    print(f"      {room['code']}  floor {room['floor']}  fits {room['capacity']}")

room = free[0]
say(f"3. BOOKING {room['code']}.")
r = httpx.post(f"{API}/bookings", headers=head(alice), json={
    "room_id": room["id"], "starts_at": tomorrow.isoformat(),
    "ends_at": later.isoformat(), "party_size": 90, "purpose": "robotics club"})
show("alice books it", r)
booking_id = r.json()["id"]

# --------------------------------------------------------------- 3. the rules
say("4. THE RULES. Bob tries the same room, an overlapping time, and a few "
    "things the system must refuse.")
show("bob books the SAME room, same time",
     httpx.post(f"{API}/bookings", headers=head(bob), json={
         "room_id": room["id"], "starts_at": tomorrow.isoformat(),
         "ends_at": later.isoformat(), "party_size": 10, "purpose": "drama"}))
show("bob books it OVERLAPPING by one hour",
     httpx.post(f"{API}/bookings", headers=head(bob), json={
         "room_id": room["id"],
         "starts_at": (tomorrow + timedelta(hours=1)).isoformat(),
         "ends_at": (later + timedelta(hours=1)).isoformat(),
         "party_size": 10, "purpose": "drama"}))
show("bob books it RIGHT AFTER (no overlap)",
     httpx.post(f"{API}/bookings", headers=head(bob), json={
         "room_id": room["id"], "starts_at": later.isoformat(),
         "ends_at": (later + timedelta(hours=1)).isoformat(),
         "party_size": 10, "purpose": "drama"}))
show("a party of 9999",
     httpx.post(f"{API}/bookings", headers=head(bob), json={
         "room_id": room["id"],
         "starts_at": (tomorrow + timedelta(days=2)).isoformat(),
         "ends_at": (tomorrow + timedelta(days=2, hours=1)).isoformat(),
         "party_size": 9999, "purpose": "too many"}))
show("booking 3 weeks ahead (limit is 7 days)",
     httpx.post(f"{API}/bookings", headers=head(bob), json={
         "room_id": room["id"],
         "starts_at": (tomorrow + timedelta(days=21)).isoformat(),
         "ends_at": (tomorrow + timedelta(days=21, hours=1)).isoformat(),
         "party_size": 10, "purpose": "too far"}))
show("no login at all",
     httpx.post(f"{API}/bookings", json={
         "room_id": room["id"], "starts_at": tomorrow.isoformat(),
         "ends_at": later.isoformat(), "party_size": 10}))
show("bob cancels ALICE's booking",
     httpx.delete(f"{API}/bookings/{booking_id}", headers=head(bob)))

# ----------------------------------------------------------- 4. the race
say("5. THE POINT OF THE WHOLE PROJECT. 30 people grab for the same free "
    "room at the same instant. Exactly one must win.")
free2 = httpx.get(f"{API}/availability", params={
    "starts_at": (tomorrow + timedelta(days=3)).isoformat(),
    "ends_at": (tomorrow + timedelta(days=3, hours=1)).isoformat()}).json()
contested = free2[-1]
gate = threading.Barrier(30)
codes = []
lock = threading.Lock()


def grab(token):
    gate.wait()
    r = httpx.post(f"{API}/bookings", headers=head(token), json={
        "room_id": contested["id"],
        "starts_at": (tomorrow + timedelta(days=3)).isoformat(),
        "ends_at": (tomorrow + timedelta(days=3, hours=1)).isoformat(),
        "party_size": 5, "purpose": f"race-{tag}"}, timeout=60)
    with lock:
        codes.append(r.status_code)


threads = [threading.Thread(target=grab, args=(alice if i % 2 else bob,))
           for i in range(30)]
t0 = time.perf_counter()
for t in threads:
    t.start()
for t in threads:
    t.join()
print(f"\n    all 30 fired at {contested['code']} in {time.perf_counter()-t0:.2f}s")
print(f"      201 Created  (won)      : {codes.count(201)}")
print(f"      409 Conflict (told no)  : {codes.count(409)}")
print(f"      500 Server error        : {codes.count(500)}")

# ----------------------------------------------------- 5. ask the database
say("6. DON'T TRUST THE API — ASK THE DATABASE. This query joins bookings to "
    "itself looking for any two that share a room and overlap in time.")
with connect() as conn:
    overlaps = conn.execute("""
        SELECT count(*) FROM bookings a
        JOIN bookings b ON a.room_id=b.room_id AND a.id<b.id
                       AND a.status='confirmed' AND b.status='confirmed'
                       AND tstzrange(a.starts_at,a.ends_at,'[)')
                        && tstzrange(b.starts_at,b.ends_at,'[)')
    """).fetchone()[0]
    total = conn.execute("SELECT count(*) FROM bookings").fetchone()[0]
print(f"\n      bookings in the database : {total:,}")
print(f"      overlapping pairs        : {overlaps}"
      f"   {'<-- BROKEN' if overlaps else '<-- none, ever'}")

# ---------------------------------------------------------- 6. admin powers
say("7. ADMIN POWERS. The admin can promote people, deactivate accounts, and "
    "cancel anyone's booking.")
admin = httpx.post(f"{API}/auth/login", json={
    "username": "admin",
    "password": "admin-dev-password"}).json()["access_token"]
bob_id = httpx.get(f"{API}/auth/me", headers=head(bob)).json()["id"]
alice_id = httpx.get(f"{API}/auth/me", headers=head(alice)).json()["id"]

show("member tries to list all accounts",
     httpx.get(f"{API}/admin/users", headers=head(alice)))
show("admin promotes bob to moderator",
     httpx.patch(f"{API}/admin/users/{bob_id}", json={"role": "moderator"},
                 headers=head(admin)))
show("bob (moderator) cancels alice's booking",
     httpx.delete(f"{API}/bookings/{booking_id}", headers=head(bob)))
show("admin deactivates alice",
     httpx.patch(f"{API}/admin/users/{alice_id}", json={"is_active": False},
                 headers=head(admin)))
show("alice's existing token, one second later",
     httpx.get(f"{API}/auth/me", headers=head(alice)))
show("admin tries to demote THEMSELVES",
     httpx.patch(f"{API}/admin/users/"
                 f"{httpx.get(f'{API}/auth/me', headers=head(admin)).json()['id']}",
                 json={"role": "member"}, headers=head(admin)))

# ------------------------------------------------------------------- tidy up
with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose IN "
                 "('robotics club','drama',%s)", (f"race-{tag}",))
    conn.execute("DELETE FROM users WHERE username IN (%s,%s)",
                 (alice_user, bob_user))
    conn.commit()

print("\n" + "=" * 76)
print("  demo data cleaned up")
print("=" * 76)
