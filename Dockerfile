FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    gettext-base \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

RUN pip install --no-cache-dir "git+https://github.com/Taxuspt/garmin_mcp"

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMIN_MCP_HOST=127.0.0.1 \
    GARMIN_MCP_PORT=8000 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f http://127.0.0.1:${PORT}/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
