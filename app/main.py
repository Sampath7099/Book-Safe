"""The web API. Reads the request, calls book_safe(), turns the result into
an HTTP status code. The concurrency logic itself lives in app/booking.py.

    uvicorn app.main:app --reload
    then open http://localhost:8000/docs
"""

import asyncio
import os
from contextlib import asynccontextmanager, contextmanager

import anyio
from datetime import datetime

import pathlib

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from app.auth import (
    DEFAULT_JWT_SECRET,
    JWT_SECRET,
    ROLES,
    check_password,
    clean_username,
    current_user,
    hash_password,
    make_token,
    require_admin,
    require_staff,
    validate_signup,
)
from app.availability import find_available_rooms
from app.booking import BookingError, book_safe
from app.cache import (
    AVAILABILITY_TTL, ROOMS_KEY, ROOMS_TTL, availability_key, cache_get,
    cache_set, invalidate_availability, invalidate_rooms,
)
from app.chat import redis_listener
from app.chat import router as chat_router
from app.db import POOL_SIZE, pool
from app.ratelimit import by_ip, by_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to serve real traffic signed with the fallback secret -- that
    # string is sitting in source control, so booting with it silently
    # would mean anyone can forge an admin token.
    if JWT_SECRET == DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is not set. Put a real secret in .env before "
            "running the server -- see .env.example."
        )

    # Endpoints are sync and run in a worker thread pool. Give it more
    # threads than the DB connection pool so a burst queues on the
    # database, not on threads.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 80
    pool.open()
    listener = asyncio.create_task(redis_listener())
    yield
    listener.cancel()
    pool.close()


app = FastAPI(
    title="BookSafe",
    description="Room booking that never double-books.",
    lifespan=lifespan,
)
app.include_router(chat_router)


STATIC = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "index.html")


@contextmanager
def db():
    """Borrow a connection, use it, hand it back.

    Not a FastAPI Depends-with-yield -- that would hold a worker thread for
    the whole request instead of just the query, and under load that stalls
    everything. autocommit=True means the only real transaction is the
    explicit one inside book_safe, so a room lock is only ever held briefly.
    """
    with pool.connection() as conn:
        conn.autocommit = True
        yield conn


def rows(conn, sql, params=None):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one_row(conn, sql, params=None):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def lock_active_admins(conn):
    """Locks every active admin row, in a stable order, before any "is this
    the last admin" check.

    A lock on just the one user being demoted isn't enough: two different
    admins can be demoted in separate transactions at the same moment, each
    one's unlocked COUNT(*) still sees the OTHER admin as active, and both
    pass -- leaving zero admins. Locking the whole active-admin set here
    forces the second transaction to wait for the first to commit before it
    can even count, so it sees the real, up-to-date number.
    """
    conn.execute(
        "SELECT id FROM users WHERE role = 'admin' AND is_active "
        "ORDER BY id FOR UPDATE"
    )


# --- request/response models ------------------------------------------

class SignUp(BaseModel):
    username: str
    password: str
    full_name: str


class Login(BaseModel):
    username: str
    password: str


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class RoomUpdate(BaseModel):
    capacity: int | None = Field(default=None, gt=0)
    is_active: bool | None = None


class NewBooking(BaseModel):
    # No user_id here -- it comes from the login token, not the request body.
    room_id: int
    starts_at: datetime
    ends_at: datetime
    party_size: int = Field(gt=0)
    purpose: str | None = None


# book_safe reports failures as a BookingError (a string with a .code).
# This maps that code to an HTTP status.
STATUS_BY_CODE = {
    BookingError.NOT_FOUND: 404,
    BookingError.CONFLICT: 409,
    BookingError.UNAVAILABLE: 400,
    BookingError.INVALID: 400,
}


def status_for(reason: str) -> int:
    return STATUS_BY_CODE.get(getattr(reason, "code", None), 400)


# --- endpoints -----------------------------------------------------------

@app.post("/auth/signup", status_code=201, summary="Create an account")
def signup(request: SignUp):
    """Everyone starts as a member. Only an admin can promote someone.

    Signup is open to anyone who can reach the site -- fine on a private
    network, not fine if this were public.
    """
    error = validate_signup(request.username, request.password, request.full_name)
    if error:
        raise HTTPException(400, error)

    username = clean_username(request.username)
    with db() as conn:
        existing = one_row(conn, "SELECT id FROM users WHERE username = %s",
                           (username,))
        if existing:
            raise HTTPException(409, "that username is taken")

        user = one_row(
            conn,
            "INSERT INTO users (username, password_hash, full_name) "
            "VALUES (%s, %s, %s) RETURNING id, username, full_name, role",
            (username, hash_password(request.password), request.full_name.strip()),
        )
    return {"access_token": make_token(user["id"], user["role"]),
            "token_type": "bearer", "user": user}


