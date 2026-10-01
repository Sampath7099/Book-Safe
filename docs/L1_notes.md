# L1 — Decisions and interview notes

## What L1 delivers

A database with a shape, 14 real rooms, ~196k rows of history, a query that
answers "what's free?", and a booking function that **works for one user and is
broken for two**. No transactions, no locking, no API, no auth — those are
L2–L5.

---

## Decision 1 — How to represent time

| Option | Verdict |
|---|---|
| Fixed slot rows (pre-create a row per room/day/period) | Rejected |
| **`starts_at` + `ends_at` columns** | **Chosen** |
| A single native `tstzrange` column | Rejected |

Fixed slots are not a bad design — many real timetabling systems use them, and
they make this problem *easy*: overlap collapses into equality, so a plain
`UNIQUE` solves it and `SELECT ... FOR UPDATE` works directly because the row
already exists. They were rejected because you cannot book 14:00–16:30, they
require ~40,000 placeholder rows a year, and they remove the phantom problem
that is the point of the project.

A single range column makes the constraint elegant but is awkward everywhere
else: filtering on just the start is clumsy, JSON serialisation is ugly, and no
other database has range types. We keep two ordinary columns and *derive* the
range inside the constraint — the elegance without the cost.

## Decision 2 — Where the "no overlap" rule lives

A ladder, where each rung fails for a different and instructive reason:

