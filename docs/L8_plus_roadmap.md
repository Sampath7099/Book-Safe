# BookSafe — L8 and beyond: from "correct on one node" to "correct, secured, and real-time at scale"

L0–L7 proved one thing extremely well: BookSafe never double-books a room, under real
concurrent load, on a single Postgres instance, verified independently rather than
self-reported. This roadmap does not touch that spine. Everything below either (a)
scales the same guarantee across multiple app instances and database nodes, (b)
closes real attack surfaces a determined adversary would actually go for, or (c) adds
one genuinely new, well-scoped subsystem (real-time chat) designed as its own
distributed-systems problem rather than bolted on naively.

No ML anywhere in this version. Everything here is system design, security, and
distributed systems — the capstone is "this survives being attacked and survives
being scaled," not "this has a model in it."

**Solo build, implemented by Claude Code, resume-first but built as if it could
actually run in production** — meaning: use the same tools a real deployment would
use (nginx, PgBouncer, Redis, Prometheus), even if the "production" here is a
docker-compose stack on your own machine. Nothing here is a toy stand-in that would
need to be rebuilt to become real; it's the real architecture, just not rented from a
cloud provider.

---

## Architecture target (end state)

```
                         ┌─────────────┐
                         │   nginx     │  (load balancer, TLS termination,
                         │ (L8)        │   rate-limit first line, L11)
                         └──────┬──────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        ┌───────────┐    ┌───────────┐    ┌───────────┐
        │ app inst 1 │    │ app inst 2 │    │ app inst 3 │   FastAPI, stateless JWT
        │ (booking + │    │ (booking + │    │ (booking + │   (L8), each holding a
        │  chat WS)  │    │  chat WS)  │    │  chat WS)  │   pool of connections to
        └─────┬──────┘    └─────┬──────┘    └─────┬──────┘   PgBouncer, not Postgres
              │                 │                 │           directly (L8)
              │      ┌──────────┴──────────┐      │
              │      │   Redis              │      │
              └─────▶│ - rate limit buckets │◀─────┘
                     │ - room/availability  │
                     │   cache (L10)         │
                     │ - Pub/Sub backplane   │
                     │   for chat (L14)      │
                     └──────────┬───────────┘
                                │
                         ┌──────┴──────┐
                         │  PgBouncer   │  (L8 — connection pooling
                         └──────┬──────┘   in front of Postgres)
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
             ┌─────────────┐        ┌─────────────┐
             │  Postgres    │──────▶│  Postgres    │   primary/replica,
             │  primary     │ repl. │  read replica│   consistency-aware
             │ (writes,     │       │ (availability│   routing (L9)
             │  bookings)   │       │  browse, chat │
             └─────────────┘        │  history)     │
                                     └─────────────┘

     Prometheus + Grafana scrape every component above (L15)
     Postgres primary also runs the hash-chained audit trigger (L13)
                and Row-Level Security policies (L12)
```

---

## L8 — Horizontal scaling: load balancer + multiple app instances + connection pooler

### Why this is a real, non-optional first step
Everything after this layer assumes more than one app instance exists — rate
limiting, chat pub/sub, and observability are all *harder and different* the moment
you're not a single process anymore, so this has to come first.

### What gets built
- **nginx** in front, load-balancing across N FastAPI containers (round-robin to
  start; note in the writeup that a smarter algorithm — least-connections — would
  matter more once request costs vary, e.g. a long-held chat WebSocket vs. a fast
  booking call).
- **Multiple app instances** via docker-compose (`app-1`, `app-2`, `app-3` — same
  image, different container). Your JWT-based stateless auth is exactly what makes
  this easy: no session affinity needed for plain HTTP.
- **PgBouncer** between the app instances and Postgres. This is the concrete fix for
  a real, easy-to-miss bug: your existing pool of 20 was *per instance*. Three
  instances × 20 = 60 direct connections to Postgres with no coordination between
  them. PgBouncer in transaction-pooling mode gives you one place that actually
  enforces the real ceiling, and is the honest answer to "what's your connection
  budget now that there isn't one app process."
- Health checks (nginx `upstream` health checks, or a simple `/healthz` endpoint each
  app instance exposes) so a crashed instance is routed around, not sent traffic.

