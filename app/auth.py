"""Login, signup, and role checks.

Authentication is "who are you" (signup/login below).
Authorization is "are you allowed to do this" (the guards at the bottom).
"""

import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()

DEFAULT_JWT_SECRET = "dev-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)
JWT_ALGORITHM = "HS256"

# Short-lived: a stolen token is only good for 2 hours, not longer.
# /auth/refresh lets an active session renew without logging in again.
TOKEN_HOURS = 2

MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32
MIN_PASSWORD_LENGTH = 8

ROLES = ("member", "moderator", "admin")

bearer = HTTPBearer(auto_error=False)


# --- passwords -----------------------------------------------------------

def hash_password(plain: str) -> str:
    """bcrypt salts automatically and is slow on purpose, so it resists
    both rainbow tables and brute force."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def clean_username(username: str) -> str:
    """Store and compare usernames lowercase so "Alice" and "alice" are the same person."""
    return username.strip().lower()


def validate_signup(username: str, password: str, full_name: str) -> str | None:
    """Returns an error message, or None if the signup is fine."""
    username = clean_username(username)
    if not (MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH):
        return (f"username must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} "
                "characters")
    if not re.fullmatch(r"[a-z0-9._-]+", username):
        return "username may use only letters, numbers, dot, underscore, hyphen"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if not full_name.strip():
        return "name is required"
    return None


# --- tokens ----------------------------------------------------------------

def make_token(user_id: int, role: str) -> str:
    """A signed JWT. Anyone can read it, nobody can forge it without JWT_SECRET."""
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


# --- guards used by every protected endpoint -------------------------------

def fetch_account(user_id: int):
    """Look up a user's current row. Shared by current_user (below) and
    the chat websocket, which auths itself separately since a browser
    can't set headers on a websocket upgrade.

    Returns (id, username, role, is_active) or None.
    """
    from app.db import pool  # avoids a circular import

    with pool.connection() as conn:
        return conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    """Reads the token, then re-checks role/is_active in the database on
    every request. A JWT can't be revoked once issued, so this is what
    makes a deactivated account stop working right away instead of
    whenever the token happens to expire."""
    if creds is None:
        raise HTTPException(401, "not logged in")
    claims = read_token(creds.credentials)
    row = fetch_account(int(claims["sub"]))

    if row is None:
        raise HTTPException(401, "account no longer exists")
    if not row[3]:
        raise HTTPException(403, "account deactivated")

    return {"id": row[0], "username": row[1], "role": row[2]}


def require_role(*allowed: str):
    """401 means we don't know who you are. 403 means we do, and no."""
    def guard(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(403, f"requires one of: {', '.join(allowed)}")
        return user
    return guard


require_admin = require_role("admin")
require_staff = require_role("moderator", "admin")
