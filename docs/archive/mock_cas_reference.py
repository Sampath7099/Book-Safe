"""A pretend CAS server, for developing and testing without the college's.

WHY THIS EXISTS
---------------
A real university CAS usually (a) only works on campus or VPN, and (b) refuses
to redirect to any address that hasn't been registered with it. Neither is
something you can rely on while writing code or running tests.

CAS is a standard protocol, so a fake that speaks it correctly is
indistinguishable to our app. Going live means changing one line in .env:

    CAS_BASE_URL=https://cas.yourcollege.ac.in/cas

This is normal practice, not a shortcut -- a test suite must not depend on a
production login server being reachable.

    uvicorn tests.mock_cas:cas --port 8100
"""

import secrets
from urllib.parse import urlencode

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, RedirectResponse

cas = FastAPI(title="Mock CAS")

# ticket -> username. A real CAS also expires these after a few seconds.
issued: dict[str, str] = {}


@cas.get("/cas/login")
def login(service: str, username: str | None = None):
    """Step 2 of the dance: the college's login page.

    Pass ?username=... to skip the form, which is what the tests do.
    """
    if username is None:
        return HTMLResponse(f"""
            <h2>College Single Sign-On (mock)</h2>
            <p>Pretend you typed your college password here.</p>
            <form method="get">
              <input type="hidden" name="service" value="{service}">
              <input name="username" placeholder="college username" autofocus>
              <button>Sign in</button>
            </form>
        """)

    ticket = "ST-" + secrets.token_urlsafe(16)
    issued[ticket] = username
    return RedirectResponse(f"{service}?{urlencode({'ticket': ticket})}")


@cas.get("/cas/p3/serviceValidate")
def service_validate(service: str, ticket: str):
    """Step 6: the app asks us, server to server, whether a ticket is real.

    Note the ticket is DELETED on use. Validating twice fails -- so a ticket
    copied out of a browser log or a referer header is worthless.
    """
    username = issued.pop(ticket, None)

    if username is None:
        body = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
          <cas:authenticationFailure code='INVALID_TICKET'>
            ticket not recognised
          </cas:authenticationFailure>
        </cas:serviceResponse>"""
    else:
        body = f"""<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
          <cas:authenticationSuccess>
            <cas:user>{username}</cas:user>
          </cas:authenticationSuccess>
        </cas:serviceResponse>"""

    return Response(content=body, media_type="application/xml")
