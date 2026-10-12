"""Tests for the OAuth 2.1 shim: discovery, registration, PKCE code flow."""

import base64
import hashlib
import os
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("MCP_BEARER_TOKEN", "test-token")

from starlette.testclient import TestClient  # noqa: E402

import oauth_shim  # noqa: E402

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def client():
    oauth_shim._pending_codes.clear()
    return TestClient(oauth_shim.app, base_url="https://mcp.example.com")


def pkce_pair(verifier="a" * 64):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def get_code(client, challenge, state="xyz"):
    resp = client.post(
        "/authorize",
        data={"redirect_uri": REDIRECT, "state": state, "code_challenge": challenge},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return query["code"][0], query.get("state", [None])[0]


def test_discovery_uses_public_host(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["token_endpoint"] == "https://mcp.example.com/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_register_returns_public_client(client):
    resp = client.post("/register", json={"redirect_uris": [REDIRECT]})
    assert resp.status_code == 201
    assert resp.json()["token_endpoint_auth_method"] == "none"


def test_authorize_get_rejects_plain_pkce(client):
    resp = client.get(
        "/authorize",
        params={"redirect_uri": REDIRECT, "code_challenge": "x", "code_challenge_method": "plain"},
    )
    assert resp.status_code == 400


def test_authorize_post_requires_challenge(client):
    resp = client.post("/authorize", data={"redirect_uri": REDIRECT}, follow_redirects=False)
    assert resp.status_code == 400


def test_state_is_url_encoded(client):
    _, challenge = pkce_pair()
    _, state = get_code(client, challenge, state="a b&c=d")
    assert state == "a b&c=d"


def test_full_pkce_flow_returns_bearer_token(client):
    verifier, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    resp = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "test-token"


def test_wrong_verifier_is_rejected(client):
    _, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    resp = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": "wrong"},
    )
    assert resp.status_code == 400


def test_code_is_single_use(client):
    verifier, challenge = pkce_pair()
    code, _ = get_code(client, challenge)
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    assert client.post("/token", data=data).status_code == 200
    assert client.post("/token", data=data).status_code == 400


def test_unknown_grant_type(client):
    resp = client.post("/token", data={"grant_type": "password"})
    assert resp.status_code == 400
