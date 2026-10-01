# L3 — Optimistic locking and isolation levels

## Experiment 1 — do all three correct strategies hold up?

Ten people, one room, one slot, all at once, **safety rule switched off** so
nothing covers for the code.

| Strategy | Booked | Overlaps | Seconds |
|---|---|---|---|
| naive (broken) | 10 | **45** | 0.14 |
| pessimistic — `FOR UPDATE` | 1 | 0 | 0.30 |
| optimistic — version column | 1 | 0 | 0.20 |
| serializable — Postgres SSI | 1 | 0 | 0.22 |

Reproduce: `python tests/strategy_demo.py`

All three are correct. The broken one is fastest, which is the entire reason
this bug ships to production so often — **wrong is cheap**.

---

## Experiment 2 — does a stricter isolation level fix it by itself?

Same naive check-then-book code, no locks, no version column. The *only* thing
changed between runs is the isolation level.

| Isolation level | Result | Overlaps |
|---|---|---|
| `READ COMMITTED` | both booked | **1 — broken** |
| `REPEATABLE READ` | both booked | **1 — still broken** |
| `SERIALIZABLE` | one killed with `40001` | **0 — safe** |

Reproduce: `python tests/isolation_demo.py`

### The middle row is the interesting one

Most people expect `REPEATABLE READ` to fix this. It doesn't, and the reason is
worth knowing.

`REPEATABLE READ` in Postgres is **snapshot isolation**: each transaction sees
the database frozen as it was when the transaction began. That reliably prevents
*non-repeatable reads* — read the same row twice, get the same answer.

But our two transactions never touch the same row. They read an *empty* result
and then each insert a **different, brand-new** row. There is no write-write
conflict, so there is nothing for snapshot isolation to detect. Both commit
happily.

This anomaly has a name: **write skew**. Two transactions each read some shared
state, each make a decision that is individually fine, and together they break
an invariant that neither one violated alone. Snapshot isolation permits it;
only true serializability forbids it.

### Why `SERIALIZABLE` catches it

Postgres implements **SSI** (Serializable Snapshot Isolation). It tracks what
each transaction read and what it wrote, and looks for dependency cycles — a
pattern that could not have arisen from running the transactions one at a time.
When it finds one, it aborts a transaction with SQLSTATE `40001`
(`serialization_failure`).

Crucially it **detects rather than prevents**, so the abort arrives late, often
at `COMMIT`. That means:

> **Any code using `SERIALIZABLE` must have a retry loop.** Without one you
> haven't made the system correct, you've made it randomly fail.

---

## The three strategies compared

### Pessimistic — `SELECT ... FOR UPDATE`

Assume a fight is likely, so take the room off the table before deciding.

- Correct at the first attempt; no retries, no wasted work
- Predictable latency
- **Serialises every booking for that room** — two people booking H304 for
  completely different weeks still queue behind each other
- Risk of deadlock if different code paths lock things in different orders

### Optimistic — version column

Assume a fight is unlikely. Note the room's version, think freely, then bump it
with `WHERE version = <what I saw>`. Zero rows changed means someone beat you,
so roll back and retry.

- Nobody blocks anybody
- Wonderful when conflicts are rare, which in a real college they are — most
  bookings are for different rooms at different times
- **Terrible under heavy contention**: everyone does the work, one wins, the
  rest throw it away and repeat
- Needs a schema column and a retry loop

An honest caveat: the conditional `UPDATE` still takes a brief row lock while it
runs. The real saving is that the lock is held for a much *shorter* time — only
from the update to the commit, not across the whole check.

### Serializable — let Postgres referee it

Write the naive code and turn the isolation level up.

- **The simplest code of the three** — no locks, no version column
- Catches anomalies you never thought of, including ones in code you write later
- Costs CPU and memory: Postgres tracks predicate locks for every transaction
- Aborts arrive late, sometimes at `COMMIT`, so the retry must redo everything
- Can produce *false positives* — aborting transactions that would have been fine

### Which would I actually ship?

**Pessimistic**, for this system. A room booking is a short transaction on a
single hot row, contention on popular rooms is real, and predictable latency
beats retry storms. The optimistic version wins if bookings spread thinly across
many rooms; `SERIALIZABLE` wins when correctness matters more than throughput
and the invariants are too complicated to lock by hand.

The point of building all three is being able to argue that, not to pick a
winner.

---

## Interview questions

**Q. What are the isolation levels and what does each prevent?**

| Level | Prevents | Still allows |
|---|---|---|
| Read Uncommitted | — | dirty reads (Postgres never actually does this; it maps to Read Committed) |
| Read Committed | dirty reads | non-repeatable reads, phantoms, write skew |
| Repeatable Read | + non-repeatable reads | **write skew** (and phantoms, per the standard) |
| Serializable | everything | nothing — but transactions may be aborted |

**Q. Why didn't `REPEATABLE READ` fix your bug?**
Because it's snapshot isolation. The two transactions inserted different rows,
so there was no write-write conflict to detect. That's write skew, and only
`SERIALIZABLE` forbids it.

**Q. What is write skew, in one sentence?**
Two transactions each read shared state and each make an individually valid
decision, which together violate an invariant neither broke alone.

**Q. Optimistic or pessimistic — how do you choose?**
By the conflict rate. Pessimistic wins under high contention because everyone
gets served once, in order. Optimistic wins under low contention because nobody
waits. Above roughly 10–20% conflicts, optimistic retries start costing more
than the queueing they avoided.

**Q. What is SQLSTATE 40001 and what must you do about it?**
`serialization_failure`. Postgres is telling you the transaction couldn't be
serialised and has been rolled back. It is **not** a bug and not a user error —
it is a normal, expected signal to retry the whole transaction. Any
`SERIALIZABLE` code without a retry loop is incomplete.

**Q. Why is Postgres's default `READ COMMITTED` if it allows all this?**
Throughput. Stricter levels cost tracking overhead and cause aborts. The default
optimises for the common case and leaves it to you to raise it where an
invariant needs it.

**Q. Doesn't the version column just reimplement locking?**
No — a lock makes others *wait*; a version makes others *find out afterwards*.
One prevents the conflict, the other detects it. The difference matters when
waiting is expensive or when the parties are on different machines, which is why
this same pattern (HTTP `ETag` / `If-Match`) is how the web does concurrency
control without any database at all.
