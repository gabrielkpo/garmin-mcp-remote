#!/bin/sh
set -e

: "${MCP_BEARER_TOKEN:?MCP_BEARER_TOKEN must be set — generate one with: openssl rand -hex 32}"
# GARMIN_EMAIL / GARMIN_PASSWORD are only required for the one-time `garmin-mcp-auth`
# step (see README). Once OAuth tokens are cached in the garmin-tokens volume, the
# server runs fine without them.

export PORT="${PORT:-8080}"

envsubst '${PORT} ${MCP_BEARER_TOKEN}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
