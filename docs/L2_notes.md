# L2 — The double-booking race, and the fix

## The result

Ten people, one room, one time slot, all at the same instant.

| | Code | DB rule | Succeeded | Overlapping pairs |
|---|---|---|---|---|
| **Act 1** | `book_naive` | ON | 1 | **0** — but 9 crashes |
| **Act 2** | `book_naive` | **OFF** | **10** | **45** — silent corruption |
| **Act 3** | `book_safe` | OFF | 1 | **0** — 9 polite refusals |

45 is `10 choose 2` — every booking overlapped every other booking. Nothing
errored. Nothing was logged. Ten clubs would have walked into H303 at 10am.

Reproduce: `python tests/race_demo.py`

---

## The bug

`book_naive` does **check, then act**:

```
1. SELECT — is this room free?
2. INSERT — book it.
```

With one user the gap between those lines is empty. With ten, it isn't:

```
t0   Alice  SELECT -> free
t1   Bob    SELECT -> free        <- both saw "free", and both were right
t2   Alice  INSERT
t3   Bob    INSERT                <- overlapping booking
```

Nobody made a mistake. The room *was* free when each of them looked. The answer
went stale between looking and acting.

## Why this is a *phantom*, not a lost update

This distinction is the heart of the layer.

A **lost update** is two transactions overwriting the same existing row. The fix
is easy: lock that row.

Here, the row we care about **does not exist yet** — it's the booking neither
person has made. You cannot lock a row that isn't there. This is a *phantom*:
a row that appears in a range you already looked at.

So `SELECT ... FROM bookings ... FOR UPDATE` is useless. There is nothing to
lock. Locking an empty result set locks nothing.

## The fix: lock the parent

`book_safe` locks the **room** row instead:

```sql
BEGIN;
SELECT capacity, is_active FROM rooms WHERE id = %s FOR UPDATE;
-- check for overlaps
-- insert
COMMIT;
```

The room row always exists, so it can always be locked. It becomes a **doorway**
that everyone wanting that room must queue at, one at a time. The first person
through holds it until they commit; the rest wait, and when they get in they can
see the booking that was just made.

The general pattern, worth remembering by name:

> **Lock the thing that exists, to protect the things that don't.**

Notice `book_safe` keeps the same artificial 50ms delay as `book_naive`. The
window is just as wide. It no longer matters, because everyone else is queued
behind the lock — which is the proof that the lock, not luck, is doing the work.

## Why the transaction is required, not optional

The lock is released the moment the transaction ends. Without `BEGIN ... COMMIT`
wrapping both steps, the lock would drop the instant the `SELECT` returned, and
the gap would reopen. **The lock and the transaction are one mechanism**, not two
features that happen to be used together.

---

## What Act 1 actually proves

Act 1 looks like a pass — zero double-bookings. It isn't.

The application logic was **just as wrong** as in Act 2. All ten threads passed
the check. Nine were stopped at the last instant by the `EXCLUDE` constraint,
which threw `ExclusionViolation`. Every one of those is an unhandled database
error — a 500 to a user in L4, not a "sorry, that room's taken."

So:

- **A constraint is a net, not a design.** It stops corruption; it does not
  produce correct behaviour.
- **And it's a Postgres luxury.** MySQL and SQL Server have no exclusion
  constraints at all. On those databases Act 1 *is* Act 2. Most real-world
  invariants (like "a user may hold at most 3 bookings") span rows or tables and
  cannot be declared at all.

That's why Act 3 is done with the rule still switched off: it proves the
application is correct *on its own*, with no help.

---

## How the race is forced

Three things make the bug reproduce every single run instead of occasionally:

1. **Ten separate connections.** One connection is one conversation. Two
   bookings racing must be two sessions — threads sharing a connection would
   just queue up politely and no race would exist.
2. **A barrier.** All ten threads stop and wait until every one of them is
   ready, then are released together. Without it they'd start staggered.
3. **A 50 ms pause** between check and insert, to widen the window.

The pause changes how *often* the bug appears. It does not create the bug — that
is a property of check-then-act. Under real load you don't need the pause; you
need traffic.

---

## Interview questions

**Q. Why not just rely on the unique/exclusion constraint?**
It prevents corruption but produces an exception, not a handled outcome — a 500
instead of a 409. It also only exists in Postgres, and most invariants can't be
expressed as a constraint at all. Correctness has to be in the application; the
constraint is defence in depth.

**Q. Why `SELECT ... FOR UPDATE` on `rooms` and not `bookings`?**
Because the conflicting booking doesn't exist yet — it's a phantom. You can't
lock a row that isn't there. The room row always exists, so it serves as the
serialisation point.

**Q. What exactly does `FOR UPDATE` do?**
Takes an exclusive row-level lock. Any other transaction that tries to
`SELECT ... FOR UPDATE` or `UPDATE` that same row blocks until this transaction
commits or rolls back. Plain `SELECT`s are unaffected — readers don't block.

**Q. Why does the transaction matter?**
The lock only lives as long as the transaction. `BEGIN ... COMMIT` is what makes
check and insert a single indivisible step. Without it, the lock is released
between the two and the race returns.

**Q. What's the downside of this fix?**
It **serialises every booking for that room**, even bookings for completely
different days that could never conflict. Under heavy load, requests queue. That
cost is what L3 addresses with optimistic locking.

**Q. How would you keep the lock cheap?**
Hold it for as little time as possible: lock, decide, insert, commit. Never do
slow work (an HTTP call, an email) inside the transaction. Our artificial 50ms
pause is exactly the anti-pattern, deliberately.
