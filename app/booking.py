"""Booking logic. One broken way to do it, three correct ways.

book_naive         checks then books, with a gap in between. BROKEN.
book_safe          locks the room row first (pessimistic). Used by the API.
book_optimistic    no lock, checks a version number instead.
book_serializable  no lock, lets Postgres's SERIALIZABLE mode catch it.

See tests/race_demo.py, tests/strategy_demo.py, tests/isolation_demo.py.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg

OVERLAP_CHECK = """
    SELECT 1 FROM bookings
    WHERE room_id = %s
      AND status  = 'confirmed'
      AND tstzrange(starts_at, ends_at, '[)') && tstzrange(%s, %s, '[)')
    LIMIT 1
"""

INSERT_BOOKING = """
    INSERT INTO bookings
        (room_id, user_id, starts_at, ends_at, party_size, purpose, idempotency_key)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id
"""

MAX_TRIES = 10

# These two Postgres errors both mean "try again, nothing is actually
# wrong" -- a serialization failure or a deadlock. They don't share a
# parent class in psycopg3, so both have to be listed.
RETRYABLE = (
    psycopg.errors.SerializationFailure,
    psycopg.errors.DeadlockDetected,
)


class Conflict(Exception):
    """Used internally by book_optimistic to trigger a retry."""


class BookingError(str):
    """A plain error string that also carries a status code.

    Works anywhere a string already worked (HTTPException, print, JSON),
    but main.py can check `.code` instead of matching on the text.
    """

    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    INVALID = "invalid"

    def __new__(cls, code, message):
        self = super().__new__(cls, message)
        self.code = code
        return self


# Tracks how many times a booking had to retry, so the demo scripts can
# compare strategies.
retries = 0
_retry_lock = threading.Lock()


def note_retry():
    global retries
    with _retry_lock:
        retries += 1


def reset_retries():
    global retries
    with _retry_lock:
        retries = 0


# --- shared helpers ---------------------------------------------------

def validate(starts_at, ends_at):
    """Rules that need "now", so they can't live in a CHECK constraint."""
    now = datetime.now(timezone.utc)
    if ends_at <= starts_at:
        return BookingError(BookingError.INVALID, "end must be after start")
    if ends_at - starts_at > timedelta(hours=24):
        return BookingError(BookingError.INVALID,
                            "booking cannot be longer than 24 hours")
    if starts_at < now:
        return BookingError(BookingError.INVALID, "cannot book in the past")
    if starts_at > now + timedelta(days=7):
        return BookingError(BookingError.INVALID,
                            "cannot book more than 7 days ahead")
    return None


def check_room(cur, room_id, party_size, lock=False):
    """Does the room exist and fit the group? lock=True also grabs FOR UPDATE."""
    query = "SELECT capacity, is_active, version FROM rooms WHERE id = %s"
    if lock:
        query += " FOR UPDATE"
    cur.execute(query, (room_id,))
    room = cur.fetchone()

    if room is None:
        return BookingError(BookingError.NOT_FOUND, "no such room"), None
    capacity, is_active, version = room
    if not is_active:
        return BookingError(BookingError.UNAVAILABLE, "room is not bookable"), None
    if party_size > capacity:
        return BookingError(
            BookingError.INVALID,
            f"{party_size} people won't fit — room holds {capacity}",
        ), None
    return None, version


def slot_taken(cur, room_id, starts_at, ends_at):
    cur.execute(OVERLAP_CHECK, (room_id, starts_at, ends_at))
    return cur.fetchone() is not None


def insert_booking(cur, room_id, user_id, starts_at, ends_at, party_size,
                   purpose, key):
    cur.execute(INSERT_BOOKING,
                (room_id, user_id, starts_at, ends_at, party_size, purpose, key))
    return cur.fetchone()[0]


CONFLICT_ERROR = BookingError(BookingError.CONFLICT, "room already booked for that time")


# --- 1. broken: check then book, no lock ------------------------------

def book_naive(conn, room_id, user_id, starts_at, ends_at, party_size,
               purpose=None, pause=0, key=None):
    """Unsafe on purpose. `pause` widens the gap so the race is easy to trigger."""
    error = validate(starts_at, ends_at)
    if error:
        return False, error, None

    cur = conn.cursor()
    error, _ = check_room(cur, room_id, party_size)
    if error:
        return False, error, None

    if slot_taken(cur, room_id, starts_at, ends_at):
        return False, CONFLICT_ERROR, None

    if pause:
        time.sleep(pause)  # the race window

    try:
        booking_id = insert_booking(cur, room_id, user_id, starts_at, ends_at,
                                    party_size, purpose, key)
        return True, "booked", booking_id
    except psycopg.errors.ExclusionViolation:
        conn.rollback()
        return False, "CRASH: database refused an overlap", None


