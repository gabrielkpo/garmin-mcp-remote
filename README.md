# garmin-mcp-remote

[![CI](https://github.com/gabrielkpo/garmin-mcp-remote/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielkpo/garmin-mcp-remote/actions/workflows/ci.yml)

A turnkey, **remotely-hostable** deployment of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)
(a Model Context Protocol server for Garmin Connect), packaged as a single
Docker image with:

- an **nginx** reverse proxy enforcing bearer-token authentication (the
  upstream server's HTTP transport has none — the maintainers explicitly say
  to put it behind a reverse proxy if you expose it beyond localhost), and
- a minimal **OAuth 2.1 shim** so the server satisfies the MCP Authorization
  spec that clients like claude.ai's custom connector actually require.

Point this at any MCP client that supports remote servers (claude.ai, Claude
Desktop, etc.) and it just works, with your Garmin data staying gated behind
auth the whole way.

## How it works

One container runs three processes (via supervisord):

- `garmin-mcp` — the actual MCP server, bound to `127.0.0.1:8000` **inside**
  the container only. Never reachable from outside directly.
- `oauth-shim` — a tiny OAuth 2.1 authorization server bound to
  `127.0.0.1:8001`, implementing just enough of the spec (discovery, Dynamic
  Client Registration, Authorization Code + PKCE, token exchange) for
  claude.ai and similar clients to connect. It only releases the static
  `MCP_BEARER_TOKEN` (as the "access token") to a caller who typed the
  separate `MCP_CONSENT_SECRET` on the consent page, or who presents a refresh
  token it signed itself — this is a protocol-compliance shim in front of a
  single-user static-token model, not a real multi-tenant auth backend. See
  [Security notes](#security-notes).
- `nginx` — listens on the public port. Routes OAuth discovery/register/
  authorize/token paths to `oauth-shim`, proxies `/healthz` unauthenticated
  (for platform/uptime health checks), and proxies everything else —
  including `/mcp` — only if the request carries a valid bearer token.
  Anything else gets a `401` with a `WWW-Authenticate` header pointing
  clients at the OAuth metadata.

```
                                    ┌─ /healthz ────────────► garmin-mcp:8000 (unauthenticated)
                                    │
internet ──► nginx (public port) ──┼─ /.well-known/*, /register,
                                    │  /authorize, /token ──────► oauth-shim:8001
                                    │
                                    └─ everything else (incl. /mcp),
                                       bearer-token gated ─────► garmin-mcp:8000
```

`garmin-mcp` and `oauth-shim` are never reachable directly — only nginx is
exposed.

## Quickstart (local / VPS / home server)

Requires Docker + Docker Compose.

```bash
git clone <this-repo-url>
cd garmin-mcp-remote
cp .env.example .env
```

Edit `.env`:
- Fill in `GARMIN_EMAIL` / `GARMIN_PASSWORD` (your real Garmin Connect login).
- Generate a bearer token and put it in `MCP_BEARER_TOKEN`:
  ```bash
  openssl rand -hex 32
  ```
- Generate a **different** secret for `MCP_CONSENT_SECRET` (the container
  refuses to start without it, and refuses a value equal to the bearer token).
  You type it once on the consent page when connecting a client:
  ```bash
  openssl rand -hex 24
  ```

**One-time authentication (handles MFA):**

```bash
docker compose run --rm garmin-mcp-remote garmin-mcp-auth
```

Follow the prompts (including the MFA code Garmin emails/texts you). This
stores OAuth tokens in the `garmin-tokens` Docker volume, valid for ~6 months.
You will not need to repeat this until they expire. (Occasional `429` /
rate-limited responses from Garmin's login endpoints during this step are
normal and usually resolve on their own within minutes to an hour — this
script makes a single attempt and stops, it will not hammer Garmin's servers.)

**Then delete `GARMIN_PASSWORD` from `.env`.** The server only needs the cached
tokens; keeping the password would inject it into the container at every start.

**Start the server:**

```bash
docker compose up -d
```

Check it's alive:

```bash
curl http://localhost:8080/healthz
# -> ok

curl http://localhost:8080/mcp
# -> 401, with a WWW-Authenticate header pointing at OAuth discovery

curl -H "Authorization: Bearer <your MCP_BEARER_TOKEN>" \
     -H "Accept: application/json, text/event-stream" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8080/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
# -> MCP protocol response
```

If this is exposed beyond your own LAN, it must be served over HTTPS. See
below for how to get a public HTTPS URL.

## Exposing it publicly

You need a public HTTPS URL pointing at `localhost:8080` for any MCP client
running in the cloud (claude.ai, etc.) to reach this. Two good options:

### Option A — ngrok static domain (free, no domain needed, good for "my
own always-on Mac/PC")

ngrok's free tier includes one permanent static subdomain (e.g.
`your-name.ngrok-free.dev`) that never changes across restarts — no need to
own a domain.

1. Create a free account at [ngrok.com](https://ngrok.com), grab your
   authtoken from the dashboard, and claim a static domain under
   **Domains → New Domain**.
2. `brew install ngrok` (or your OS's equivalent), then:
   ```bash
   ngrok config add-authtoken <your-authtoken>
   ngrok http --url=https://<your-static-domain> 8080
   ```
3. To survive reboots without manual intervention, run it as a background
   service instead of a foreground terminal command. On macOS, a
   `launchd` LaunchAgent works well:

   ```xml
   <!-- ~/Library/LaunchAgents/com.yourname.ngrok-garmin.plist -->
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
       <key>Label</key>
       <string>com.yourname.ngrok-garmin</string>
       <key>ProgramArguments</key>
       <array>
           <string>/opt/homebrew/bin/ngrok</string>
           <string>http</string>
           <string>--url=https://<your-static-domain></string>
           <string>8080</string>
       </array>
       <key>RunAtLoad</key><true/>
       <key>KeepAlive</key><true/>
       <key>StandardOutPath</key><string>/tmp/ngrok-garmin.log</string>
       <key>StandardErrorPath</key><string>/tmp/ngrok-garmin.log</string>
   </dict>
   </plist>
   ```

   ```bash
   launchctl load ~/Library/LaunchAgents/com.yourname.ngrok-garmin.plist
   ```

   Also enable **Docker Desktop → Settings → General → "Start Docker Desktop
   when you sign in"**, and make sure `docker-compose.yml`'s
   `restart: unless-stopped` is in place (it is, by default in this repo).
   With both in place, a full reboot of the machine brings everything back
   up on its own — Docker starts, the container restarts, the tunnel
   reconnects, no manual steps.

   Caveat: this only survives while the host machine stays powered on and
   connected to the internet. If you ever turn it off, the connector breaks
   until you turn it back on.

### Option B — Cloud platform (Railway / Render / Fly.io)

For something that stays up even with your own machine off. All three build
directly from this repo's `Dockerfile` and inject a `PORT` env var that the
container already respects. Set `MCP_BEARER_TOKEN`, `GARMIN_EMAIL`,
`GARMIN_PASSWORD` as secrets/environment variables in the platform's
dashboard, and attach a **persistent volume** mounted at
`/root/.garminconnect` (all three support this) so cached auth tokens
survive redeploys.

The one wrinkle: the initial `garmin-mcp-auth` MFA step is interactive, and
these platforms don't run interactive shells by default against a live
deploy. The reliable path:

1. Deploy once so the volume exists.
2. Use the platform's shell/exec feature (`railway run` / `railway ssh`,
   Render's shell tab, `fly ssh console`) to run `garmin-mcp-auth` once
   inside the running container, against the attached volume.
3. From then on, restarts/redeploys reuse the cached tokens.

Check each platform's current docs for the exact exec command, as these
change. All three also give you HTTPS on your public URL automatically.

Typically costs a few dollars a month for an always-on instance once any
free trial credit runs out (Render's free tier sleeps after inactivity,
which defeats the point for a connector that needs to respond on demand).

## Connecting from claude.ai

Add a **custom connector** in claude.ai's connector settings:

- URL: `https://<your-public-host>/mcp`
- Leave the OAuth Client ID / Secret fields blank.

claude.ai will automatically discover the OAuth endpoints, register itself
as a client, and open a consent page. Type your `MCP_CONSENT_SECRET` and
approve it once and the connector is live. You should not need to touch the connector again
after that, as long as the URL stays the same (which it will, with a static
ngrok domain or a cloud platform's assigned hostname).

If you're wiring up a client that doesn't support OAuth discovery, the
bearer token also works directly, as a header only:
- `Authorization: Bearer <your MCP_BEARER_TOKEN>`

A `?token=` query parameter is **not** accepted: it would end up in access
logs and `Referer` headers.

## Security notes

- **The bearer token gives full read/write access to your Garmin account**
  (sleep, HR, weight, activity GPS tracks, etc.). Treat it like a password:
  long, random, never committed to git, rotated if you suspect it leaked.
- **The token is only released after a consent step.** `/authorize` requires
  `MCP_CONSENT_SECRET` (a value distinct from the bearer token, compared in
  constant time and rate-limited), and `/token` only accepts an authorization
  code bound to a PKCE verifier or a refresh token signed with that secret.
  Knowing the server's URL is no longer enough to obtain the token.
- **Redirect URIs are allowlisted.** Only `https://claude.ai/api/mcp/auth_callback`
  by default; add others with `OAUTH_ALLOWED_REDIRECT_URIS` (comma-separated,
  exact match). The shim is built for one user and one client family.
- **Revoking access:** change `MCP_CONSENT_SECRET` to invalidate every refresh
  token, and change `MCP_BEARER_TOKEN` to invalidate every issued access
  token, then redeploy. Do both if you suspect a leak.
- Always run this behind HTTPS in production. Plain HTTP bearer auth over
  the open internet is trivially sniffable.
- `.env` is gitignored on purpose. Never commit real credentials, the
  bearer token, the consent secret, or any tunnel provider authtoken.
- Your Garmin password is only needed for the initial `garmin-mcp-auth`
  handshake, which produces the OAuth tokens that actually get cached. Remove it
  from `.env` once that step succeeds.
- `docker compose` publishes the port on `127.0.0.1` only: the public entry is the
  tunnel or a TLS reverse proxy, never the LAN. The bearer check runs in the shim
  (constant-time comparison), so the token is not written into the nginx config.
- On a host where the Host header is not fixed by a tunnel, set `PUBLIC_BASE_URL`
  so the OAuth metadata never echoes a client-supplied host.
- Supply chain: the base image is pinned by digest, the upstream `garmin_mcp`
  server by commit (`GARMIN_MCP_REF` in the Dockerfile), and every GitHub Action
  by commit SHA. Review the upstream diff before bumping `GARMIN_MCP_REF`.

## Development

The OAuth shim is covered by unit tests (discovery, dynamic client registration,
consent and redirect allowlist, PKCE code flow, single-use codes, refresh-token
signing and expiry, fail-closed startup). CI runs lint, tests and a Docker build on
every pull request.

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

## Releases

Versions follow [semantic versioning](https://semver.org). Pull request titles use
[Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `chore:`),
and [release-please](https://github.com/googleapis/release-please) opens a release PR
that updates `CHANGELOG.md` and tags the version when merged.

## License

MIT — see [LICENSE](LICENSE).

## Credits

All Garmin Connect integration logic is from
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp). This repo adds
the auth layer (bearer-token gate + OAuth 2.1 shim) and container packaging
needed to expose it safely over the internet to OAuth-requiring MCP clients.