| Approach | Why it fails |
|---|---|
| Application code only | Races under concurrency (this is L1, and L2's victim) |
| `UNIQUE` constraint | **Impossible.** `UNIQUE` compares for *equality*. 14:00–16:00 and 15:00–17:00 are different values that still conflict |
| `CHECK` constraint | **Impossible.** `CHECK` sees *one row at a time*. Overlap is a relationship *between* rows |
| A trigger | **Has the same race.** A trigger's `SELECT` is as blind as your Python's. Moving a check into the database does not make it concurrency-safe |
| **`EXCLUDE USING gist`** | **Chosen.** Enforced atomically at write time via an index |

**The caveat that matters:** `EXCLUDE` is PostgreSQL-only. MySQL and SQL Server
have nothing equivalent. Across most of the industry you *must* solve this in
application code with locking — which is why L2 is the portable answer and not
optional decoration.

## Decision 3 — Cancellation is a soft delete

`status = 'cancelled'` rather than `DELETE`. Keeps the audit trail (who
cancelled what, when) and allows undo.

The cost is real and we accept it knowingly: **every query must now remember
`WHERE status = 'confirmed'`**, and forgetting it is one of the most common
bugs in production systems. It is also why the exclusion constraint is
*partial* — without `WHERE status = 'confirmed'`, a cancelled booking would
block its room's slot forever.

## Decision 4 — Three tables (normalization)

If room details were denormalized into `bookings`, three things break:

- **Update anomaly** — H202's capacity changes; thousands of rows need updating
  and can disagree with each other mid-way.
- **Insert anomaly** — you cannot record that a room exists until someone books it.
- **Delete anomaly** — cancel the last booking for a room and the room vanishes.

The schema is cleanly 3NF with **no denormalization at all**.

Deliberate near-exception: `floor` is derivable from `code` ('H201' → floor 2).
Stored explicitly anyway, because parsing strings inside SQL is fragile and a
parse cannot be indexed cheaply.

(There is no `building_code` column. Everything is block H today; when a second
building appears, adding the column then is a small, honest change. Building it
now would be guessing.)

## Decision 5 — Raw SQL, no ORM

An ORM hides `BEGIN`, hides `FOR UPDATE`, and hides isolation levels — the
three things this project exists to demonstrate. You cannot learn transactions
through a layer designed to make you not think about them.

The cost we accept: **no migration history, no rollback, no schema versioning.**
`schema.sql` drops and recreates. Fine for one developer with no production
data; unacceptable for a real product, which would use Alembic or Flyway.

## Decision 6 — `NOT EXISTS`, not `NOT IN`

| Form | Issue |
|---|---|
| **`NOT EXISTS`** | **Chosen.** Reads like the English sentence; Postgres rewrites it into an anti-join |
| `LEFT JOIN ... WHERE b.id IS NULL` | Usually the same plan, but putting the time condition in `WHERE` instead of `ON` silently converts it to an INNER JOIN — a wrong answer with no error |
| `NOT IN (SELECT ...)` | **Dangerous.** If the subquery yields one NULL, the entire expression is NULL and you get **zero rows back, silently.** A wrong answer that looks like an empty result |

## Decision 7 — `GENERATED ALWAYS AS IDENTITY`, not `SERIAL`

`SERIAL` is an old Postgres-ism that creates a hidden sequence with awkward
ownership semantics. `IDENTITY` is the SQL standard. `ALWAYS` means the
application physically cannot supply its own id.

UUIDs would be the alternative — needed if ids were client-generated or
sharded, or to avoid leaking row counts (`/bookings/1834` tells an attacker
roughly how much traffic you have). Cost: 16 bytes vs 8, and random UUIDs
scatter B-tree inserts instead of appending. We took integers; the id-leak
point returns in L5.

---

## The deliberate bug

`book_naive()` in `app/booking.py` is **wrong on purpose**:

```
1. SELECT  — is the room free?
   <-- race window: nothing is locked here
2. INSERT  — book it.
```

Two requests can both pass step 1 before either reaches step 2.

**This is a phantom, not a lost update.** The conflicting row does not exist at
the moment of the check, which is why `SELECT ... FOR UPDATE` on `bookings`
cannot help — *you cannot lock a row that does not exist*. L2 fixes it by
locking the parent `rooms` row instead: lock the thing that exists in order to
protect the things that do not.

Right now the `EXCLUDE` constraint catches the collision at INSERT time and
raises `ExclusionViolation`. That is defence in depth working — but an
exception is not graceful handling, and if the constraint were removed (or the
database were MySQL) nothing would catch it at all. L2 demonstrates both.

---

## Likely interview questions

**Q. Why not just use a `UNIQUE` constraint?**
Because `UNIQUE` tests equality and booking conflicts are overlaps. Two
bookings 14:00–16:00 and 15:00–17:00 have different values and still conflict.
Equality is structurally the wrong operator, so `EXCLUDE ... WITH &&`
generalises `UNIQUE` from `=` to any operator.

**Q. Why did you need the `btree_gist` extension?**
The constraint mixes an equality test on `room_id` (an integer — a B-tree
operator class) with a range-overlap test (a GiST operator class) inside one
GiST index. `btree_gist` provides GiST operator classes for scalar types so
both can live in the same index.

**Q. Why `'[)'` bounds?**
Half-open: start inclusive, end exclusive. A lecture ending at 16:00 and one
starting at 16:00 must not conflict. With `'[]'` every back-to-back booking in
the building would collide. This is the fencepost bug of booking systems.

**Q. Why `CHECK (ends_at > starts_at)` and not `>=`?**
In Postgres a zero-length range is *empty*, and an empty range **overlaps
nothing**. Without the strict check, a 14:00→14:00 booking slips past the
exclusion constraint every single time, unlimited copies. A real hole closed by
one line.

**Q. Why isn't the 7-day booking horizon a `CHECK` constraint?**
It compares against `now()`, and Postgres forbids non-immutable functions
inside `CHECK` — the constraint would be re-evaluated inconsistently on dump,
restore, or `ALTER TABLE VALIDATE`. So it lives in application code. The 24-hour
duration rule *is* a `CHECK`, because it compares two columns to each other and
needs no clock.

**Q. Why does `rooms` have no index?**
14 rows. A sequential scan is genuinely the fastest plan and Postgres will
choose it regardless. An index is a claim about data volume.

**Q. Why store `floor` when it's in the room code?**
Parsing strings in SQL is fragile and cannot be indexed cheaply. Reality is
arbitrary anyway — if a room is renamed, the derived value silently breaks.

---

## Verify it yourself

```bash
docker exec -it booksafe-db psql -U booksafe -d booksafe -c '\d bookings'
docker exec -it booksafe-db psql -U booksafe -d booksafe -c 'SELECT code, floor, capacity FROM rooms ORDER BY code;'
./.venv/bin/python app/availability.py
./.venv/bin/python app/booking.py
```

Confirm the constraint refuses an overlap with no Python involved:

```sql
INSERT INTO bookings (room_id,user_id,starts_at,ends_at,party_size)
SELECT id,1,now()+interval '1 day',now()+interval '1 day 2 hours',10 FROM rooms WHERE code='H301';
-- run it twice; the second must fail with:
-- ERROR: conflicting key value violates exclusion constraint "no_overlapping_bookings"
```
