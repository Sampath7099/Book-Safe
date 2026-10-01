-- BookSafe — builds the three tables.
--
--   docker exec -i booksafe-db psql -U booksafe -d booksafe < db/schema.sql
--
-- WARNING: this wipes everything first. Fine while building; a real project
-- would use migrations that never delete data.

DROP TABLE IF EXISTS bookings CASCADE;
DROP TABLE IF EXISTS rooms    CASCADE;
DROP TABLE IF EXISTS users    CASCADE;

-- Needed by the no-overlap rule at the bottom of this file.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Needed by the audit hash chain at the bottom of this file.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- The 14 rooms in block H. Written once, basically never changes.
CREATE TABLE rooms (
    id        INTEGER  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code      TEXT     NOT NULL UNIQUE,        -- 'H201'
    floor     SMALLINT NOT NULL,
    capacity  INTEGER  NOT NULL CHECK (capacity > 0),
    version   INTEGER  NOT NULL DEFAULT 1,     -- unused until L3
    is_active BOOLEAN  NOT NULL DEFAULT TRUE
);


-- Who can book. Just a username and password -- only the bcrypt hash is
-- stored. Nothing here checks that someone actually belongs to the
-- college, which is fine on a private network but would need SSO or an
-- invite step for anything public.
CREATE TABLE users (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,     -- stored lowercase
    password_hash TEXT NOT NULL,            -- bcrypt. NEVER the password.
    full_name     TEXT NOT NULL,

    --   member    - book rooms, cancel their own
    --   moderator - + cancel anyone's booking, edit rooms
    --   admin     - + manage accounts
    role          TEXT NOT NULL DEFAULT 'member'
                  CHECK (role IN ('member', 'moderator', 'admin')),

    -- Checked on EVERY request, so deactivation takes effect immediately
    -- rather than whenever the person's token happens to expire.
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ
);


-- Every booking ever made. Grows forever. All the interesting problems live here.
CREATE TABLE bookings (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room_id    INTEGER NOT NULL REFERENCES rooms(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    starts_at  TIMESTAMPTZ NOT NULL,
    ends_at    TIMESTAMPTZ NOT NULL,
    party_size INTEGER NOT NULL CHECK (party_size > 0),
    purpose    TEXT,

    -- Cancelling keeps the row (for the record) and just flips this.
    -- So every query about live bookings must say status = 'confirmed'.
    status     TEXT NOT NULL DEFAULT 'confirmed'
               CHECK (status IN ('confirmed', 'cancelled')),

    -- Lets a client safely retry a request that may already have worked.
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,

    -- Must be ">" not ">=". A zero-length booking counts as empty, and an
    -- empty time range overlaps nothing — so it would sail past the rule below.
    CHECK (ends_at > starts_at),

    CHECK (ends_at - starts_at <= INTERVAL '24 hours'),

    -- THE rule this whole project is about: no two confirmed bookings may
    -- share a room AND overlap in time. '[)' means a booking ending at 16:00
    -- doesn't clash with one starting at 16:00.
    CONSTRAINT no_overlapping_bookings
        EXCLUDE USING gist (
            room_id                             WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        ) WHERE (status = 'confirmed')
);


-- Makes "show me my bookings" fast once there are lots of rows.
-- (rooms gets no index — 14 rows, not worth it.)
CREATE INDEX idx_bookings_user ON bookings (user_id) WHERE status = 'confirmed';

-- One booking per idempotency key, per user. This is what makes a retried
-- request safe even if two copies arrive at the same instant: the database
-- lets exactly one through and rejects the twin.
CREATE UNIQUE INDEX idx_bookings_idempotency
    ON bookings (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;


-- Chat messages, scoped to a booking. app/chat.py checks who's allowed to
-- join before any of these get written.
DROP TABLE IF EXISTS messages CASCADE;
CREATE TABLE messages (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    booking_id BIGINT NOT NULL REFERENCES bookings(id),
    sender_id  INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_booking ON messages (booking_id, created_at);


-- Row-level security as a second layer, on top of the ownership check
-- main.py already does. The app connects as `booksafe`, which owns these
-- tables and bypasses RLS by default -- it needs to see every booking, not
-- just one user's. `booksafe_readonly` is a weaker role that RLS actually
-- applies to, used in tests/rls_demo.py to prove the policy works even if
-- the app-layer check is skipped entirely.
--
-- Created here with NOLOGIN and no password -- this file gets run straight
-- against the real database, so it must never contain a usable credential.
-- `python db/setup_rls_demo_role.py` turns login on with a freshly
-- generated password, kept out of source control, only when someone
-- actually wants to run the RLS demo.
DROP ROLE IF EXISTS booksafe_readonly;
CREATE ROLE booksafe_readonly NOLOGIN;
GRANT SELECT ON bookings TO booksafe_readonly;

ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;

CREATE POLICY own_bookings_only ON bookings
    FOR SELECT
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::int);


-- Tamper-evident audit log, written by a trigger so it can't be skipped
-- by a future endpoint or by direct SQL access. Each row's hash covers the
-- previous row's hash, so editing any row breaks the chain from that point
-- on. db/verify_audit.py walks it and reports the first broken link.
DROP TABLE IF EXISTS audit_log CASCADE;
CREATE TABLE audit_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name TEXT   NOT NULL,
    row_id     TEXT   NOT NULL,
    action     TEXT   NOT NULL,   -- INSERT / UPDATE
    row_data   JSONB  NOT NULL,
    prev_hash  TEXT,              -- NULL only for the very first row ever
    hash       TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION audit_row() RETURNS TRIGGER AS $$
DECLARE
    prev     TEXT;
    payload  JSONB := to_jsonb(NEW);
    lock_key BIGINT := hashtext(TG_TABLE_NAME);
BEGIN
    -- Lock scoped to the table being written, not the whole audit log --
    -- otherwise a room edit would serialize behind a booking insert for no
    -- reason. And a SESSION lock, released right after the insert below,
    -- not a transaction lock held until COMMIT: two bookings for different
    -- rooms only need to take turns for the few microseconds it takes to
    -- read the last hash and write the next one, not for however long the
    -- rest of their transaction happens to run. The EXCEPTION block exists
    -- only to guarantee that unlock still happens if the insert itself
    -- fails -- a session lock that never gets released would jam every
    -- future booking on that pooled connection.
    PERFORM pg_advisory_lock(lock_key);
    BEGIN
        SELECT hash INTO prev FROM audit_log
            WHERE table_name = TG_TABLE_NAME ORDER BY id DESC LIMIT 1;
        INSERT INTO audit_log (table_name, row_id, action, row_data, prev_hash, hash)
        VALUES (
            TG_TABLE_NAME, NEW.id::text, TG_OP, payload, prev,
            encode(digest(coalesce(prev, '') || payload::text, 'sha256'), 'hex')
        );
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_advisory_unlock(lock_key);
        RAISE;
    END;
    PERFORM pg_advisory_unlock(lock_key);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_bookings AFTER INSERT OR UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER audit_rooms AFTER INSERT OR UPDATE ON rooms
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER audit_users AFTER INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION audit_row();