### What to prove (in your L8_notes.md, matching your existing style)
Re-run your L6 load test, but now against the load-balanced stack. The headline
number changes shape: instead of "112 succeeded, 388 rejected, 0 overlaps," you now
also have to show **the same zero-overlap guarantee holds when three different
processes are racing for the same room**, not just three threads in one process —
this is a genuinely stronger claim than L6 made, and worth stating as an explicit
upgrade.

---

## L9 — Read replica with consistency-aware routing

### Why this is a real distributed-systems problem, not just "add more DB"
A naive read replica introduces a real bug: if a client checks availability against
the replica, and the replica is a few milliseconds behind the primary, they can be
told a slot is free when it was already just taken. Silently ignoring this and
hoping it's rare is exactly the kind of thing a capstone reviewer would (correctly)
push on.

### What gets built
- A Postgres streaming replica (docker-compose service, standard `postgres:16` with
  replication configured — a legitimate thing to actually set up and get working, not
  just describe).
- **Explicit routing logic**, not a coin flip: pure browsing reads (room list,
  someone's own past bookings, chat history) go to the replica. Any read that is
  *about to become part of a booking decision* (the availability check immediately
  before a `book_*` call) goes to the primary, inside the same transaction as the
  write — because that's the read whose staleness would actually cause a bug.
- A deliberately-run demo that **shows the lag exists** (write to primary, read from
  replica immediately, observe the brief window where it isn't there yet) before
  showing your routing decision avoids it mattering for the correctness-critical path
  — the same "prove it, don't assert it" instinct as the rest of this project.

---

## L10 — Caching layer (Redis) with real invalidation, not a demo cache

### What gets built
- Cache room list and availability-window queries in Redis, since they're read far
  more than bookings are written.
- **Invalidation on every write that affects it** — a booking or cancellation must
  invalidate (or update) the relevant cached availability windows in the same logical
  operation, not on a timer alone (a timer-only TTL cache would let you show a
  now-stale "available" result for however long the TTL is — acceptable for room
  *browsing*, not acceptable feeding directly into a booking decision, which is why
  L9's routing rule still applies: the cache is for browsing, the actual pre-booking
  check still goes straight to the primary, uncached).
- **Cache stampede handling**: when a hot cache key expires, don't let every
  concurrent request that misses it hit Postgres simultaneously — use a lock/mutex-
  on-recompute pattern (e.g., first request to miss takes a short Redis lock and
  repopulates, others wait briefly and re-read) — a real, nameable caching problem
  worth handling deliberately rather than ignoring.

---

## L11 — Rate limiting and abuse prevention

### Why this directly answers the "boring but effective" attack from our discussion
The most realistic attack on this system isn't a clever concurrency exploit — it's
just volume: hammer `/login` to brute-force credentials, or hammer `/bookings` from
one account to burn through the connection pool and lock out everyone else.

### What gets built
- **Redis-backed token-bucket rate limiter**, applied per-user (post-auth) and
  per-IP (pre-auth, so login itself is protected before you even know who's
  attacking).
- Specifically protect: `/login` (brute-force defense), `/bookings` create/cancel
  (pool-exhaustion defense), and the new chat send endpoint (spam/harassment
  defense).
- A deliberate attack-and-defend demo: script that hammers `/login` past the limit,
  show it gets throttled (429) rather than the pool degrading for everyone else —
  same evidentiary style as your existing demos.

---

## L12 — Security hardening: auth, IDOR, and Postgres Row-Level Security

### What gets built
1. **JWT hardening** — short-lived access tokens + refresh tokens, and confirm (with
   a deliberate test) that a deactivated user's *existing* token stops working
   immediately, not just at next login — closing the exact gap identified in our
   discussion.
2. **IDOR lockdown + attack test** — every booking mutation endpoint explicitly
   checks ownership or moderator role before acting, and you write
   `tests/idor_demo.py` in the same spirit as `race_demo.py`: attempt to cancel
   another user's booking, escalate your own role via a crafted payload, assert both
   are rejected. This is your authorization equivalent of the concurrency proof —
   same rigor, different invariant.
3. **Postgres Row-Level Security** as a second, independent layer of the same
   guarantee: policies on `bookings` so that even a query that "forgot" the
   application-layer ownership check is still blocked by Postgres itself. Demo it by
   writing a query that deliberately skips the app-layer check and showing RLS
   catches it anyway — the concrete "defense in depth actually means something here"
   proof.

---

## L13 — Tamper-evident, hash-chained audit log

### What gets built
- An append-only `audit_log` table, written via a **Postgres trigger** (not
  application code, so it can't be forgotten or bypassed) on every booking/room/user
  state change.
- Each row's hash includes the previous row's hash — a hash chain, same integrity
  principle as a blockchain, no cryptocurrency baggage, applied to a real, motivated
  problem: detecting if someone with direct DB access (a compromised credential, a
  rogue admin) tampered with history.
- A verification script that walks the chain and reports the first broken link.
  Demo: directly tamper with a row via raw SQL, run the verifier, show it catches
  exactly where the chain broke.

---

## L14 — Real-time chat, scoped and designed as its own scaling problem

### The actual design problem (not "add a websocket route")
Two users connected to *different* app instances (L8) need messages to reach each
other. A naive single-instance WebSocket server would work locally and then quietly
fail the moment you're behind the load balancer — this is the single most common
real-world WebSocket-at-scale bug, and designing around it deliberately is the point.

### What gets built
- **Scoping first, before any code:** chat is tied to a specific booking context —
  e.g., the person who just lost a room to a conflict can message the person who
  holds it, or a moderator can message either party about a dispute. It is **not**
  open messaging between arbitrary users. Every WebSocket connection and every
  message send is authorized against "does this user have a legitimate relationship
  to this booking" before anything is allowed — directly closes the harassment-vector
  concern from our discussion.
- **FastAPI native WebSocket endpoints**, authenticated at handshake time (JWT passed
  and validated before the upgrade is accepted, not after).
- **Redis Pub/Sub as the cross-instance backplane**: when app instance A's user sends
  a message, A publishes it to a Redis channel keyed by the conversation/booking id.
  Every app instance subscribes to the channels for connections it's currently
  holding, so instance B — holding the *other* party's WebSocket — receives and
  forwards it, regardless of which instance either party happened to be routed to by
  nginx.
- **Message persistence** to Postgres (a `messages` table, read via the replica per
  L9's routing rule, written to the primary), so chat history survives a reconnect
  or a server restart — this isn't just a real-time relay, it's a real feature with
  real durability.
- **XSS-safe rendering** on the frontend — message content is always treated as text,
  never injected as raw HTML.
- A deliberate scale demo: open connections to two different app instances (force it
  by connecting directly to each container, bypassing the load balancer for the
  test), send a message from one, prove it arrives on the other — the concrete proof
  that the pub/sub backplane is actually doing its job, not just working by accident
  because you happened to land on the same instance during testing.

---

## L15 — Observability across a now multi-instance, multi-subsystem system

### Why this stops being optional here
Once there are three app instances, a load balancer, a cache, a pub/sub backplane,
and two database nodes, "read the one process's stdout" (your L6-era debugging
approach) stops working. This is the natural, necessary next chapter of the same
debugging discipline that found your three L6 bugs — just at the scale where you
actually need tooling for it instead of just careful reading.

### What gets built
- **Structured logging** (JSON logs, with a request/trace id that follows a request
  across nginx → app instance → Postgres/Redis calls, so you can reconstruct one
  request's full path after the fact).
- **Prometheus metrics**: request latency and error rate per endpoint, connection
  pool utilization (both the app-level and PgBouncer-level pools), cache hit/miss
  rate, WebSocket connection count per instance, rate-limiter rejection count.
- **Grafana dashboards** on top of the above — a real, visual "here's what the system
  looks like under the L6-style load test" artifact, which is a genuinely strong
  thing to screenshot for a resume/portfolio.

---

## L16 — Chaos / fault injection: correctness under failure, not just load

### What gets built
- Kill one app instance mid-load-test — confirm nginx routes around it and no
  requests are lost beyond the ones actually in flight on that instance (which
  should fail cleanly, not hang).
- Kill Redis — confirm the system **degrades gracefully**: rate limiting and caching
  are best-effort conveniences, so losing Redis should not take down booking
  correctness (fail open on cache, fail appropriately on rate limiting — a real
  design decision to make and defend, not an accident).
- Kill the Postgres primary — confirm the system fails predictably (writes stop,
  clearly, rather than silently corrupting) rather than attempting automatic
  failover unless you deliberately build that (a good explicit scope boundary to
  state: "automatic primary failover is out of scope; this proves failure is
  detected and handled safely, not that the system self-heals").
- Every one of these is proven the same way as L6: independent audit after the
  fact, never trusting the system's own self-report during the chaos run.

---

## L17 — The red-team chapter: attack everything above, on purpose

This is the capstone payoff chapter, written in the same narrative style as your
L6 notes (bug → why it happens → fix → retest proving the fix). Deliberately attack
your own finished system:

1. Attempt IDOR against another user's booking — confirm rejection (L12).
2. Attempt to keep using a token after the user is deactivated — confirm it's dead
   immediately (L12).
3. Brute-force `/login` past the rate limit — confirm throttling, not degradation
   for other users (L11).
4. Attempt to exhaust the connection pool from one account via rapid booking
   requests — confirm the rate limiter catches it before the pool does (L11 + L8).
5. Attempt to tamper with a row directly via raw SQL and see if the audit chain
   verifier catches it (L13).
6. Attempt to open a chat connection to a booking you have no relationship to —
   confirm rejection (L14).
7. Kill Redis mid-chat-session — confirm chat degrades (reconnect/backoff) without
   taking booking down with it (L16 + L14).

Document every attempt as its own mini-case, exactly like the three bugs in
`docs/L6_notes.md` — this chapter is what makes the whole system's security claims
*proven* rather than asserted, matching the evidentiary standard the rest of the
project already holds itself to.

---

## L18 (stretch, if time allows) — Formal verification of the concurrency core (TLA+)

Kept from the earlier draft of this roadmap because it's genuinely valuable and has
nothing to do with ML: write a TLA+/PlusCal spec of `book_safe`/`book_optimistic`/
`book_serializable` and model-check it with TLC for the no-overlap and no-deadlock
invariants across all interleavings, not just the ones a load test happened to
generate. This is a separate, parallel artifact — it doesn't depend on L8–L17 and can
be done any time, including first, if you'd rather bank the highest-rigor piece
early.

---

## Recommended build order (given solo + Claude-Code-implemented + resume timeline)

1. **L8** (load balancer + multi-instance + PgBouncer) — everything else assumes this
   exists.
2. **L11** (rate limiting) and **L12** (IDOR/JWT/RLS security hardening) — fast,
   high-value, directly closes the attacks we discussed, doesn't depend on anything
   else new.
3. **L13** (tamper-evident audit log) — self-contained, DB-native, quick to add.
4. **L14** (real-time chat) — the standout new feature, and the one that most
   changes what this project *is* (adds a genuine real-time distributed-systems
   problem, not just more of the same).
5. **L9** (read replica) and **L10** (caching) — natural pair, both about read-path
   scaling.
6. **L15** (observability) — most valuable once there's enough distributed
   complexity (post L8/L9/L10/L14) to actually need it.
7. **L16** (chaos testing) and **L17** (red-team chapter) — the proof chapters,
   done last since they attack everything built before them.
8. **L18** (TLA+) — independent, can slot in anywhere, including in parallel with
   the above if you want the formal-proof artifact banked early.

---

## What this buys you on a resume, stated plainly

Not "I added a chat feature" — rather: *"I took a single-node system with a proven
correctness guarantee and scaled it horizontally (load balancer, connection pooler,
read replica with explicit consistency-aware routing), closed real attack surfaces
I identified by deliberately red-teaming my own system (IDOR, JWT lifecycle, rate
limiting, tamper-evidence), and added a real-time feature designed around its actual
cross-instance scaling problem (WebSocket + Pub/Sub backplane) rather than one that
would have quietly broken under the same load balancer I'd just built."* That's a
sentence that survives a systems-design interview follow-up question, which is
exactly the bar the rest of this project already meets.
