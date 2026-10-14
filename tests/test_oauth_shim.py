"""Tests for the OAuth 2.1 shim: discovery, registration, consent and PKCE flow.

The shim must never release the bearer token to a caller who has not proved
knowledge of MCP_CONSENT_SECRET (directly, or through a refresh token it signed).
"""

import base64
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

BEARER = "test-bearer-token-aaaaaaaaaaaaaaaa"
CONSENT = "test-consent-secret-0123456789"
os.environ.setdefault("MCP_BEARER_TOKEN", BEARER)
os.environ.setdefault("MCP_CONSENT_SECRET", CONSENT)

from starlette.testclient import TestClient  # noqa: E402

import oauth_shim  # noqa: E402

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    oauth_shim._pending_codes.clear()
    oauth_shim._failed_consents.clear()
    return TestClient(oauth_shim.app, base_url="https://mcp.example.com")


def pkce_pair(verifier="a" * 64):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def authorize(client, challenge, state="xyz", secret=None, redirect_uri=REDIRECT):
    return client.post(
        "/authorize",
        data={
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "consent_secret": oauth_shim.CONSENT_SECRET if secret is None else secret,
        },
        follow_redirects=False,
    )


def get_code(client, challenge, state="xyz"):
    resp = authorize(client, challenge, state=state)
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return query["code"][0], query.get("state", [None])[0]


def full_flow(client):
    verifier, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    return client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
    ).json()


# --- discovery / registration -------------------------------------------------


def test_discovery_uses_public_host(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["token_endpoint"] == "https://mcp.example.com/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_register_returns_public_client(client):
    resp = client.post("/register", json={"redirect_uris": [REDIRECT]})
    assert resp.status_code == 201
    assert resp.json()["token_endpoint_auth_method"] == "none"


# --- authorize: consent and redirect allowlist --------------------------------


def test_authorize_get_rejects_plain_pkce(client):
    resp = client.get(
        "/authorize",
        params={"redirect_uri": REDIRECT, "code_challenge": "x", "code_challenge_method": "plain"},
    )
    assert resp.status_code == 400


def test_authorize_get_asks_for_the_consent_secret(client):
    resp = client.get("/authorize", params={"redirect_uri": REDIRECT, "code_challenge": "x"})
    assert resp.status_code == 200
    assert 'name="consent_secret"' in resp.text


def test_authorize_post_requires_challenge(client):
    resp = client.post(
        "/authorize",
        data={"redirect_uri": REDIRECT, "consent_secret": CONSENT},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_authorize_without_consent_secret_is_denied(client):
    _, challenge = pkce_pair()
    resp = client.post(
        "/authorize",
        data={"redirect_uri": REDIRECT, "code_challenge": challenge},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert not oauth_shim._pending_codes


def test_authorize_with_wrong_consent_secret_is_denied(client):
    _, challenge = pkce_pair()
    resp = authorize(client, challenge, secret="not-the-secret")
    assert resp.status_code == 403
    assert "location" not in resp.headers
    assert not oauth_shim._pending_codes


def test_bearer_token_is_not_accepted_as_consent_secret(client):
    _, challenge = pkce_pair()
    resp = authorize(client, challenge, secret=oauth_shim.BEARER_TOKEN)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "bad_redirect",
    ["https://evil.example.com/cb", "http://claude.ai/api/mcp/auth_callback", REDIRECT + "/x"],
)
def test_authorize_rejects_redirects_outside_allowlist(client, bad_redirect):
    _, challenge = pkce_pair()
    post = authorize(client, challenge, redirect_uri=bad_redirect)
    assert post.status_code == 400
    get = client.get("/authorize", params={"redirect_uri": bad_redirect, "code_challenge": challenge})
    assert get.status_code == 400
    assert not oauth_shim._pending_codes


def test_repeated_wrong_secrets_are_throttled(client):
    _, challenge = pkce_pair()
    for _ in range(oauth_shim.MAX_FAILED_CONSENTS):
        assert authorize(client, challenge, secret="guess").status_code == 403
    # Even the correct secret is refused while the throttle window is open.
    assert authorize(client, challenge).status_code == 429


def test_state_is_url_encoded(client):
    _, challenge = pkce_pair()
    _, state = get_code(client, challenge, state="a b&c=d")
    assert state == "a b&c=d"


# --- token: authorization_code ------------------------------------------------


def test_full_pkce_flow_returns_bearer_token(client):
    body = full_flow(client)
    assert body["access_token"] == oauth_shim.BEARER_TOKEN


def test_refresh_token_is_not_the_bearer_token(client):
    body = full_flow(client)
    assert body["refresh_token"] != oauth_shim.BEARER_TOKEN
    assert oauth_shim.BEARER_TOKEN not in body["refresh_token"]


def test_wrong_verifier_is_rejected(client):
    _, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    resp = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": "wrong"},
    )
    assert resp.status_code == 400
    assert "access_token" not in resp.json()


def test_code_is_single_use(client):
    verifier, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    assert client.post("/token", data=data).status_code == 200
    assert client.post("/token", data=data).status_code == 400


def test_unknown_code_is_rejected(client):
    resp = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": "nope", "code_verifier": "a" * 64},
    )
    assert resp.status_code == 400
    assert "access_token" not in resp.json()


