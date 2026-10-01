"""L11 — hammer /auth/login and check it throttles instead of just failing.

    python tests/ratelimit_demo.py       (API must be running, Redis too)

Limit is 10 attempts/minute per IP. After that it should come back 429,
not 401 -- the server should stop even checking the password.
"""

import httpx

API = "http://127.0.0.1:8000"
ATTEMPTS = 20

print(f"firing {ATTEMPTS} bad logins at {API}/auth/login as fast as possible\n")

codes = []
for i in range(ATTEMPTS):
    reply = httpx.post(f"{API}/auth/login",
                       json={"username": "admin", "password": "wrong"})
    codes.append(reply.status_code)
    tag = "429 THROTTLED" if reply.status_code == 429 else str(reply.status_code)
    print(f"  attempt {i + 1:>2}: {tag}")

throttled = codes.count(429)
allowed = len(codes) - throttled
print(f"\n  allowed through : {allowed}")
print(f"  throttled (429) : {throttled}")

verdict = "PASS" if throttled > 0 and allowed <= 10 else "FAIL"
print(f"  verdict         : {verdict}")
