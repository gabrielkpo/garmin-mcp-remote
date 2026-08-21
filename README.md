# garmin-mcp-remote

A turnkey, **remotely-hostable** deployment of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)
(a Model Context Protocol server for Garmin Connect), packaged as a single Docker
image with an nginx reverse proxy in front that enforces bearer-token
authentication.

This exists because the upstream server's HTTP transport has **no built-in
authentication** — the maintainers explicitly say to put it behind a reverse
proxy if you expose it beyond localhost. This repo is that reverse proxy,
pre-wired.

## How it works

One container runs two processes (via supervisord):

- `garmin-mcp` — the actual MCP server, bound to `127.0.0.1:8000` **inside**
  the container only. It is never reachable from outside directly.
- `nginx` — listens on the public port, proxies `/healthz` unauthenticated
  (for platform health checks), and proxies everything else **only** if the
  request carries `Authorization: Bearer <MCP_BEARER_TOKEN>`. Anything else
  gets a `401`.

```
internet → nginx (public port, checks bearer token) → garmin-mcp (127.0.0.1:8000, never exposed)
```

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

**One-time authentication (handles MFA):**

```bash
docker compose run --rm garmin-mcp-remote garmin-mcp-auth
```

Follow the prompts (including the MFA code Garmin emails/texts you). This
stores OAuth tokens in the `garmin-tokens` Docker volume, valid for ~6 months.
You will not need to repeat this until they expire.

**Start the server:**

```bash
docker compose up -d
```

Check it's alive:

```bash
curl http://localhost:8080/healthz
# -> ok

curl -H "Authorization: Bearer WRONG" http://localhost:8080/mcp
# -> 401

curl -H "Authorization: Bearer <your MCP_BEARER_TOKEN>" http://localhost:8080/mcp
# -> MCP protocol response
```

If this is exposed beyond your own LAN, put it behind HTTPS (e.g. Caddy,
Traefik, or your cloud platform's built-in TLS termination — see below).
Sending a bearer token over plain HTTP on the open internet defeats the
point.

## Deploying to Railway / Render / Fly.io

All three build directly from this repo's `Dockerfile` and inject a `PORT`
env var that the container already respects. Set `MCP_BEARER_TOKEN`,
`GARMIN_EMAIL`, `GARMIN_PASSWORD` as secrets/environment variables in the
platform's dashboard, and attach a **persistent volume** mounted at
`/root/.garminconnect` (all three platforms support this) so cached auth
tokens survive redeploys.

The one wrinkle: the initial `garmin-mcp-auth` MFA step is interactive, and
these platforms don't run interactive shells by default against a live
deploy. The reliable path is:

1. Deploy once so the volume exists.
2. Use the platform's shell/exec feature (`railway run` / `railway ssh`,
   Render's shell tab, `fly ssh console`) to run `garmin-mcp-auth` once
   inside the running container, against the attached volume.
3. From then on, restarts/redeploys reuse the cached tokens.

Check each platform's current docs for the exact exec command, as these
change. All three also give you HTTPS on your public URL automatically,
which you should use instead of plain HTTP.

## Connecting from claude.ai

In claude.ai's connector settings, add a **custom connector**:

- URL: `https://<your-deployed-host>/mcp`
- Header: `Authorization: Bearer <your MCP_BEARER_TOKEN>`

## Security notes

- **The bearer token is the only thing standing between the internet and
  full read/write access to your Garmin account** (sleep, HR, weight,
  activity GPS tracks, etc.). Treat it like a password: long, random, never
  committed to git, rotated if you suspect it leaked.
- To revoke access, change `MCP_BEARER_TOKEN` and redeploy — old tokens stop
  working immediately.
- Always run this behind HTTPS in production. Plain HTTP bearer auth over
  the open internet is trivially sniffable.
- `.env` is gitignored on purpose. Never commit real credentials or the
  bearer token.
- This repo does not store your Garmin password anywhere persistent — it's
  only used transiently for the initial `garmin-mcp-auth` handshake, which
  produces OAuth tokens that are what actually get cached.

## Credits

All Garmin Connect integration logic is from
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp). This repo just
adds the auth layer and container packaging needed to expose it safely over
the internet.
