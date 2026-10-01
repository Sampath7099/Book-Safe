# L5 — Authentication and authorization, via college CAS

## The model

| | How you log in | Role | Who creates the account |
|---|---|---|---|
| College people | **CAS single sign-on** | `member` | Automatically, on first login |
| Administrators | email + password (bcrypt) | `admin` | By hand, `db/seed_admin.py` |

**There is no sign-up form.** If the college's CAS doesn't recognise you, there
is no door. That isn't a rule the app enforces — it's a door that doesn't exist,
which is a much stronger position.

**The login method decides the role**, and that's enforced by the database:

```sql
CONSTRAINT login_method CHECK (
    (cas_username IS NOT NULL AND password_hash IS NULL     AND role = 'member')
 OR (cas_username IS NULL     AND password_hash IS NOT NULL AND role = 'admin')
)
```

Verified by attacking it directly in `psql`, bypassing the application:

| Attempt | Result |
|---|---|
| CAS account with `role='admin'` | **rejected** |
| CAS member given a password | **rejected** |
| Password account with `role='member'` | **rejected** |
| Legitimate CAS member | accepted |

Privilege escalation is not merely unimplemented — it is **structurally
impossible**, even from a database console.

---

## What CAS is, and the ticket dance

CAS (Central Authentication Service) is the college's single sign-on — the same
thing Moodle uses. Like "Sign in with Google", but run by the college and only
for people who belong there.

```
1. user      -> our app     "I want to book H201"
2. our app   -> user        302 to https://cas.college/login?service=<us>
3. user      -> CAS         [password typed on the COLLEGE's page]
4. CAS       -> user        302 back to us with ?ticket=ST-a8f3c9
5. user      -> our app     "here's my ticket"

   >> we do NOT trust it <<

6. our app   -> CAS         server-to-server: "is ST-a8f3c9 genuine?"
7. CAS       -> our app     XML: <cas:user>21b0777</cas:user>
8. our app   -> user        our own JWT, valid 8 hours
```

**Step 6 is the entire security model.** The ticket arrives inside the user's
browser, so it is only a *claim* — anyone can type `?ticket=whatever` into the
URL bar. We open our own connection to CAS, which the user cannot touch or
intercept, and ask.

Verified:

| Attack | Result |
|---|---|
| Invented ticket `ST-i-just-made-this-up` | **401** |
| Replaying a genuine ticket a second time | **401** |

Tickets are **single-use and expire in seconds**, so one copied out of a log or
a `Referer` header is already dead.

### What CAS does and doesn't do

| | Who |
|---|---|
| **Authentication** — "is this really Sampath?" | **CAS** |
| **Authorization** — "may Sampath cancel this booking?" | **us** |
| Bookings, roles, app data | **our database** |

CAS returns a username and nothing else. It doesn't know our app exists. So we
keep our own `users` table and create a row on first login (*just-in-time
provisioning*), and we decide permissions ourselves.

### The biggest win

**We never see anyone's password.** Not in memory, not in a log, not in the
database. If this entire database leaked tomorrow, no college credential is
exposed — the only password hash in it belongs to a local admin account.

You cannot leak what you never held.

---

## Why we still need our own token

A CAS ticket is single-use and dies in seconds. It proves who *arrived*; it
can't prove who is *still here*. And you cannot do a browser redirect on every
API call.

So after validating the ticket once, we issue a **JWT** — a signed note saying
*"this is user 502, a member, until 8pm"*.

The user can read it (a JWT is signed, **not encrypted** — never put secrets in
one) but cannot change it, because altering the payload invalidates the
signature and they don't have `JWT_SECRET`.

Verified: a token hand-edited to say `"role": "admin"` → **401**.

---

## Why bcrypt and not SHA-256, for the admin password

| | SHA-256 | bcrypt |
|---|---|---|
| Speed | ~1,000,000,000/sec on a GPU | ~10/sec |
| Salted | no, unless you do it yourself | **automatically** |
| Built for | file checksums | **passwords** |

Two properties matter:

**Salting.** bcrypt mixes in random bytes per password, so two people with the
same password get different hashes. Without it, an attacker precomputes hashes
once (a rainbow table) and cracks every matching account at once.

**Slowness is the feature.** ~100ms is invisible when you check one login and
ruinous when an attacker checks billions. Being fast is what makes SHA-256 the
wrong tool.

And **hashing is not encryption**: encryption is designed to be reversed with a
key; hashing is one-way by design. We never need the original password back —
we only need to check whether a new attempt hashes to the same thing.

Confirmed on disk:

```
member  21b0001    none — CAS checks it, we never see it
admin   None       $2b$12$Hhr.DBMBzc9X1BkRuM3yhusouh0CsnC...
```

`$2b$` = bcrypt, `12` = cost factor (2^12 rounds).

---

## SQL injection

Every query in this project passes values as **parameters**, never by pasting
them into a string:

```python
cur.execute("SELECT ... WHERE email = %s", (email,))   # safe
cur.execute(f"SELECT ... WHERE email = '{email}'")     # catastrophic
```

The difference isn't escaping — it's that the SQL text and the data travel
**separately**. The database parses the query *first*, then fills in values. A
value can never become a command, because by the time it arrives the query has
already been understood.

Verified: logging in as `admin@college.edu' OR '1'='1` → **401**. The string was
searched for *literally*, as an email address. No such user.

---

## Verified: 10 attacks, 10 refusals

```
AUTHENTICATION
  PASS  book with no token at all                     401
  PASS  book with a garbage token                     401
  PASS  token hand-edited to say role=admin           401

AUTHORIZATION
  PASS  alice books a room                            201
  PASS  bob cancels alice's booking                   403
  PASS  member edits a room (admin only)              403
  PASS  admin edits a room                            200
  PASS  admin cancels alice's booking                 204

INJECTION & ACCOUNT ATTACKS
  PASS  SQL injection in the admin login form         401
  PASS  brute-force a real admin's password           401
```

Reproduce: `python tests/security_demo.py`

### 401 vs 403

- **401 Unauthorized** — *"I don't know who you are."* Log in.
- **403 Forbidden** — *"I know exactly who you are, and no."* Logging in again
  won't help.

Also note `cancel_booking` checks **404 before 403**: if the booking doesn't
exist you get 404, and only then do we check ownership. Reversing that order
leaks the existence of other people's bookings to anyone who probes ids.

---

## Going live against the real CAS

Development runs against `tests/mock_cas.py`, a small server that speaks the
real CAS protocol. This is deliberate, not a shortcut: a university CAS usually
only works on campus or VPN, and refuses to redirect to any address not
registered with it — neither is something a test suite can depend on.

To switch, change one line in `.env`:

```
CAS_BASE_URL=https://cas.yourcollege.ac.in/cas
SERVICE_URL=https://booksafe.yourcollege.ac.in/auth/callback
```

You will likely also need to:

1. **Register the service** with whoever runs CAS. Most reject unknown
   `service` URLs outright.
2. **Serve over HTTPS.** Many CAS servers refuse plain `http` callbacks, and a
   token over plain HTTP can be read in transit anyway.
3. **Check which CAS version.** We call `/p3/serviceValidate` (CAS 3.0). Older
   servers use `/serviceValidate` (CAS 2.0) and don't return attributes.

The `service` parameter must be **byte-identical** between the login redirect
and the validation call — it's part of what the ticket is bound to. A trailing
slash difference is a classic hour-long debugging session.

---

## Known gaps (honest list)

1. **No logout.** JWTs can't be revoked before expiry without a denylist. A
   real deployment adds one, plus CAS single-logout.
2. **No rate limiting** on the admin login. Brute force is slowed by bcrypt but
   not blocked.
3. **`JWT_SECRET` defaults to a dev value.** Fine locally; must be a real
   random secret in production, or anyone can mint admin tokens.
4. **8-hour tokens with no refresh.** Long-lived tokens are a bigger window if
   one is stolen. Production uses short access tokens plus refresh tokens.

---

## Interview questions

**Q. Why use CAS instead of your own login?**
Three reasons. Only college members can get in, with no membership list to
maintain. We never handle a password, so a database leak exposes no credentials.
And users get single sign-on with everything else on campus. The cost is a hard
dependency on CAS being reachable.

**Q. Why validate the ticket server-side? Why not trust it?**
It travels through the user's browser, so it's attacker-controlled — anyone can
type one into the URL bar. The back-channel call to CAS is the only thing the
user can't forge or intercept.

**Q. Authentication vs authorization?**
Authentication is *who you are* — CAS does that. Authorization is *what you may
do* — that's ours. CAS will happily confirm a person's identity and tell us
nothing about whether they may cancel a booking.

**Q. Why a JWT on top of CAS?**
The CAS ticket is single-use and expires in seconds. It proves arrival, not
continued presence, and you can't redirect a browser on every API call.

**Q. Is a JWT encrypted?**
No — signed. Anyone can decode and read it. It guarantees *integrity*, not
*secrecy*. Never put anything private in one.

**Q. Why bcrypt over SHA-256?**
SHA-256 is fast and unsalted, which is exactly wrong for passwords. bcrypt salts
automatically and is deliberately slow, with a tunable cost factor you raise as
hardware improves.

**Q. Hashing vs encryption?**
Encryption is two-way with a key; hashing is one-way. You never need a password
back — only to check whether a new attempt hashes to the same value.

**Q. How do parameterized queries stop injection?**
The query text and the data are sent separately. The database parses the SQL
first, then binds values. A value can't become a command because the query's
structure was fixed before the value arrived.

**Q. 401 or 403?**
401 = I don't know you, log in. 403 = I know you, and the answer is still no.
