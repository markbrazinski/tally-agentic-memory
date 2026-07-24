"""Judge-demo auth surface: login/logout endpoints, the enforcement middleware,
and a minimal login page.

When TALLY_JUDGE_AUTH_ENABLED=true and Cognito is configured, EVERY request must
carry a valid Cognito JWT (Authorization: Bearer or the tally_session cookie),
except a small public allowlist: the login page + its API, health/readiness, and
static assets. Unauthenticated API calls get 401; unauthenticated page loads
redirect to /login. This protects pages, /api reads, PDF bytes, the SSE stream,
and the import endpoint uniformly.

Login uses Cognito's USER_PASSWORD_AUTH flow (username + password → JWT), so the
judge just needs a username and password — no hosted-UI redirect. The JWT is set
as an httpOnly, Secure, SameSite=Strict cookie so SSE/PDF/page GETs (which can't
set an Authorization header) are also authenticated.
"""

from __future__ import annotations

import boto3
from fastapi import Cookie, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.platform.cognito_auth import CognitoAuthError, CognitoConfig, verify_cognito_jwt

SESSION_COOKIE = "tally_session"

# Public allowlist — everything else requires a valid JWT.
_PUBLIC_EXACT = frozenset({"/login", "/healthz", "/readyz", "/api/login", "/api/logout"})


def _is_public(path: str) -> bool:
    return (
        path in _PUBLIC_EXACT
        or path.startswith("/assets/")
        or path == "/favicon.ico"
    )


def install_judge_auth(app, *, config: CognitoConfig) -> None:
    """Add the enforcement middleware + login/logout routes to the app.

    Routes are registered here via `add_api_route` (not decorators, not a
    sub-router) so they attach to the same `app.router` that uvicorn serves,
    before the `app.mount("/", StaticFiles)` catch-all. A sub-router included
    with `include_router` materialized lazily and landed AFTER the mount at
    request time — the mount then shadowed /login and /api/login to 404 (this
    bit us live: middleware enforced 401s but the login page 404'd).
    """

    @app.middleware("http")
    async def enforce_cognito(request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        token = _request_token(request)
        if not token:
            return _unauthenticated(request)
        try:
            verify_cognito_jwt(token, config)
        except CognitoAuthError:
            return _unauthenticated(request)
        return await call_next(request)

    _register_auth_routes(app, config)


def _request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.cookies.get(SESSION_COOKIE)


def _unauthenticated(request: Request):
    # API/asset/XHR requests get a clean 401; top-level page navigations get a
    # redirect to the login screen.
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(status_code=401, content={"error": "unauthorized"},
                            headers={"WWW-Authenticate": "Bearer"})
    if "text/html" in accept and request.method == "GET":
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse(status_code=401, content={"error": "unauthorized"})


class LoginRequest(BaseModel):
    username: str
    password: str


def _register_auth_routes(app, config: CognitoConfig) -> None:
    # Registered directly on `app` (not via include_router): a sub-router is
    # materialized lazily and lands AFTER the `app.mount("/", StaticFiles)`
    # catch-all in match order, so the static mount shadows /login and
    # /api/login → 404. Direct @app routes register eagerly, before the mount,
    # exactly like /healthz and /invoices. (This bit us live: middleware
    # enforced but the login routes 404'd once the static dir existed.)

    @app.post("/api/login")
    def login(body: LoginRequest) -> Response:
        client = boto3.client("cognito-idp", region_name=config.region)
        try:
            resp = client.initiate_auth(
                ClientId=config.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": body.username, "PASSWORD": body.password},
            )
        except client.exceptions.NotAuthorizedException:
            return JSONResponse(status_code=401, content={"error": "invalid_credentials"})
        except client.exceptions.UserNotConfirmedException:
            return JSONResponse(status_code=403, content={"error": "user_not_confirmed"})
        except Exception:  # noqa: BLE001 - never leak provider internals to the client
            return JSONResponse(status_code=502, content={"error": "auth_unavailable"})

        auth_result = resp.get("AuthenticationResult")
        if not auth_result:
            # A challenge (e.g. NEW_PASSWORD_REQUIRED) — the judge account is
            # provisioned with a permanent password, so this is unexpected.
            return JSONResponse(status_code=403, content={"error": "auth_challenge_required"})

        # Use the ID token (carries email/username) as the session; expiry comes
        # from the token itself (validated on every request).
        id_token = auth_result["IdToken"]
        expires_in = int(auth_result.get("ExpiresIn", 3600))
        response = JSONResponse({"ok": True})
        response.set_cookie(
            key=SESSION_COOKIE, value=id_token, max_age=expires_in,
            httponly=True, secure=True, samesite="strict", path="/",
        )
        return response

    @app.post("/api/logout")
    def logout(tally_session: str | None = Cookie(default=None)) -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(_LOGIN_HTML)


# Minimal, self-contained login screen — deliberately separate from the Claude
# product UI (which is never modified). No external assets.
_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tally — Sign in</title>
<style>
  :root{color-scheme:light}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:#1D2A33;color:#F5F0E7;display:flex;min-height:100vh;align-items:center;
    justify-content:center}
  .card{background:#FCFBF8;color:#23272F;width:340px;max-width:92vw;border-radius:10px;
    padding:32px 28px;box-shadow:0 10px 40px rgba(0,0,0,.35)}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:4px}
  .brand b{font-size:20px;letter-spacing:.02em}
  .sub{color:#6F7883;font-size:13px;margin:0 0 22px}
  label{display:block;font-size:12px;color:#40515C;margin:14px 0 6px;
    text-transform:uppercase;letter-spacing:.05em}
  input{width:100%;padding:11px 12px;border:1px solid #DED6C7;border-radius:7px;
    font-size:15px;background:#fff}
  input:focus{outline:2px solid #C8A955;border-color:#C8A955}
  button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:7px;
    background:#1D2A33;color:#F5F0E7;font-size:15px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{color:#B4513F;font-size:13px;margin-top:14px;min-height:18px}
  .foot{color:#8A96A0;font-size:11px;margin-top:20px;text-align:center}
</style></head>
<body>
  <form class="card" id="f">
    <div class="brand"><b>TALLY</b></div>
    <p class="sub">Judge demo — sign in to continue</p>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button id="b" type="submit">Sign in</button>
    <div class="err" id="e"></div>
    <div class="foot">Representative demonstration · authorized access only</div>
  </form>
<script>
  const g=id=>document.getElementById(id);
  const f=g('f'),e=g('e'),b=g('b');
  f.addEventListener('submit',async(ev)=>{
    ev.preventDefault();e.textContent='';
    b.disabled=true;b.textContent='Signing in…';
    try{
      const r=await fetch('/api/login',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:f.username.value,password:f.password.value})});
      if(r.ok){location.href='/';return;}
      const d=await r.json().catch(()=>({}));
      e.textContent=d.error==='invalid_credentials'
        ?'Incorrect username or password.':'Sign-in failed. Try again.';
    }catch(_){e.textContent='Network error. Try again.';}
    b.disabled=false;b.textContent='Sign in';
  });
</script>
</body></html>
"""
