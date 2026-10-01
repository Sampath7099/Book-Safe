"""L12 — Row-Level Security as a second layer of protection.

Connects directly as `booksafe_readonly`, skipping the app entirely, and
checks that Postgres still only shows each row to its owner.

    python db/setup_rls_demo_role.py   (first time, or after a schema reload)
    python tests/rls_demo.py           (Postgres must be running, schema loaded)
"""

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import DATABASE_URL, connect  # noqa: E402

load_dotenv()
RLS_DEMO_PASSWORD = os.environ.get("RLS_DEMO_PASSWORD")
if not RLS_DEMO_PASSWORD:
    sys.exit("RLS_DEMO_PASSWORD not set -- run python db/setup_rls_demo_role.py first")

READONLY_URL = DATABASE_URL.rsplit("@", 1)[-1]
READONLY_URL = f"postgresql://booksafe_readonly:{RLS_DEMO_PASSWORD}@{READONLY_URL}"

results = []


def check(what, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {what:<58} {got}  (want {want})")


with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='rls-test'")
    conn.commit()
    alice_id = conn.execute(
        "INSERT INTO users (username, password_hash, full_name) "
        "VALUES ('rls-alice', 'x', 'Alice') "
        "ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username "
        "RETURNING id").fetchone()[0]
    bob_id = conn.execute(
        "INSERT INTO users (username, password_hash, full_name) "
        "VALUES ('rls-bob', 'x', 'Bob') "
        "ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username "
        "RETURNING id").fetchone()[0]
    room_id = conn.execute("SELECT id FROM rooms WHERE code='H302'").fetchone()[0]
    start = (datetime.now(timezone.utc) + timedelta(days=4)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO bookings (room_id, user_id, starts_at, ends_at, "
        "party_size, purpose) VALUES (%s, %s, %s, %s, 5, 'rls-test')",
        (room_id, alice_id, start, start + timedelta(hours=1)))
    conn.commit()

print("\nDIRECT SQL AS A LOW-PRIVILEGE ROLE, NO APP LAYER INVOLVED")

with psycopg.connect(READONLY_URL) as ro:
    seen = ro.execute(
        "SELECT count(*) FROM bookings WHERE purpose='rls-test'").fetchone()[0]
    check("with no app.user_id set at all, rows visible", seen, 0)

with psycopg.connect(READONLY_URL) as ro:
    ro.execute("SELECT set_config('app.user_id', %s, false)", (str(bob_id),))
    seen = ro.execute(
        "SELECT count(*) FROM bookings WHERE purpose='rls-test'").fetchone()[0]
    check("claiming to be bob (not the owner), rows visible", seen, 0)

with psycopg.connect(READONLY_URL) as ro:
    ro.execute("SELECT set_config('app.user_id', %s, false)", (str(alice_id),))
    seen = ro.execute(
        "SELECT count(*) FROM bookings WHERE purpose='rls-test'").fetchone()[0]
    check("as alice (the actual owner), rows visible", seen, 1)

with connect() as conn:
    conn.execute("DELETE FROM bookings WHERE purpose='rls-test'")
    conn.execute("DELETE FROM users WHERE username IN ('rls-alice', 'rls-bob')")
    conn.commit()

print(f"\n{sum(results)}/{len(results)} checks passed")
