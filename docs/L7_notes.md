# L7 — Self-hosted accounts, roles and admin powers

Replaces the CAS single-sign-on version. The CAS implementation is preserved in
`docs/archive/` and documented in `docs/L5_cas_notes.md` — it's still a real
thing that was built, and the front-channel / back-channel reasoning is worth
being able to explain.

## The model

| Role | Can |
|---|---|
| `member` | book rooms, cancel their own bookings |
| `moderator` | + cancel anyone's booking, edit rooms |
| `admin` | + manage accounts: promote, demote, deactivate, delete |

Everyone signs up as a `member`. Only an admin can promote. The **first** admin
is created by hand (`db/seed_admin.py`) because otherwise there'd be nobody able
to grant the role — a small bootstrapping problem every RBAC system has.

## Endpoints added

| Method | Path | |
|---|---|---|
| POST | `/auth/signup` | create an account (college email only) |
| POST | `/auth/login` | email + password → token |
| GET | `/admin/users` | list accounts, with booking counts |
| PATCH | `/admin/users/{id}` | change role, deactivate/reactivate |
| DELETE | `/admin/users/{id}` | anonymise (or `?hard=true` to really delete) |

---

## What was traded away by dropping CAS

Stated plainly, because it's the first thing an interviewer would probe.

| | CAS | Self-hosted |
|---|---|---|
| Who verifies the password | The college | **Us** |
| Passwords in our database | **None** | Every user's bcrypt hash |
| Can an outsider get in? | **No** — no account, no entry | Only a domain check stands in the way |
| Can a recruiter try the app? | No | **Yes** |
| Blast radius of a database leak | No credentials exposed | Every hash exposed |

**The honest weak point:** signup checks that the address *ends in*
`@iiit.ac.in`. That proves someone **typed** a college address, not that they
own one. `notastudent@iiit.ac.in` sails through.

Closing it requires **email verification** — mail a token to the address,
activate only when it comes back. That is precisely the job CAS was doing for
free. Worth saying out loud rather than hoping nobody asks.

---

## Design decisions worth defending

### Role is read from the database, not the token

The JWT carries a role, but `current_user` **ignores it** and re-reads role and
`is_active` from the database on every request.

That costs one indexed lookup per request. It buys **immediate revocation**: an
admin deactivates someone and their very next request fails, rather than
whenever their token happens to expire (L12 also shortened that window from
8 hours to 2, plus added `/auth/refresh` so an active session doesn't need a
full re-login to renew it — belt and suspenders, not a replacement for this).

This is the stateless-vs-stateful token argument, and this is us picking a side.
A pure stateless JWT cannot be revoked at all — that's its well-known weakness.
The alternatives are short-lived tokens plus refresh (more moving parts) or a
revocation denylist (a database lookup anyway, which is what we're already
doing).

Verified:

```
PASS  admin deactivates alice                        200
PASS  alice's EXISTING token now rejected            403
PASS  alice cannot log in again                      403
PASS  admin reactivates alice                        200
PASS  alice works again with her ORIGINAL token      200
```

### Two lockout guards

1. **You cannot change your own role or delete your own account.** The classic
   way to lock yourself out of your own system.
2. **The last active admin cannot be demoted, deactivated or deleted** — by
   anyone, including another admin. Without this, one bad request leaves nobody
   able to fix it.

The check runs inside a transaction with `SELECT ... FOR UPDATE` on the target
row. Two admins demoting each other simultaneously is the same
check-then-act race as the booking bug — count the remaining admins and act on
it, without a lock, and both could succeed.

### Deletion is two different questions

Bookings reference users by foreign key. Really deleting a row would either
destroy booking history or break referential integrity — and the history is a
**fact**: that room *was* booked, by someone, and the record matters after they
leave.

**Default — anonymise.** Scrub the personal data, keep the row:
- `email` → `deleted-504@removed.invalid`
- `full_name` → `Deleted User`
- `password_hash` → an unusable value, so login is impossible
- upcoming bookings cancelled; past ones kept, now anonymous

This is what data-protection rules actually ask for: the person becomes
unidentifiable, the history survives.

**`?hard=true`** genuinely deletes the row, but **only if the account has no
bookings at all**. Otherwise it returns 409 explaining why. Refusing beats
silently cascading and destroying records.

