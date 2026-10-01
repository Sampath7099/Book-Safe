# L4 — The REST API

## What changed

Before L4, the only way to book a room was to run a Python file on one laptop.
BookSafe was a **library**. Now anything that speaks HTTP can use it — a phone,
a web page, another service. It's a **service**.

The API is deliberately thin: it reads the request, calls `book_safe()`, and
translates the answer into a status code. **All the concurrency thinking stays
in `app/booking.py`.** That separation is the point — the correctness lives in
one place and the API is a doorway to it, not a second implementation.

Run it:

```
uvicorn app.main:app --reload
```

Interactive docs, generated automatically from the code: <http://localhost:8000/docs>

## Endpoints

| Method | Path | Does |
|---|---|---|
| GET | `/rooms` | list all rooms |
| GET | `/availability?starts_at=&ends_at=&min_capacity=` | what's free |
| POST | `/bookings` | book a room |
| GET | `/bookings/{id}` | look up one booking |
| DELETE | `/bookings/{id}` | cancel (soft — flips status) |
| GET | `/users/{id}/bookings` | someone's upcoming bookings |

## Status codes, and why each one

| Code | When | Why not something else |
|---|---|---|
| **201** Created | booking made | 200 means "here's your answer"; 201 means "I made a new thing" |
| **200** OK | repeat of a request you already sent | nothing new was created, so not 201 |
| **204** No Content | cancelled | success, and there's nothing to send back |
| **400** Bad Request | breaks a rule (party too big, more than 7 days ahead) | the request itself is wrong |
| **404** Not Found | no such room or booking | — |
| **409** Conflict | room is taken | **the request was perfectly valid — the world disagreed.** This is the important one |
| **422** Unprocessable | malformed JSON, `party_size: 0` | FastAPI/Pydantic rejects it before our code runs |
| **500** | should never happen | a 500 means *we* broke, not the user |

**409 vs 400 is the distinction worth being able to defend.** A 4xx says "you did
something wrong", but "that room is booked" isn't the caller's fault — they
asked a reasonable question and lost a race. 409 says *"valid request, current
state won't allow it, try something else."* Sending 500 for a taken room (which
is what L1 did) tells the client to retry an unretryable thing.

## Verified behaviour

| Test | Result |
|---|---|
| Book a free room | 201 |
| Book the same room and time again | **409, not 500** |
| Book a nonexistent room | 404 |
| Party of 9999 in a 150-seat room | 400 |
| `party_size: 0` | 422 |
| Cancel | 204 |
| Cancel again | 409 |

---

## Idempotency keys

**The problem.** A phone sends a booking. The server books it. The reply is lost
on a flaky connection. The phone doesn't know whether it worked, so it retries —
and books the room twice. The user is charged twice, or holds two bookings.

Retrying is not optional: on an unreliable network, a client that never retries
loses requests. So the *server* has to make retrying safe.

**The fix.** The client generates a random key and sends it as a header:

```
Idempotency-Key: abc-123-retry
```

The server checks whether it has already seen that key from that user. If yes,
it returns **the original booking with 200** instead of making a second one.

Verified — same request sent three times:

```
attempt 1 -> HTTP 201   booking id 196241
attempt 2 -> HTTP 200   booking id 196241
attempt 3 -> HTTP 200   booking id 196241
rows actually created: 1
```

*Idempotent* means: doing it twice is the same as doing it once.

**Why there's also a unique index.** The lookup alone has the same race as
everything else in this project — two copies of the retry could both check, both
miss, and both book. So:

```sql
CREATE UNIQUE INDEX idx_bookings_idempotency
    ON bookings (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

The check is the fast path; the index is the guarantee. Same "policy in the app,
guarantee in the database" split as everywhere else.

Note some HTTP methods are idempotent by nature — `GET`, `PUT` and `DELETE` can
be repeated safely. `POST` cannot, which is exactly why it needs a key.

---

## Does the guarantee survive HTTP?

L2 and L3 proved the booking code is safe when called directly. But now there's
a web server in between, with a shared pool of connections and requests arriving
in any order. **An untested guarantee is a guess**, so `tests/api_race.py` fires
50 simultaneous requests at the same room and slot:

```
201 Created   : 1
409 Conflict  : 49
anything else : none

bookings in the database : 1
overlapping pairs        : 0
```

One winner, 49 polite refusals, **zero 500s**. The correctness held through the
front door.

---

## The connection pool

Every request needs a database connection. Opening a fresh one per request costs
more than most queries, and Postgres only allows ~100 at a time — so 500
concurrent users would exhaust the server outright.

Instead the app keeps a **pool** of 20 reusable connections. A request borrows
one, uses it, hands it back.

This is why L6's headline number needs care: **500 in-flight HTTP requests is not
500 concurrent database transactions.** The pool size is the real ceiling, and
reporting it honestly is the difference between a defensible metric and one that
falls apart under a follow-up question.

---

## A bug worth keeping in the notes

The first version set `conn.row_factory = dict_row` so rows would serialise into
neat JSON. That setting applies to **every** cursor on the connection —
including the ones inside `booking.py`, which expect plain tuples. So
`capacity, is_active, version = room` unpacked a dict, and Python hands you the
**keys**. `capacity` became the string `"capacity"`, and `party_size > capacity`
died with `'>' not supported between instances of 'int' and 'str'`.

A textbook **leaky abstraction**: a presentation preference in the API silently
changed how the core logic read its data. Fixed by asking for dicts *per query*
in `main.py` rather than setting it connection-wide, so the two layers can't
interfere.

---

## Known rough edges (deliberate)

1. **`user_id` comes from the request body.** Anyone can book as anyone, and
   anyone can cancel anyone's booking. L5 replaces it with a login token.
2. **Error codes are derived from error *sentences*** (`status_for()` does
   substring matching). A shared set of error codes would be cleaner. Left
   visible rather than hidden.
3. **Only `book_safe` is exposed.** The optimistic and serializable strategies
   exist but aren't reachable over HTTP. L6 wires them up to compare under load.

---

## Interview questions

**Q. Why 409 and not 400 when a room is taken?**
400 means the request was malformed. This one was perfectly well formed — the
current state of the world just doesn't allow it. 409 Conflict says "valid
request, wrong moment." It also tells the client something useful: retrying the
identical request won't help, but a different room or time might.

**Q. What is idempotency and why does POST need help with it?**
Idempotent means repeating the operation has the same effect as doing it once.
GET, PUT and DELETE are naturally idempotent; POST creates something new each
time. Since clients must retry on unreliable networks, the server accepts a
client-supplied key and returns the original result for repeats.

**Q. Why keep the API thin?**
Correctness lives in one place. If booking logic were duplicated in the
controller, the API and the direct-Python path could drift and only one would be
correct. It also means L6 can load-test the same code path users hit.

**Q. Why a connection pool?**
Connection setup (TCP, auth, session state) costs more than a short query, and
Postgres caps concurrent connections around 100. A pool bounds concurrency and
reuses the expensive part.

**Q. What happens when all 20 pooled connections are busy?**
New requests wait for one to free up, and eventually time out. That's a
deliberate backpressure choice: queueing is better than knocking the database
over. It also means the pool size, not the web server, sets true concurrency.