@app.post("/auth/login", summary="Log in",
         dependencies=[Depends(by_ip("login", limit=10, window_seconds=60))])
def login(request: Login):
    """Same error either way (unknown user or wrong password) so nobody
    can use this to check which usernames exist."""
    with db() as conn:
        account = one_row(
            conn,
            "SELECT id, password_hash, role, is_active FROM users "
            "WHERE username = %s",
            (clean_username(request.username),),
        )
        if account is None or not check_password(request.password,
                                                 account["password_hash"]):
            raise HTTPException(401, "invalid username or password")
        if not account["is_active"]:
            raise HTTPException(403, "account deactivated")

        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s",
                     (account["id"],))

    return {"access_token": make_token(account["id"], account["role"]),
            "token_type": "bearer"}


@app.post("/auth/refresh", summary="Renew a token before it expires")
def refresh(user: dict = Depends(current_user)):
    """Swap a still-valid token for a fresh one. current_user already
    checks is_active, so a deactivated account can't use this to stay logged in."""
    return {"access_token": make_token(user["id"], user["role"]),
            "token_type": "bearer"}


@app.get("/auth/me", summary="Who am I?")
def whoami(user: dict = Depends(current_user)):
    with db() as conn:
        return one_row(
            conn,
            "SELECT id, username, full_name, role, is_active, created_at, "
            "last_login_at FROM users WHERE id = %s",
            (user["id"],),
        )


@app.get("/rooms", summary="List all rooms")
def list_rooms():
    cached = cache_get(ROOMS_KEY)
    if cached is not None:
        return cached
    with db() as conn:
        result = rows(conn, "SELECT id, code, floor, capacity FROM rooms "
                            "WHERE is_active ORDER BY code")
    cache_set(ROOMS_KEY, result, ROOMS_TTL)
    return result


@app.get("/availability", summary="Which rooms are free?")
def availability(
    starts_at: datetime,
    ends_at: datetime,
    min_capacity: int = 1,
):
    if ends_at <= starts_at:
        raise HTTPException(400, "end must be after start")

    key = availability_key(starts_at, ends_at, min_capacity)
    cached = cache_get(key)
    if cached is not None:
        return cached

    with db() as conn:
        result = find_available_rooms(conn, starts_at, ends_at, min_capacity)
    cache_set(key, result, AVAILABILITY_TTL)
    return result


@app.post("/bookings", status_code=201, summary="Book a room",
         dependencies=[Depends(by_user("bookings", limit=20, window_seconds=60))])
