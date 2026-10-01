# L17 — Red team: attack the finished system

Same rule as L6: a claim only counts once something tried to break it and
failed. This goes back over L10-L14 and attacks each one, bug -> fix ->
retest, same style as [L6](L6_notes.md).

Reproduce: `python tests/<script>.py` for each case (API needs to be running).

---

## 1. IDOR — reading or cancelling someone else's booking

`GET /bookings/{id}` had no auth at all. Anyone could read any booking's
details just by guessing the ID.

**Fix:** `get_booking` in `app/main.py` now requires login and uses the
same 404-before-403 rule as cancel — a booking that isn't yours doesn't
even reveal that it exists.

**Retest:** `tests/idor_demo.py` — bob reading alice's booking (404),
cancelling it (403), an anonymous request (401), alice reading her own
(200). Also checks role escalation: a non-admin trying to PATCH someone's
role (403), and a `"role": "admin"` field smuggled into a signup request
(ignored, since that field doesn't exist on the model).

## 2. Reusing a token after deactivation

`current_user` re-reads role and is_active from the database on every
request, so deactivating someone takes effect immediately, not whenever
their token happens to expire.

**Retest:** `tests/security_demo.py`, the deactivation section — alice's
still-valid token stops working the moment she's deactivated, and works
again the moment she's reactivated.

## 3. Brute-forcing /login

**Fix:** a per-IP rate limit (10/min) on `/auth/login`, checked before the
password is even compared.

**Retest:** `tests/ratelimit_demo.py` — 20 rapid wrong-password attempts:
the first 10 get a real 401, everything after gets 429 with a
`Retry-After` header.

## 4. Row-level security catching a skipped app check

The app connects as `booksafe`, which owns the tables and bypasses RLS
(it needs to see every booking). `booksafe_readonly` is a weaker role RLS
actually applies to — standing in for a leaked or over-scoped credential.

**Retest:** `tests/rls_demo.py` connects directly as that role with raw
SQL, skipping the API entirely. With no user set, or claiming to be the
wrong user: zero rows visible either way. Only the real owner sees the row.

## 5. Tampering with the audit chain

**Retest:** `tests/audit_tamper_demo.py` — makes a real change, checks the
chain is intact, then edits an `audit_log` row directly with SQL (standing
in for a compromised DB credential) and checks again. `db/verify_audit.py`
points at the exact row that broke.

## 6. Unauthorized chat access

Chat only opens for a booking's owner, staff, or someone with another
booking for the same room and time.

**Retest:** `tests/chat_demo.py` — alice and admin exchange a message
fine. A third, unrelated user gets their handshake rejected (403) before
the connection is even accepted.

---

## What's out of scope here

- No chaos testing (L16) — nothing here kills Redis or Postgres mid-request.
- Chat access is scoped to real bookings only. A booking attempt that lost
  to a conflict is never stored (book_safe just returns 409), so there's no
  row to check "the other side" against. Chat covers the realistic case —
  staff resolving a double-booking, or two overlapping claims — without a
  separate log of failed attempts.
- Rate limiting is fixed-window, not sliding, so a burst right at the
  window edge can briefly get through at close to 2x the limit. Documented
  in `app/ratelimit.py`, not hidden.

---

## Interview questions

**Q. Does this actually handle 5,000 concurrent users?**
No, and I wouldn't claim it does. The load test proves correctness at 500
concurrent *requests* — which is a reasonable stand-in for a realistic
burst (everyone piling onto one popular slot at once) — verified by an
independent SQL audit, not the app's own reporting. It doesn't prove
throughput at literal 5,000-simultaneous-user scale, and I haven't run a
test that size because this runs on a single instance with a 20-connection
pool; the honest next step to actually test that would be a real load test
at higher concurrency, not extrapolating from 500.

**Q. Then why size it for 5,000 users at all?**
Because that's the size of the college it's meant for, not because every
one of them books at the same instant. Real usage is bursty — a popular
slot opening, a deadline for club room requests — not sustained 5,000-wide
load. The architecture (connection pooling, rate limiting per user/IP,
Redis caching that fails open) is sized for that burst pattern on one
instance, not for constant high concurrency, which is why L8/L9 (load
balancer, multiple instances, read replica) aren't built — they'd be
solving a problem this deployment doesn't have yet.

**Q. What's the actual bottleneck if load did increase?**
The Postgres connection pool, fixed at 20. The load test showed this
directly: 500 requests through 20 connections means most of a request's
latency is queueing for a connection, not the booking logic itself (which
runs in milliseconds). That's a known, deliberate ceiling — the fix is
either a bigger pool (bounded by Postgres's own connection limit) or
PgBouncer in front of multiple app instances, which is exactly what L8
would add if this needed to scale past one instance.

**Q. Why didn't you load-test chat or the audit log?**
Time and priority. They're functionally correct — verified with real
concurrent websocket clients and a real tamper-and-detect run — but
neither has been stress-tested at volume. Chat in particular is the part
I'd want load data on before calling it production-ready: message
persistence and Redis pub/sub delivery under hundreds of simultaneous
conversations is a different problem than two clients exchanging one message.

**Q. Why let the app run unlimited requests if Redis goes down, instead of blocking everything to be safe?**
Because Redis is a performance/abuse-prevention layer, not the source of
truth — Postgres is, and Postgres still enforces the one guarantee that
actually matters (no double-booking) with or without Redis. Failing closed
would mean a Redis blip takes down login and booking entirely over a
secondary system, which is a worse outcome than briefly unlimited rate
limits. I tested this directly: stopped Redis mid-run, and every route
kept returning correct results.

**Q. If you had to scale this for real tomorrow, what's the first thing you'd build?**
L8 — PgBouncer and multiple app instances behind a load balancer — since
everything past that point (read replicas, more aggressive caching,
observability) assumes horizontal scaling already exists. It's not built
because this deployment doesn't need it yet, not because it's hard; the
design (stateless JWT auth, no server-side session state) was chosen
specifically so that step wouldn't require rearchitecting anything.
