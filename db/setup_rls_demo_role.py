"""Turns on login for booksafe_readonly, with a fresh random password.

db/schema.sql creates this role with NOLOGIN -- schema.sql gets run
straight against the real database, so it can never contain a usable
credential. This script is the opt-in step: generates a password, sets it
on the role, and writes it to .env (gitignored) so tests/rls_demo.py can
pick it up. Re-run any time to rotate the password.

    python db/setup_rls_demo_role.py
"""

import pathlib
import secrets
import sys

from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"
ENV_KEY = "RLS_DEMO_PASSWORD"

password = secrets.token_urlsafe(24)

with connect() as conn:
    # ALTER ROLE doesn't accept a normal %s parameter for the password --
    # it's DDL, not a query -- so it's built as a safely-quoted SQL literal
    # instead of string-formatted in directly.
    conn.execute(
        sql.SQL("ALTER ROLE booksafe_readonly LOGIN PASSWORD {}")
        .format(sql.Literal(password))
    )
    conn.commit()

lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
lines = [line for line in lines if not line.startswith(f"{ENV_KEY}=")]
lines.append(f"{ENV_KEY}={password}")
ENV_FILE.write_text("\n".join(lines) + "\n")

print(f"booksafe_readonly can log in now -- password written to {ENV_FILE}")