```
PASS  hard-delete someone who has bookings   409
PASS  anonymise instead                      200
PASS  carol can no longer log in             401
      her row: deleted-504@removed.invalid | Deleted User | active=False
      booking history kept: 1 row
PASS  hard-delete someone with NO bookings   200
```

### Vague error messages, deliberately

- Signup with a taken email → *"could not create that account"*, not *"that
  email is registered"*
- Login failure → *"invalid email or password"* whether the account exists or not

Precise errors let a stranger **enumerate** who has an account here. Same
reasoning as Postgres refusing to say whether a role exists.

---

## Verified: 27 checks

```
SIGN-UP RULES
  PASS  outsider signs up with a gmail address          400
  PASS  password of 4 characters                        400
  PASS  signing up with an email already taken          409
  PASS  new account defaults to member                  member

LOGIN
  PASS  right email, wrong password                     401
  PASS  SQL injection in the login form                 401
  PASS  token hand-edited to claim role=admin           401
  PASS  no token at all                                 401

WHO MAY DO WHAT
  PASS  alice books a room                              201
  PASS  bob cancels alice's booking                     403
  PASS  member edits a room                             403
  PASS  member lists all accounts                       403

ADMIN POWERS
  PASS  admin promotes bob to moderator                 200
  PASS  bob (now moderator) cancels alice's booking     204
  PASS  moderator still cannot manage accounts          403
  PASS  admin invents a role that doesn't exist         400

LOCKOUT GUARDS
  PASS  admin demotes THEMSELVES                        403
  PASS  admin deletes their OWN account                 403
```

Reproduce: `python tests/security_demo.py`

---

## The bug this layer produced

`current_user` runs on every request and needs the database. I wrote it using
`connect()` — a **brand new connection each time**, bypassing the pool.

Fine at low traffic. Under the 500-request load test:

```
psycopg.OperationalError: connection failed:
FATAL: sorry, too many clients already
```

Postgres allows ~100 connections. 500 concurrent requests each opening their own
blew straight through it, and two requests returned 500 instead of 409.

**Fix:** move the pool into `app/db.py` so `auth.py` and `main.py` share one
bounded set of connections. The module now says which to use and why:

```
connect()   one fresh connection. Scripts and tests only.
pool        shared and reusable. Everything that runs inside a request.
```

Note the ordering: `current_user` borrows a connection and **releases it before
the endpoint takes one**, so a request never holds two at once. Holding both
simultaneously would deadlock the pool the same way the yield-dependency
deadlocked the thread pool in L6.

Also fixed here: `JWT_SECRET` was 20 bytes, and PyJWT warns below 32 for
HMAC-SHA256. Regenerated at 64.

---

## Interview questions

**Q. Why build your own auth instead of using SSO?**
Honest answer: SSO is the better security posture — we held no passwords, and
only real college members could ever get in. We moved to self-hosted accounts
for demoability, so anyone can try the system without institutional
credentials. The trade is explicit: we now own password storage, and the
domain check is weaker than CAS's identity guarantee until email verification
is added.

**Q. How do you stop outsiders signing up?**
Right now, a domain check on the email. That's honestly weak — it proves
somebody typed a college address, not that they own one. Email verification is
the real fix and it's the next thing I'd build.

**Q. Why re-read the role from the database instead of trusting the token?**
So deactivation and demotion take effect immediately. A JWT is valid until it
expires and can't be revoked; one indexed lookup per request buys instant
revocation. If that lookup became a bottleneck I'd cache it with a short TTL
and accept a few seconds of staleness.

**Q. What happens when you delete a user who has bookings?**
By default we anonymise: scrub the personal fields, keep the row, cancel their
upcoming bookings, retain the history. A hard delete is only permitted when the
account has no bookings, otherwise it returns 409 — cascading would destroy
records that are facts about the building's use.

**Q. Why can't an admin demote themselves?**
Lockout prevention, plus a concurrency guard: two admins demoting each other at
once is a check-then-act race, so the count-and-update runs inside a
transaction with the row locked. The same bug as double-booking, in a different
costume.

**Q. Why are your error messages so vague?**
"That email is taken" tells a stranger who has an account here. Account
enumeration is a real reconnaissance step, so signup and login both return
deliberately uninformative failures.
