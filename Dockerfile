# DSA VLOP Transparency Query API — container image for Cloud Run.
#
# The image is self-contained: the SQLite DB is seeded at build time from the
# vendored dataset snapshot (data/vlop-dsa.json), so the running container has no
# startup dependency on any external data source. Refresh the snapshot with
# scripts/refresh-dataset.sh when the upstream dataset changes.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/demo.db

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code + the vendored dataset snapshot.
COPY main.py seed.py ./
COPY static/ ./static/
COPY data/ ./data/

# Build the read-only SQLite DB into the image.
RUN python seed.py --source data/vlop-dsa.json --db "$DB_PATH"

# Run as a non-root user. The DB is opened mode=ro and the app writes nothing
# under /app, so we deliberately leave /app root-owned and only world-readable —
# the runtime user can read the seeded DB but cannot modify it (defence in depth).
RUN useradd --system --uid 10001 appuser
USER appuser

# Cloud Run sends traffic to $PORT (default 8080) and terminates TLS at its front
# end, setting forwarded headers — trust them for correct client IP / scheme.
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
