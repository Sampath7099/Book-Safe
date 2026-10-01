"""Connects to Postgres.

connect()  a single fresh connection, for scripts and tests.
pool       a shared pool of connections, for the web server -- opening a
           new connection per request would exhaust Postgres (~100 max).

Anything running inside a request should use the pool, not connect().
"""

import os

import psycopg
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

POOL_SIZE = 20


def connect():
    """One new connection. Scripts and tests only."""
    return psycopg.connect(DATABASE_URL)


# min_size == max_size so all connections open up front instead of growing
# under a burst. open=False means this doesn't connect until pool.open() is
# called at server startup -- otherwise every script that imports this file
# would open 20 connections just by doing so.
pool = ConnectionPool(DATABASE_URL, min_size=POOL_SIZE, max_size=POOL_SIZE,
                      timeout=60, open=False)


if __name__ == "__main__":
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT version()")
        print(cur.fetchone()[0])