def test_redirect_uri_must_match_at_the_token_endpoint(client):
    verifier, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://claude.ai/other",
        },
    )
    assert resp.status_code == 400


def test_unknown_grant_type(client):
    resp = client.post("/token", data={"grant_type": "password"})
    assert resp.status_code == 400


# --- token: refresh_token -----------------------------------------------------


def test_refresh_without_token_is_rejected(client):
    resp = client.post("/token", data={"grant_type": "refresh_token"})
    assert resp.status_code == 400
    assert "access_token" not in resp.json()


@pytest.mark.parametrize(
    "forged",
    ["", "garbage", "v1.1.2.3", oauth_shim.BEARER_TOKEN, "v1.9999999999.abc." + "0" * 64],
)
def test_forged_refresh_tokens_are_rejected(client, forged):
    resp = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": forged})
    assert resp.status_code == 400
    assert oauth_shim.BEARER_TOKEN not in resp.text


def test_issued_refresh_token_works_and_rotates(client):
    first = full_flow(client)
    resp = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert resp.status_code == 200
    second = resp.json()
    assert second["access_token"] == oauth_shim.BEARER_TOKEN
    assert second["refresh_token"] != oauth_shim.BEARER_TOKEN


def test_expired_refresh_token_is_rejected(client, monkeypatch):
    first = full_flow(client)
    later = time.time() + oauth_shim.REFRESH_TTL_SECONDS + 10
    monkeypatch.setattr(oauth_shim.time, "time", lambda: later)
    resp = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert resp.status_code == 400


def test_tampered_signature_is_rejected(client):
    token = full_flow(client)["refresh_token"]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    resp = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tampered})
    assert resp.status_code == 400


# --- startup: fail closed -----------------------------------------------------


def _import_in_subprocess(env_overrides):
    env = {k: v for k, v in os.environ.items() if k not in ("MCP_CONSENT_SECRET", "MCP_BEARER_TOKEN")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import oauth_shim"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_refuses_to_start_without_consent_secret():
    result = _import_in_subprocess({"MCP_BEARER_TOKEN": BEARER})
    assert result.returncode != 0
    assert "MCP_CONSENT_SECRET" in result.stderr


def test_refuses_a_short_consent_secret():
    result = _import_in_subprocess({"MCP_BEARER_TOKEN": BEARER, "MCP_CONSENT_SECRET": "short"})
    assert result.returncode != 0


def test_refuses_consent_secret_equal_to_bearer_token():
    same = "x" * 40
    result = _import_in_subprocess({"MCP_BEARER_TOKEN": same, "MCP_CONSENT_SECRET": same})
    assert result.returncode != 0
    assert "differ" in result.stderr