def create_booking(
    request: NewBooking,
    response: Response,
    user: dict = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Book a room.

    201 booked, 200 you already sent this exact request, 409 room's taken,
    404 no such room, 400 the request breaks a rule.

    The Idempotency-Key header protects against retries: if a client's
    connection drops after booking but before it sees the reply, retrying
    with the same key returns the original booking instead of a second one.
    """
    with db() as conn:
        if idempotency_key:
            seen = one_row(
                conn,
                "SELECT id, room_id, starts_at, ends_at, status FROM bookings "
                "WHERE user_id = %s AND idempotency_key = %s",
                (user["id"], idempotency_key),
            )
            if seen:
                response.status_code = 200
                return seen

        try:
            ok, reason, booking_id = book_safe(
                conn,
                request.room_id,
                user["id"],
                request.starts_at,
                request.ends_at,
                request.party_size,
                request.purpose,
                key=idempotency_key,
            )
        except psycopg.errors.UniqueViolation:
            # Two copies of the same retry raced each other and both got
            # past the SELECT above before either committed. The unique
            # index on the idempotency key catches it here instead.
            ok, reason, booking_id = False, None, None

        if not ok:
            if idempotency_key:
                # Could also be our own earlier attempt under this key,
                # committed while this request was waiting on the room
                # lock -- check before treating it as a real conflict.
                mine = one_row(
                    conn,
                    "SELECT id, room_id, starts_at, ends_at, status FROM bookings "
                    "WHERE user_id = %s AND idempotency_key = %s",
                    (user["id"], idempotency_key),
                )
                if mine:
                    response.status_code = 200
                    return mine
            raise HTTPException(status_for(reason), reason)

        invalidate_availability()
        return one_row(
            conn,
            "SELECT id, room_id, user_id, starts_at, ends_at, party_size, "
            "purpose, status FROM bookings WHERE id = %s",
            (booking_id,),
        )


@app.get("/bookings/{booking_id}", summary="Look up one booking")
def get_booking(booking_id: int, user: dict = Depends(current_user)):
    """Yours, or any booking if you're staff. 404 (not 403) if it's not
    yours, so we don't confirm the booking even exists to someone who
    shouldn't see it."""
    with db() as conn:
        booking = one_row(
            conn,
            "SELECT id, room_id, user_id, starts_at, ends_at, party_size, "
            "purpose, status FROM bookings WHERE id = %s",
            (booking_id,),
        )
        if booking is None:
            raise HTTPException(404, "no such booking")
        if booking["user_id"] != user["id"] and user["role"] == "member":
            raise HTTPException(404, "no such booking")
        return booking


@app.delete("/bookings/{booking_id}", status_code=204, summary="Cancel a booking")
def cancel_booking(
    booking_id: int,
    user: dict = Depends(current_user),
):
    """Cancel your own booking. Admins can cancel anyone's. This only flips
    the status -- the row stays, so the room frees up but the record survives."""
    with db() as conn:
        with conn.transaction():
            booking = one_row(
                conn,
                "SELECT status, user_id FROM bookings WHERE id = %s FOR UPDATE",
                (booking_id,),
            )
            if booking is None:
                raise HTTPException(404, "no such booking")
            if booking["user_id"] != user["id"] and user["role"] == "member":
                raise HTTPException(403, "that is not your booking")
            if booking["status"] == "cancelled":
                raise HTTPException(409, "already cancelled")

            conn.execute(
                "UPDATE bookings SET status='cancelled', cancelled_at=now() "
                "WHERE id = %s",
                (booking_id,),
            )
        invalidate_availability()
        return Response(status_code=204)


@app.patch("/rooms/{room_id}", summary="Edit a room (administrators only)")
def update_room(
    room_id: int,
    request: RoomUpdate,
    staff: dict = Depends(require_staff),
):
    """Bumps `version` (the optimistic-locking column) so a booking already
    in flight notices the room changed under it."""
    with db() as conn:
        with conn.transaction():
            room = one_row(conn, "SELECT id FROM rooms WHERE id=%s FOR UPDATE", (room_id,))
            if room is None:
                raise HTTPException(404, "no such room")
            conn.execute(
                "UPDATE rooms SET capacity = COALESCE(%s, capacity), "
                "is_active = COALESCE(%s, is_active), version = version + 1 "
                "WHERE id = %s",
                (request.capacity, request.is_active, room_id),
            )
        invalidate_rooms()
        invalidate_availability()
        return one_row(conn, "SELECT id, code, floor, capacity, is_active, version "
                             "FROM rooms WHERE id=%s", (room_id,))


@app.get("/me/bookings", summary="My upcoming bookings")
def my_bookings(user: dict = Depends(current_user)):
    with db() as conn:
        user_id = user["id"]
        return rows(
            conn,
            """
            SELECT b.id, r.code AS room, b.starts_at, b.ends_at, b.purpose, b.status
            FROM bookings b
            JOIN rooms r ON r.id = b.room_id
            WHERE b.user_id = %s
              AND b.status = 'confirmed'
              AND b.starts_at >= now()
            ORDER BY b.starts_at
            """,
            (user_id,),
        )


# --- account management (administrators only) ----------------------------

@app.get("/admin/users", summary="List accounts (admin)")
def list_users(admin: dict = Depends(require_admin), q: str | None = None,
               limit: int = 50):
    with db() as conn:
        return rows(
            conn,
            """
            SELECT u.id, u.username, u.full_name, u.role, u.is_active,
                   u.created_at, u.last_login_at,
                   count(b.id) FILTER (WHERE b.status='confirmed') AS bookings
            FROM users u
            LEFT JOIN bookings b ON b.user_id = u.id
            WHERE (%s IS NULL OR u.username ILIKE '%%' || %s || '%%')
            GROUP BY u.id
            ORDER BY u.id
            LIMIT %s
            """,
            (q, q, limit),
        )


@app.patch("/admin/users/{user_id}", summary="Change a role, or (de)activate")
def update_user(user_id: int, request: UserUpdate,
                admin: dict = Depends(require_admin)):
    """Promote, demote, deactivate, or reactivate an account.

    An admin can't change their own role/status (avoids locking themselves
    out), and the last remaining admin can't be demoted or deactivated by
    anyone. Since current_user re-reads role/is_active from the database,
    both changes apply on the account's very next request.
    """
    if request.role is not None and request.role not in ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(ROLES)}")

    if user_id == admin["id"]:
        raise HTTPException(403, "you cannot change your own role or status")

    with db() as conn:
        with conn.transaction():
            lock_active_admins(conn)

            target = one_row(
                conn,
                "SELECT id, username, role, is_active FROM users "
                "WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            if target is None:
                raise HTTPException(404, "no such user")

            losing_an_admin = target["role"] == "admin" and (
                (request.role is not None and request.role != "admin")
                or request.is_active is False
            )
            if losing_an_admin:
                remaining = one_row(
                    conn,
                    "SELECT count(*) AS n FROM users "
                    "WHERE role = 'admin' AND is_active AND id <> %s",
                    (user_id,),
                )
                if remaining["n"] == 0:
                    raise HTTPException(409, "that is the last admin")

            conn.execute(
                "UPDATE users SET role = COALESCE(%s, role), "
                "is_active = COALESCE(%s, is_active) WHERE id = %s",
                (request.role, request.is_active, user_id),
            )

        return one_row(
            conn,
            "SELECT id, username, full_name, role, is_active FROM users "
            "WHERE id = %s",
            (user_id,),
        )


@app.delete("/admin/users/{user_id}", summary="Erase an account")
def delete_user(user_id: int, admin: dict = Depends(require_admin),
                hard: bool = False):
    """Two ways to remove an account, because deletion isn't one question.

    Default: anonymise. Scrub the personal fields, keep the row, cancel
    upcoming bookings. Booking history is a fact about the building and
    survives even after the person's data doesn't.

    hard=true: actually delete the row. Only allowed if the account has no
    bookings or messages -- otherwise the foreign keys would have to cascade
    and take real history with them, so this refuses instead.
    """
    if user_id == admin["id"]:
        raise HTTPException(403, "you cannot delete your own account")

    with db() as conn:
        with conn.transaction():
            lock_active_admins(conn)

            target = one_row(
                conn, "SELECT id, role FROM users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            if target is None:
                raise HTTPException(404, "no such user")

            if target["role"] == "admin":
                remaining = one_row(
                    conn,
                    "SELECT count(*) AS n FROM users "
                    "WHERE role='admin' AND is_active AND id <> %s",
                    (user_id,),
                )
                if remaining["n"] == 0:
                    raise HTTPException(409, "that is the last admin")

            booking_count = one_row(
                conn, "SELECT count(*) AS n FROM bookings WHERE user_id = %s",
                (user_id,),
            )["n"]
            # messages.sender_id also references users(id), same as bookings --
            # has to be checked too or a hard-delete can crash on a stray FK.
            message_count = one_row(
                conn, "SELECT count(*) AS n FROM messages WHERE sender_id = %s",
                (user_id,),
            )["n"]

            if hard:
                if booking_count or message_count:
                    raise HTTPException(
                        409,
                        f"account has {booking_count} bookings and "
                        f"{message_count} chat messages; deleting the row "
                        "would destroy that history. Omit hard=true to anonymise "
                        "instead.",
                    )
                conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
                return {"deleted": user_id, "mode": "hard"}

            cancelled = conn.execute(
                "UPDATE bookings SET status='cancelled', cancelled_at=now() "
                "WHERE user_id = %s AND status='confirmed' AND starts_at > now()",
                (user_id,),
            ).rowcount

            conn.execute(
                """
                UPDATE users
                SET username = 'deleted-' || id,
                    full_name = 'Deleted User',
                    password_hash = 'ACCOUNT_DELETED_NO_LOGIN',
                    is_active = FALSE,
                    role = 'member',
                    deleted_at = now()
                WHERE id = %s
                """,
                (user_id,),
            )

    return {"deleted": user_id, "mode": "anonymised",
            "bookings_kept": booking_count,
            "upcoming_bookings_cancelled": cancelled}