# --- 2. pessimistic: lock the room, then decide ------------------------

def book_safe(conn, room_id, user_id, starts_at, ends_at, party_size,
              purpose=None, pause=0, key=None):
    """Locks the room row first. Anyone else booking it waits until we commit."""
    error = validate(starts_at, ends_at)
    if error:
        return False, error, None

    with conn.transaction():
        cur = conn.cursor()

        error, _ = check_room(cur, room_id, party_size, lock=True)
        if error:
            return False, error, None

        if slot_taken(cur, room_id, starts_at, ends_at):
            return False, CONFLICT_ERROR, None

        if pause:
            time.sleep(pause)

        booking_id = insert_booking(cur, room_id, user_id, starts_at, ends_at,
                                    party_size, purpose, key)
        return True, "booked", booking_id


# --- 3. optimistic: no lock, check a version number ---------------------

def book_optimistic(conn, room_id, user_id, starts_at, ends_at, party_size,
                    purpose=None, pause=0, key=None):
    """Reads the room's version, does the checks, then updates only if the
    version hasn't changed. If someone else booked in between, the update
    matches zero rows and we retry."""
    error = validate(starts_at, ends_at)
    if error:
        return False, error, None

    for attempt in range(MAX_TRIES):
        try:
            with conn.transaction():
                cur = conn.cursor()

                error, version = check_room(cur, room_id, party_size)
                if error:
                    return False, error, None

                if slot_taken(cur, room_id, starts_at, ends_at):
                    return False, CONFLICT_ERROR, None

                if pause:
                    time.sleep(pause)

                cur.execute(
                    "UPDATE rooms SET version = version + 1 "
                    "WHERE id = %s AND version = %s",
                    (room_id, version),
                )
                if cur.rowcount == 0:
                    raise Conflict  # someone else beat us, retry

                booking_id = insert_booking(cur, room_id, user_id, starts_at,
                                            ends_at, party_size, purpose, key)
                return True, "booked", booking_id

        except Conflict:
            note_retry()
            continue
        except RETRYABLE:
            note_retry()
            time.sleep(0.01 * (attempt + 1))
            continue
        except psycopg.errors.ExclusionViolation:
            return False, CONFLICT_ERROR, None

    return False, f"gave up after {MAX_TRIES} retries", None


# --- 4. serializable: let Postgres handle it -----------------------------

def book_serializable(conn, room_id, user_id, starts_at, ends_at, party_size,
                      purpose=None, pause=0, key=None):
    """Same code as book_naive, but run under SERIALIZABLE isolation, so
    Postgres itself aborts one side of any conflicting pair and we retry."""
    error = validate(starts_at, ends_at)
    if error:
        return False, error, None

    previous = conn.isolation_level
    conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
    try:
        for attempt in range(MAX_TRIES):
            try:
                with conn.transaction():
                    cur = conn.cursor()

                    error, _ = check_room(cur, room_id, party_size)
                    if error:
                        return False, error, None

                    if slot_taken(cur, room_id, starts_at, ends_at):
                        return False, CONFLICT_ERROR, None

                    if pause:
                        time.sleep(pause)

                    booking_id = insert_booking(cur, room_id, user_id, starts_at,
                                                ends_at, party_size, purpose, key)
                    return True, "booked", booking_id

            except RETRYABLE:
                note_retry()
                time.sleep(0.01 * (attempt + 1))
            except psycopg.errors.ExclusionViolation:
                return False, CONFLICT_ERROR, None

        return False, f"gave up after {MAX_TRIES} retries", None
    finally:
        conn.isolation_level = previous


if __name__ == "__main__":
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from app.db import connect

    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=2)

    with connect() as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT id FROM rooms WHERE code = 'H201'")
        room_id = cur.fetchone()[0]

        print(f"Booking H201 for {start:%d %b %H:%M}-{end:%H:%M}\n")
        ok, msg, booking_id = book_safe(conn, room_id, 1, start, end, 30, "robotics club")
        print(f"  1st try: {msg}  (id {booking_id})")
        ok, msg, booking_id = book_safe(conn, room_id, 2, start, end, 30, "drama club")
        print(f"  2nd try: {msg}")

        cur.execute("DELETE FROM bookings WHERE purpose IN ('robotics club','drama club')")
