#!/bin/sh
set -e

# If a command was passed (e.g. `docker compose run --rm garmin-mcp-remote garmin-mcp-auth`),
# run it directly instead of launching the full nginx+supervisord stack. This is what
# makes the one-time interactive auth step work instead of looping into the server.
# The token volume may hold files written by an older, root-run image.
chown -R app:app /data/garminconnect

drop_privileges() {
    exec setpriv --reuid=app --regid=app --init-groups "$@"
}

if [ "$#" -gt 0 ]; then
    drop_privileges "$@"
fi

: "${MCP_BEARER_TOKEN:?MCP_BEARER_TOKEN must be set — generate one with: openssl rand -hex 32}"
: "${MCP_CONSENT_SECRET:?MCP_CONSENT_SECRET must be set (and differ from MCP_BEARER_TOKEN) — generate one with: openssl rand -hex 24}"
# GARMIN_EMAIL / GARMIN_PASSWORD are only required for the one-time `garmin-mcp-auth`
# step (see README). Once OAuth tokens are cached in the garmin-tokens volume, the
# server runs fine without them.

export PORT="${PORT:-8080}"

envsubst '${PORT}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

# supervisord stays root to hand stdout/stderr to its children; garmin-mcp and the
# OAuth shim run as the app user, nginx workers too (see supervisord.conf, nginx.conf).
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
