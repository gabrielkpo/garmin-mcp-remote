"""
Minimal OAuth 2.1 authorization server shim for a single-user MCP deployment.

claude.ai's remote MCP connector expects the target server to implement the
MCP Authorization spec (OAuth 2.1 + PKCE + Dynamic Client Registration) --
a static Authorization header or query-string token is not enough for it to
even attempt a connection. This shim exists purely to satisfy that protocol
in front of the real server, which still gates on the single static
MCP_BEARER_TOKEN: /token hands that token out as the "access_token", so
nginx's existing bearer check needs no changes.

Security model: single-user only, and the token is only ever released to a
caller who proves one of two things:

* knowledge of MCP_CONSENT_SECRET, typed on the consent page (a secret that is
  distinct from the bearer token and never sent to clients), or
* possession of a refresh token that this server signed itself (HMAC keyed by
  MCP_CONSENT_SECRET), which is itself only issued after a successful consent.

The redirect URI must also be on an allowlist, so a consent cannot be sent to an
attacker-controlled site. Rotating MCP_CONSENT_SECRET revokes every refresh
token. This is still NOT a general-purpose OAuth server.
"""

import base64
import hashlib
import hmac
import html
import os
import secrets
import sys
import time
from urllib.parse import urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]
CODE_TTL_SECONDS = 300
REFRESH_TTL_SECONDS = 15552000  # ~180 days, same as the advertised access token life
MIN_CONSENT_SECRET_LENGTH = 16
MAX_FAILED_CONSENTS = 5
FAILED_CONSENT_WINDOW_SECONDS = 300

DEFAULT_ALLOWED_REDIRECT_URIS = "https://claude.ai/api/mcp/auth_callback"


def _load_consent_secret() -> str:
    """Fail closed: the shim refuses to start without a strong, distinct secret."""
    value = os.environ.get("MCP_CONSENT_SECRET", "")
    if len(value) < MIN_CONSENT_SECRET_LENGTH:
        sys.exit(
            "MCP_CONSENT_SECRET must be set to at least "
            f"{MIN_CONSENT_SECRET_LENGTH} characters (generate one with: openssl rand -hex 24)"
        )
    if hmac.compare_digest(value, BEARER_TOKEN):
        sys.exit("MCP_CONSENT_SECRET must differ from MCP_BEARER_TOKEN")
    return value


CONSENT_SECRET = _load_consent_secret()
ALLOWED_REDIRECT_URIS = frozenset(
    uri.strip()
    for uri in os.environ.get("OAUTH_ALLOWED_REDIRECT_URIS", DEFAULT_ALLOWED_REDIRECT_URIS).split(",")
    if uri.strip()
)

# In-memory only -- fine for a single-process, single-user deployment. A
# restart invalidates any in-flight (not yet completed) authorization
# attempt, which just means the user retries the "Connect" click.
_pending_codes: dict[str, dict] = {}
_failed_consents: list[float] = []


def _too_many_failures() -> bool:
    now = time.time()
    _failed_consents[:] = [t for t in _failed_consents if now - t < FAILED_CONSENT_WINDOW_SECONDS]
    return len(_failed_consents) >= MAX_FAILED_CONSENTS


def _sign(payload: str) -> str:
    return hmac.new(CONSENT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _issue_refresh_token() -> str:
    """Stateless signed refresh token: v1.<issued_at>.<nonce>.<hmac>."""
    payload = f"v1.{int(time.time())}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_sign(payload)}"


def _refresh_token_is_valid(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return False
    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(payload), parts[3]):
        return False
    try:
        issued_at = int(parts[1])
    except ValueError:
        return False
    return 0 <= time.time() - issued_at <= REFRESH_TTL_SECONDS


def _base_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"https://{host}"


async def protected_resource_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
        }
    )


async def authorization_server_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


async def register(request: Request):
    # Dynamic Client Registration (RFC 7591). Every caller is accepted as a
    # public (secret-less) client -- PKCE is what actually protects the
    # token exchange, not client authentication.
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(16)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


_CONSENT_PAGE = """<!doctype html>
<html><body style="font-family: system-ui; max-width: 32rem; margin: 4rem auto; text-align: center;">
<h2>Authorize access to Garmin Connect</h2>
<p>"{client}" is requesting access to your Garmin data through this MCP server.</p>
<form method="post" action="/authorize">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <p><input type="password" name="consent_secret" placeholder="Consent secret"
            autocomplete="current-password" required
            style="font-size: 1rem; padding: 0.5rem; width: 16rem;"></p>
  <button type="submit" style="font-size: 1rem; padding: 0.6rem 1.5rem;">Authorize</button>
</form>
</body></html>"""


async def authorize_get(request: Request):
    q = request.query_params
    redirect_uri = q.get("redirect_uri")
    code_challenge = q.get("code_challenge")
    code_challenge_method = q.get("code_challenge_method", "S256")
    state = q.get("state", "")
    client_id = q.get("client_id", "this client")

    if not redirect_uri or not code_challenge:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if redirect_uri not in ALLOWED_REDIRECT_URIS:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri is not allowed"},
            status_code=400,
        )
    if code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "only S256 is supported"},
            status_code=400,
        )

    return HTMLResponse(
        _CONSENT_PAGE.format(
            client=html.escape(client_id),
            redirect_uri=html.escape(redirect_uri, quote=True),
            state=html.escape(state, quote=True),
            code_challenge=html.escape(code_challenge, quote=True),
        )
    )


async def authorize_post(request: Request):
    form = await request.form()
    redirect_uri = form.get("redirect_uri")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge")
    if not redirect_uri or not code_challenge:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if redirect_uri not in ALLOWED_REDIRECT_URIS:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri is not allowed"},
            status_code=400,
        )
    if _too_many_failures():
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "too many failed attempts"},
            status_code=429,
        )
    supplied = str(form.get("consent_secret", ""))
    if not hmac.compare_digest(supplied.encode(), CONSENT_SECRET.encode()):
        _failed_consents.append(time.time())
        return JSONResponse({"error": "access_denied"}, status_code=403)

    code = secrets.token_urlsafe(32)
    _pending_codes[code] = {
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "exp": time.time() + CODE_TTL_SECONDS,
    }

    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}{urlencode(params)}"
    return RedirectResponse(location, status_code=302)


async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code")
        verifier = form.get("code_verifier", "")
        entry = _pending_codes.pop(code, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, entry["code_challenge"]):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )
        presented = form.get("redirect_uri")
        if presented and presented != entry["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    elif grant_type == "refresh_token":
        presented_refresh = str(form.get("refresh_token", ""))
        if not _refresh_token_is_valid(presented_refresh):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return JSONResponse(
        {
            "access_token": BEARER_TOKEN,
            "token_type": "Bearer",
            "expires_in": 15552000,
            "refresh_token": _issue_refresh_token(),
        }
    )


app = Starlette(
    routes=[
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
        Route("/.well-known/oauth-authorization-server", authorization_server_metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
