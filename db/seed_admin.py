"""Create the first administrator account.

Everyone who signs up starts as a `member`, and only an admin can promote
anyone. So the very first admin has to be made here, by hand -- otherwise
there would be nobody able to grant the role.

    python db/seed_admin.py admin 'some strong password'
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.db import connect  # noqa: E402

username = sys.argv[1] if len(sys.argv) > 1 else "admin"
password = sys.argv[2] if len(sys.argv) > 2 else "admin-dev-password"

with connect() as conn:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (username, password_hash, full_name, role, is_active)
        VALUES (%s, %s, %s, 'admin', TRUE)
        ON CONFLICT (username) DO UPDATE
            SET password_hash = EXCLUDED.password_hash,
                role = 'admin',
                is_active = TRUE
        RETURNING id, (xmax = 0) AS was_inserted
        """,
        (username, hash_password(password), "Administrator"),
    )
    user_id, was_inserted = cur.fetchone()
    conn.commit()

print(f"admin ready: {username}  (id {user_id})")
if not was_inserted:
    # Signup is public, so someone could have already grabbed this
    # username as a plain member before this script ran. Force role and
    # password every time instead of trusting the conflict path, and say
    # so, so it isn't a silent surprise.
    print(f"NOTE: '{username}' already existed -- password reset and role "
          "forced to admin just now. If you didn't expect that, find out "
          "who created it.")
print("stored as a bcrypt hash — the password itself is never written down")
