# ARCHIVED — the CAS single-sign-on version of auth.py, replaced in L7 by
# self-hosted accounts. Kept because the ticket-validation design (front-channel
# vs back-channel trust) is worth being able to explain.

"""Who are you, and what are you allowed to do?

TWO DOORS
---------
College people log in through CAS, the college's single sign-on. We never see
their password -- the college checks it and simply tells us who showed up.

Administrators log in with an email and password stored here as a bcrypt hash.
There is no sign-up form: admin accounts are created by hand.

WHAT CAS DOES AND DOESN'T DO
----------------------------
CAS answers "is this really Sampath?" -- authentication.
It does NOT answer "may Sampath cancel this booking?" -- that's authorization,
and it's ours to decide. CAS also doesn't know our app exists, so we keep our
own users table and create a row the first time someone logs in.
"""

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import bcrypt
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Load .env HERE too, not just in db.py. This module reads the environment at
# import time, and it gets imported before db.py — so without this line the
# settings below silently fall back to their defaults and .env is ignored.
load_dotenv()

# Point this at your college's CAS to go live. The local mock speaks the same
# protocol, so switching environments is this one line.
CAS_BASE_URL = os.environ.get("CAS_BASE_URL", "http://127.0.0.1:8100/cas")

# Where CAS sends people back to. Must match EXACTLY between the login redirect
# and the validation call, or CAS refuses the ticket -- it's part of what the
# ticket is signed against.
SERVICE_URL = os.environ.get("SERVICE_URL", "http://127.0.0.1:8000/auth/callback")

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
TOKEN_HOURS = 8

bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
#  passwords (admins only)
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """bcrypt, not SHA-256.

    Two reasons. bcrypt SALTS automatically, so two people with the same
    password get different hashes and one leaked hash can't be reused. And it
    is deliberately SLOW -- roughly 100ms -- which is irrelevant when you check
    one login but ruinous for an attacker checking billions.

    Hashing is one-way. Encryption can be undone; this cannot. That's the point.
    """
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
#  our own tokens
# ---------------------------------------------------------------------------

def make_token(user_id: int, role: str) -> str:
    """A signed note saying "this is user 42, a member, until 8pm".

    The user can read it -- a JWT is not encrypted -- but cannot change it
    without invalidating the signature, because they don't have JWT_SECRET.

    We need our own token because a CAS ticket is single-use and dies in
    seconds. It proves who arrived; it can't prove who's still here.
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "role": role,
         "iat": now, "exp": now + timedelta(hours=TOKEN_HOURS)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def read_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expired, log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid token")


# ---------------------------------------------------------------------------
#  CAS
# ---------------------------------------------------------------------------

def cas_login_url() -> str:
    """Where to send someone who needs to prove who they are."""
    return f"{CAS_BASE_URL}/login?{urlencode({'service': SERVICE_URL})}"


def validate_ticket(ticket: str) -> str | None:
    """Ask CAS directly whether a ticket is genuine. Returns the username.

    THIS IS THE SECURITY MODEL. The ticket arrives in the user's browser, so
    it is only a CLAIM -- anyone can type ?ticket=anything into the URL bar.
    We open our OWN connection to CAS, which the user cannot touch, and ask.

    Tickets are single-use and expire in seconds, so a stolen one is already
    dead by the time anyone finds it in a log.
    """
    url = f"{CAS_BASE_URL}/p3/serviceValidate"
    try:
        reply = httpx.get(
            url, params={"service": SERVICE_URL, "ticket": ticket}, timeout=10
        )
    except httpx.RequestError:
        raise HTTPException(503, "cannot reach the college login server")

    if reply.status_code != 200:
        return None

    # CAS answers in XML, namespaced under cas:
    ns = {"cas": "http://www.yale.edu/tp/cas"}
    try:
        root = ET.fromstring(reply.text)
    except ET.ParseError:
        return None

    user = root.find(".//cas:authenticationSuccess/cas:user", ns)
    return user.text.strip() if user is not None and user.text else None


def find_or_create_member(conn, cas_username: str) -> dict:
    """First login creates the account. There is no sign-up form.

    Role is hardcoded to 'member' -- an admin can never be created this way.
    The database enforces that too (see the login_method constraint).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, role FROM users WHERE cas_username = %s", (cas_username,)
        )
        row = cur.fetchone()

        if row is None:
            cur.execute(
                "INSERT INTO users (cas_username, full_name, role) "
                "VALUES (%s, %s, 'member') RETURNING id, role",
                (cas_username, cas_username),
            )
            row = cur.fetchone()

        cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row[0],))
    conn.commit()
    return {"id": row[0], "role": row[1], "cas_username": cas_username}


# ---------------------------------------------------------------------------
#  guards used by the endpoints
# ---------------------------------------------------------------------------

def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    """Every protected endpoint depends on this. No token -> 401."""
    if creds is None:
        raise HTTPException(401, "not logged in")
    claims = read_token(creds.credentials)
    return {"id": int(claims["sub"]), "role": claims["role"]}


def require_admin(user: dict = Depends(current_user)) -> dict:
    """401 means "I don't know who you are". 403 means "I know, and no"."""
    if user["role"] != "admin":
        raise HTTPException(403, "admins only")
    return user
