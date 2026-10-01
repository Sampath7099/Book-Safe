"""L13 — prove the audit chain, then break it on purpose and watch it get caught.

    python tests/audit_tamper_demo.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402
from db.verify_audit import verify_chain  # noqa: E402

with connect() as conn:
    conn.execute(
        "UPDATE rooms SET capacity = capacity WHERE code = 'H101'")  # generates an audit row
    conn.commit()

    ok, broken_at = verify_chain(conn)
    print(f"before tampering : {'PASS -- chain intact' if ok else f'FAIL at id={broken_at}'}")

    target = conn.execute(
        "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute(
        "UPDATE audit_log SET row_data = row_data || '{\"capacity\": 99999}' "
        "WHERE id = %s", (target,))
    conn.commit()

    ok, broken_at = verify_chain(conn)
    verdict = "PASS -- tampering caught" if (not ok and broken_at == target) else "FAIL"
    print(f"after tampering  : {verdict} (flagged id={broken_at}, tampered id={target})")
