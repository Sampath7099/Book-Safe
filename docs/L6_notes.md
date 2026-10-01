# L6 — The proof

Everything before this was a claim. This turns it into evidence.

Setup: 14 rooms × 8 one-hour windows = **112 bookable room-hours**. Requests go
through the real API — web server, connection pool of 20, JWT auth, pessimistic
locking. Correctness is never judged by what the code prints: after every run we
go back to the database and ask **it** whether any two bookings overlap.

Reproduce: `python tests/loadtest.py` (API must be running)

---

## Phase 1 — 500 concurrent requests spread over 112 slots

```
201 Created             : 112
409 Conflict            : 388
anything else           : none
wall clock              : 6.10s   (82 req/s)
latency p50/p95/max     : 4947ms / 6017ms / 6036ms

>> DATABASE AUDIT <<
confirmed bookings      : 112   (expected 112)
overlapping pairs       : 0
verdict                 : PASS
```

Every slot that was asked for got booked exactly once. Every loser got a clean
409 — **not one 500**.

## Phase 2 — 500 concurrent requests for ONE slot

Maximum contention: all 500 want the same room, same hour.

```
201 Created             : 1
409 Conflict            : 499
anything else           : none
wall clock              : 6.98s   (72 req/s)

>> DATABASE AUDIT <<
confirmed bookings      : 1   (expected 1)
overlapping pairs       : 0
verdict                 : PASS
```

## Phase 3 — the three strategies under identical load

Same 500 bookings, same 20-connection budget, HTTP removed so this measures
*database* behaviour rather than web-server throughput.

| Strategy | Booked | Overlaps | Retries | Seconds | req/s |
|---|---|---|---|---|---|
| pessimistic (`FOR UPDATE`) | 112 | **0** | **0** | 0.36 | 1381 |
| optimistic (version column) | 112 | **0** | 68 | 0.37 | 1353 |
| serializable (Postgres SSI) | 112 | **0** | 163 | 0.51 | 980 |

Whole-table audit afterwards, across all **196,160** bookings: **0 overlapping
pairs.**

### Reading the table

At this contention level (500 requests over 112 slots — roughly 4.5 contenders
per slot) all three are correct and pessimistic is marginally fastest.

The **retry column is the real story**. Pessimistic does zero wasted work: you
queue, you get served, you're done. Optimistic threw away 68 attempts;
serializable threw away 163 and paid ~40% more wall-clock for it.

That is exactly the predicted trade-off. Optimistic strategies convert *waiting*
into *repeating*, which is a good deal only when collisions are rare. Here they
aren't rare, so the pessimistic lock wins — and it would win by more as
contention rises.

---

## The honest reading of the headline number

The defensible claim is:

> **500 concurrent booking requests against 112 room-hours: exactly 112
> succeeded, 388 were cleanly rejected with 409, zero double-bookings —
> verified by a post-run SQL audit.**

The claim that would get you caught is *"my system handles 500 concurrent
users."* It doesn't, and here's why:

- **500 in-flight HTTP requests ≠ 500 concurrent database transactions.** The
  real ceiling is the **connection pool: 20**. Everything else queues.
- The endpoints are synchronous, so they run in a worker-thread pool (raised to
  80). That's a second ceiling above the pool.
- Client and server are the **same laptop**, sharing a CPU. A real deployment
  separates them and the numbers change.
- Latency p50 of ~5s is *queueing*, not per-request work — each individual
  booking takes single-digit milliseconds. 500 requests through 20 connections
  means most spend their life waiting in line.

Report the pool size next to the number and the metric survives scrutiny.
Quote throughput without it and the first follow-up question exposes it.

---

## Three bugs this phase uncovered

The load test's real value wasn't the number. It was finding three bugs that
**nothing at lower concurrency could have surfaced**.

### 1. Thread-pool deadlock (the serious one)

Symptom: the server accepted exactly **40** requests, then froze. 360
`PoolTimeout`s, one booking created.

40 is anyio's default worker-thread count. The connection was being supplied by
a FastAPI **dependency with `yield`**, which FastAPI runs in the threadpool and
holds open for the whole request. So each request consumed **two** thread
tokens: one parked in the dependency, one for the endpoint body.

With 40 tokens: 40 requests each grabbed one token inside the dependency. 20 got
connections; 20 blocked waiting. But the 20 *holding* connections could not get
a second token to actually run — so they never finished, never released, and
nothing drained until the pool timeout fired.

**Fix:** take the connection inside the endpoint (`with db() as conn:`) instead
of via a yield-dependency. One thread per request, so the queue always drains.

The general lesson: **a resource pool behind a thread pool deadlocks whenever a
request needs two threads to make progress.**

### 2. Locks held for the whole HTTP request

The demos all set `conn.autocommit = True` before booking; `main.py` never did.
Without it, psycopg opens a transaction on the *first* statement and holds it
until the connection returns to the pool — so a room lock taken inside
`book_safe` was held across response serialisation too.

**Fix:** `conn.autocommit = True` on checkout, so the only transaction is the
explicit `with conn.transaction()`: lock, decide, insert, commit.

### 3. Retryable errors that weren't being caught

Two separate mistakes here.

First, `book_serializable` only caught `SerializationFailure`. Under load,
Postgres also raised **`DeadlockDetected` while checking the exclusion
constraint** — two transactions inserting into overlapping index ranges can
deadlock inside the constraint check itself. Uncaught, it killed threads.

Second, the fix for that was itself wrong. I assumed `SerializationFailure` and
`DeadlockDetected` both subclass `TransactionRollback` and caught the parent.
They don't — in psycopg3 both subclass `OperationalError` **directly**, so the
handler caught neither and every error escaped. Verified with `issubclass`
rather than assumed:

```python
RETRYABLE = (
    psycopg.errors.SerializationFailure,   # 40001
    psycopg.errors.DeadlockDetected,       # 40P01
)
```

Also found: with a retry budget of **5**, serializable booked only 111 of 112 —
one slot lost every contender to retry exhaustion. At **10** it reached 112. A
retry limit is a correctness parameter, not a safety net.

---

## Interview questions

**Q. What does your headline number actually prove?**
That the booking path is correct under sustained concurrent load through the
real HTTP stack, verified independently by querying the database afterwards
rather than trusting the application's own reporting. It does not prove
throughput at scale — the connection pool is 20, and client and server shared a
laptop.

**Q. Why audit in SQL instead of counting successful responses?**
Because the code under test would be reporting on itself. A self-join looking
for overlapping ranges is evidence from an independent source. In L2 that
distinction mattered: the naive version reported ten successes and *was* wrong.

**Q. How did you generate the concurrency?**
`asyncio` + `httpx`, 500 tasks created up front and released together by an
event, so they arrive as a burst rather than a ramp. Server-side concurrency is
bounded by the 20-connection pool.

**Q. Which strategy would you ship?**
Pessimistic. Zero retries, zero wasted work, predictable latency, and bookings
are short transactions on a hot row. Optimistic would win if load spread thinly
across many rooms; serializable is best when the invariants are too complex to
lock by hand.

**Q. Why is p50 latency 5 seconds if a booking takes milliseconds?**
That's queueing, not work. 500 requests through 20 connections means most of a
request's life is spent waiting for a connection. It's a measure of the
bottleneck, not of the booking logic.

**Q. What surprised you?**
That the load test found three bugs that were invisible at low concurrency — a
thread-pool deadlock, locks held far longer than intended, and an exception
class I had assumed wrong. Concurrency bugs don't appear gradually; they appear
at a threshold.
