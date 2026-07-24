FROM python:3.12-slim AS css
WORKDIR /build
# Split so a network blip in the pip install fails the layer instead of being
# masked by `|| true` and poisoning the cache with a tailwindcss-less layer.
RUN pip install --no-cache-dir pytailwindcss
RUN tailwindcss --help >/dev/null 2>&1 || true
COPY tailwind.css ./
COPY css ./css
COPY app/templates ./app/templates
RUN tailwindcss -i tailwind.css -o app.css --minify

FROM python:3.12-slim
WORKDIR /srv
# gosu lets the entrypoint drop from root to an unprivileged user for the app
# process (see docker-entrypoint.sh) after fixing /data ownership.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
COPY --from=css /build/app.css ./app/static/app.css
ARG APP_VERSION=dev
ENV DATA_DIR=/data RUN_SCHEDULER=1 APP_VERSION=$APP_VERSION
EXPOSE 8000
ENTRYPOINT ["/docker-entrypoint.sh"]
# Probe the DB-backed /health so an orchestrator sees "unhealthy" when SQLite is
# locked/corrupt or /data failed to mount, not just when the process is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# --proxy-headers so the app trusts X-Forwarded-* behind the reverse proxy and
# records the real client IP (login audit / rate-limit keys) instead of the proxy's.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
