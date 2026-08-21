#!/bin/sh
set -e

# If a command was passed (e.g. `docker compose run --rm garmin-mcp-remote garmin-mcp-auth`),
# run it directly instead of launching the full nginx+supervisord stack. This is what
# makes the one-time interactive auth step work instead of looping into the server.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

: "${MCP_BEARER_TOKEN:?MCP_BEARER_TOKEN must be set — generate one with: openssl rand -hex 32}"
# GARMIN_EMAIL / GARMIN_PASSWORD are only required for the one-time `garmin-mcp-auth`
# step (see README). Once OAuth tokens are cached in the garmin-tokens volume, the
# server runs fine without them.

export PORT="${PORT:-8080}"

envsubst '${PORT} ${MCP_BEARER_TOKEN}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
