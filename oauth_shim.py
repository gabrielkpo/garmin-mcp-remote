"""
Minimal OAuth 2.1 authorization server shim for a single-user MCP deployment.

claude.ai's remote MCP connector expects the target server to implement the
MCP Authorization spec (OAuth 2.1 + PKCE + Dynamic Client Registration) --
a static Authorization header or query-string token is not enough for it to
even attempt a connection. This shim exists purely to satisfy that protocol
in front of the real server, which still gates on the single static
MCP_BEARER_TOKEN as before: /token always hands out that same token as the
"access_token", so nginx's existing bearer check needs no changes.

Security model: single-user only. /authorize has no real user identity or
session -- it shows a one-click consent page and issues a code to whoever
clicks it, with no validation that the requester is "you" beyond knowing the
server's URL. That's an acceptable tradeoff for a personal deployment behind
an unguessable/private URL, but this is NOT a general-purpose OAuth server
and must not be exposed as one.
"""
import base64
import hashlib
import html
import os
import secrets
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]
CODE_TTL_SECONDS = 300

# In-memory only -- fine for a single-process, single-user deployment. A
# restart invalidates any in-flight (not yet completed) authorization
# attempt, which just means the user retries the "Connecter" click.
_pending_codes: dict[str, dict] = {}


def _base_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"https://{host}"


async def protected_resource_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
    })


async def authorization_server_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


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
<h2>Autoriser l'accès à Garmin Connect</h2>
<p>« {client} » demande à accéder à vos données Garmin via ce serveur MCP.</p>
<form method="post" action="/authorize">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <button type="submit" style="font-size: 1rem; padding: 0.6rem 1.5rem;">Autoriser</button>
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

    code = secrets.token_urlsafe(32)
    _pending_codes[code] = {
        "code_challenge": code_challenge,
        "exp": time.time() + CODE_TTL_SECONDS,
    }

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
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
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if expected != entry["code_challenge"]:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )
    elif grant_type == "refresh_token":
        pass  # static single-token model: refreshing just re-issues the same token
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return JSONResponse(
        {
            "access_token": BEARER_TOKEN,
            "token_type": "Bearer",
            "expires_in": 15552000,
            "refresh_token": BEARER_TOKEN,
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
