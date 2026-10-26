# Base image and upstream server pinned so a rebuild never pulls unreviewed code:
# this container holds a full Garmin Connect session. Dependabot bumps the digest.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# Taxuspt/garmin_mcp commit, reviewed before each bump.
ARG GARMIN_MCP_REF=30324f70ccb50c2570126501df17acf0b5572083

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    gettext-base \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# The MCP server, the OAuth shim and the nginx workers run as an unprivileged user.
# Root only renders the nginx config, fixes the token volume ownership and supervises.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /data/garminconnect \
    && chown app:app /data/garminconnect

RUN pip install --no-cache-dir "git+https://github.com/Taxuspt/garmin_mcp@${GARMIN_MCP_REF}"

COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY oauth_shim.py /oauth_shim.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMINTOKENS=/data/garminconnect \
    HOME=/home/app \
    GARMIN_MCP_HOST=127.0.0.1 \
    GARMIN_MCP_PORT=8000 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f http://127.0.0.1:${PORT}/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
