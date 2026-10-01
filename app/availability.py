"""Which rooms are free? Read-only — this file never changes anything."""

from psycopg.rows import dict_row


def find_available_rooms(conn, starts_at, ends_at, min_capacity=1):
    """Rooms big enough with nothing booked in that window.

    We only store what's booked, never what's free (free is unbounded),
    so this checks for the absence of an overlapping booking.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT r.id, r.code, r.floor, r.capacity
        FROM rooms r
        WHERE r.is_active
          AND r.capacity >= %s
          AND NOT EXISTS (
              SELECT 1 FROM bookings b
              WHERE b.room_id = r.id
                AND b.status  = 'confirmed'
                AND tstzrange(b.starts_at, b.ends_at, '[)')
                 && tstzrange(%s, %s, '[)')
          )
        ORDER BY r.capacity, r.code
        """,
        (min_capacity, starts_at, ends_at),
    )
    return cur.fetchall()


if __name__ == "__main__":
    import pathlib
    import sys
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from app.db import connect

    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=2)

    with connect() as conn:
        print(f"Rooms free {start:%d %b, %H:%M}-{end:%H:%M} that fit 50 people:\n")
        for room in find_available_rooms(conn, start, end, min_capacity=50):
            print(f"  {room['code']}   floor {room['floor']}   fits {room['capacity']}")
