# BookSafe

A room-booking system for college lecture halls that never double-books a
room, no matter how many people try to book the same slot at once.

The hard part isn't CRUD, it's concurrency. Booking a room means "check if
it's free, then book it" — two steps with a gap in between, and that gap is
where the bug lives. This project fixes that gap three different ways and
proves each one works under real load.

## The result

500 concurrent booking requests against 112 room-hours: exactly 112
succeeded, 388 got a clean 409 Conflict, zero double-bookings. Checked by
querying the database afterward, not by trusting the app's own numbers.

Three locking strategies were built and compared under the same load
(pessimistic locking, optimistic retry, and Postgres SERIALIZABLE with
retry). All three came out correct. Pessimistic did the least wasted work.

Tested with a connection pool of 20 — 500 requests hitting the API doesn't
mean 500 requests hitting Postgres at once, the pool is the real limit.

## Domain

Building H, 14 rooms across three floors:

| Rooms | Capacity |
|---|---|
| H101, H201, H301 | 40 |
| H102, H202, H302 | 60 |
| H103, H203, H303 | 80 |
| H104, H204, H304 | 100 |
| H105, H205 | 150 |

A booking can last at most 24 hours and can be made at most 7 days ahead.

## Build layers

| Layer | Topic | Status |
|---|---|---|
| L0 Setup | Postgres + repo + connectivity | done |
| L1 Data model & naive booking | DBMS: schema | done |
| L2 The race → pessimistic locking | DBMS: transactions & locks | done |
| L3 Isolation levels & optimistic locking | DBMS: isolation | done |
| L4 REST API | Backend | done |
| L5 Auth & authorization | Security | done |
| L6 Load-test proof | Reliability | done |
| L7 Accounts, roles & admin | Security | done |
| L10 Redis caching (rooms, availability) | Performance | done |
| L11 Rate limiting | Security | done |
| L12 IDOR / JWT lifecycle / Postgres RLS | Security | done |
| L13 Hash-chained audit log | Security | done |
| L14 Real-time chat (websockets + Redis pub/sub) | Feature | done |
| L17 Red-team chapter | Security | done — see [docs/L17_notes.md](docs/L17_notes.md) |

L8/L9 (load balancer, multiple instances, read replica) and L15/L16
(monitoring, chaos testing) aren't built — see
[docs/L8_plus_roadmap.md](docs/L8_plus_roadmap.md) for why.

## How far has this actually been tested?

Worth being precise about this rather than just claiming "handles 5k users":

| Claim | Status |
|---|---|
| No double-booking, ever, under contention | **Proven.** 500 concurrent requests, verified by an independent SQL audit afterward, not the app's own reporting. See L6. |
| Survives one account trying to brute-force or spam | **Proven.** Rate limiter tested directly — 429s kick in, rest of the app stays responsive. |
| Keeps working if Redis goes down | **Proven.** Stopped Redis mid-run; login, booking, and browsing all kept returning correct results, just uncached. |
| Handles 5,000 *concurrent* users | **Not tested.** The load test proves correctness at 500 concurrent requests, which stands in for a realistic burst (everyone piling onto a popular slot at once), not literal simultaneous use by every registered user. |
| Chat and audit log under real load | **Not load-tested.** Functionally verified (messages deliver, unauthorized access rejected, tampering caught) but never stress-tested at volume. |

The honest framing: this is built to be **correct under contention it's
actually been tested against**, on a single instance sized for bursty
traffic, not a system benchmarked at the literal scale of 5,000 simultaneous
users. See the "Interview questions" section at the bottom of
[docs/L17_notes.md](docs/L17_notes.md) for more on this.

## Running the database

PostgreSQL 16 in Docker. Data lives in the named volume `booksafe-pgdata`,
which sticks around after the container stops.

    docker volume create booksafe-pgdata
    docker run -d --name booksafe-db \
      -e POSTGRES_USER=booksafe -e POSTGRES_PASSWORD=booksafe \
      -e POSTGRES_DB=booksafe -p 5432:5432 \
      -v booksafe-pgdata:/var/lib/postgresql/data \
      --restart unless-stopped postgres:16

Or `docker compose up -d` does the same thing from `docker-compose.yml`.

Docker instead of a system install: gets Postgres 16 instead of an older
distro version, and password auth over TCP without touching `pg_hba.conf`.

Postgres instead of something simpler: this needs real row-level locking,
SERIALIZABLE isolation, and range exclusion constraints. SQLite can't do any of that.

## Running Redis

Used for rate limiting, caching, and the chat pub/sub channel.

    docker run -d --name booksafe-redis -p 6379:6379 --restart unless-stopped redis:7-alpine

`docker-compose.yml` runs both Postgres and Redis together.

## Setup

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env      # then check DATABASE_URL and REDIS_URL
    python app/db.py          # should print the Postgres version
    python app/cache.py       # should print True (Redis reachable)
    docker exec -i booksafe-db psql -U booksafe -d booksafe < db/schema.sql
    python db/seed_rooms.py
    python db/seed_admin.py admin 'some strong password'
    uvicorn app.main:app --reload

## Notes

- Config lives in `.env`, which is git-ignored: `JWT_SECRET` and `REDIS_URL`.
- Default transaction isolation is READ COMMITTED, which is exactly the
  level where the double-booking race can happen. See L3.
- `db/verify_audit.py` walks the audit log and reports the first tampered
  row, if there is one.
