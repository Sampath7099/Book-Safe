"""Walks the audit hash chain and reports the first broken link, if any.

    python db/verify_audit.py

Each row's hash covers the previous row's hash plus its own data, so
editing any row in place -- through the app or straight in psql -- makes
that row's hash (and everything after it) fail to recompute.

There's one chain per table (bookings, rooms, users), not one shared chain,
so a room edit never has to wait behind a booking insert. This walks each
table's chain separately for the same reason.
"""

import hashlib
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402


def verify_chain(conn):
    """Returns (ok, first_broken_id_or_None)."""
    rows = conn.execute(
        "SELECT id, table_name, row_data::text, prev_hash, hash "
        "FROM audit_log ORDER BY id"
    ).fetchall()

    by_table = defaultdict(list)
    for row in rows:
        by_table[row[1]].append(row)

    for table_rows in by_table.values():
        expected_prev = None
        for row_id, _table, row_json, prev_hash, hash_ in table_rows:
            if prev_hash != expected_prev:
                return False, row_id  # someone rewired the chain itself
            recomputed = hashlib.sha256(
                ((prev_hash or "") + row_json).encode()
            ).hexdigest()
            if recomputed != hash_:
                return False, row_id  # someone edited this row's data in place
            expected_prev = hash_

    return True, None


if __name__ == "__main__":
    with connect() as conn:
        ok, broken_at = verify_chain(conn)
    if ok:
        print("chain intact -- every link recomputes correctly")
    else:
        print(f"TAMPERING DETECTED -- first broken link is audit_log.id={broken_at}")
        sys.exit(1)
