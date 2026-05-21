# Productionizing the SQL Query API

This document is a step-by-step plan for turning the demo into a real,
externally-callable service. Changes are organized into three phases so
you can ship something usable quickly and harden it over time.

---

## What the demo already gets right

Before listing what to change, it's worth noting what to **keep**:

- The async-job / poll pattern is exactly right for long-running queries. Don't replace it with blocking HTTP.
- Read-only SQLite connection (`mode=ro`) is a solid safety layer. Keep it even after migrating to a real DB.
- The `interrupt()` cancellation on `DELETE /jobs/{id}` is correct.
- FastAPI's auto-generated OpenAPI docs (`/docs`) are a free win for external users — keep them.
- The star schema is clean and query-friendly as-is.

---

## Phase 1 — Minimum viable external API

These changes are required before exposing the service to anyone outside localhost.

### 1.1 Real API key management

**Problem:** Keys are hard-coded strings in `main.py`.

**Fix:** Store keys in a secret manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault, or even a Postgres table with hashed keys). Load them at startup, never commit them.

```
API key lifecycle:
  1. Admin creates key via internal CLI / admin endpoint
  2. Key hash stored in DB (bcrypt or SHA-256, depending on lookup needs)
  3. App loads valid keys into memory on startup + refreshes on a schedule
  4. Key can be revoked without a code deploy
```

Each key record should carry: `owner_id`, `name`, `created_at`, `expires_at`, `scopes` (read-only vs admin), `last_used_at`.

For a self-serve model, add a lightweight key-issuance endpoint behind your own auth (OAuth2 / email magic link).

### 1.2 Persistent job storage

**Problem:** All jobs live in a Python dict. Restart = all jobs gone. Single process = no horizontal scaling.

**Fix:** Replace `_jobs: dict[str, Job]` with Redis (simplest) or Postgres.

Redis is the natural fit:
- `HSET jobs:{job_id} status queued submitted_at ... sql ...`
- `EXPIRE jobs:{job_id} 86400` — auto-expire results after 24 h
- `RPUSH queue:pending {job_id}` — pending job queue

Or use **Celery** (backed by Redis) which gives you the queue, worker pool, retry logic, and result backend in one package. The `_execute_job` function maps cleanly onto a Celery task.

```python
# before
_executor.submit(_execute_job, job.id)

# after (Celery)
execute_job.delay(job.id)
```

### 1.3 HTTPS and a real domain

Terminate TLS at a load balancer or reverse proxy (nginx, Caddy, AWS ALB).
Never expose uvicorn directly on port 80/443.

```
Internet → ALB (TLS) → nginx / Caddy → uvicorn workers
```

Use Caddy for simplicity (automatic Let's Encrypt), nginx if you need fine-grained config.

### 1.4 Rate limiting

**Problem:** A single key can submit unlimited queries and pin the workers.

**Fix:** Add per-key rate limiting at the API layer.

Options (pick one):
- **slowapi** (FastAPI-native, wraps limits library): a few lines of middleware
- **nginx `limit_req_zone`**: handled before the app sees the request
- **AWS API Gateway**: handles throttling + quotas if you're on AWS

Start with something like: 60 requests/minute per key, 5 concurrent jobs per key.

```python
# slowapi example
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=lambda req: req.headers.get("X-API-Key", get_remote_address(req)))

@app.post("/query")
@limiter.limit("60/minute")
def submit_query(...): ...
```

### 1.5 Structured logging

Replace `print` (there is none, but add it) with structured JSON logs so you can query them in CloudWatch / Datadog / Loki.

```python
import structlog
log = structlog.get_logger()
log.info("job.submitted", job_id=job.id, key=principal["key"], sql_length=len(body.sql))
log.info("job.done", job_id=job.id, row_count=len(rows), elapsed_s=elapsed)
```

---

## Phase 2 — Operational hardening

Once the service is live and you have a few real users.

### 2.1 Query sandboxing

The read-only connection already blocks writes. Add these on top:

- **Timeout per key tier**: default 30 s, paid/trusted keys get 5 min. Pass as `PRAGMA busy_timeout` and enforce with `asyncio.wait_for` or a threading `Event`.
- **Row limit by tier**: free keys cap at 10k rows, paid at 100k.
- **Query length cap**: reject SQL over ~10 KB.
- **Disallow certain pragmas**: parse for `PRAGMA` statements that could leak schema metadata you don't want exposed. (A denylist is simpler than an allowlist for SQL.)

### 2.2 Result storage for large payloads

**Problem:** Large results are buffered entirely in worker memory, then held there until the client fetches them.

**Fix:** Write completed results to object storage (S3, GCS, R2) and return a pre-signed URL. The in-memory approach is fine for small results; add a threshold (e.g., > 1 MB serialized) to switch automatically.

```
job done
  ├── rows < 1 MB  →  store in Redis / Postgres, return via /result as today
  └── rows ≥ 1 MB  →  write to S3, return {result_url: "https://s3.../...?sig=..."}
                       URL expires in 1 h
```

This also makes CSV downloads much faster (direct S3 → client, no proxy through your app).

### 2.3 Database for the data (if SQLite becomes a bottleneck)

SQLite is surprisingly capable for read-heavy analytical queries, but it has two limits:
- Single writer (irrelevant here since it's read-only)
- No concurrent readers across processes without WAL mode

If you need multiple uvicorn workers reading simultaneously:
```
# Enable WAL mode once after seeding
sqlite3 demo.db "PRAGMA journal_mode=WAL;"
```

For larger datasets or multi-region: migrate to **DuckDB** (drop-in, column-oriented, fast for analytics) or **PostgreSQL** (if you also need row-level security or PostGIS).

### 2.4 Health and readiness endpoints

Required for any load balancer or container orchestrator:

```python
@app.get("/healthz")          # liveness — is the process up?
def health(): return {"ok": True}

@app.get("/readyz")           # readiness — can it serve traffic?
def ready():
    try:
        _connect_ro().close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(503, detail=str(e))
```

### 2.5 Metrics

Expose a `/metrics` endpoint (Prometheus format) with:
- `api_requests_total` — by endpoint, status code, key tier
- `job_duration_seconds` — histogram
- `jobs_in_flight` — gauge
- `job_queue_depth` — gauge

**prometheus-fastapi-instrumentator** adds this in ~5 lines.

### 2.6 Error response standardization

FastAPI's default error shape is `{"detail": "..."}`. Standardize to a consistent envelope so clients can parse errors reliably:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Too many requests. Retry after 14 seconds.",
    "retry_after": 14
  }
}
```

---

## Phase 3 — Scale and multi-tenancy

Only needed once you have real load or paying customers.

### 3.1 Horizontal worker scaling

With Celery + Redis:
- Web tier (`uvicorn`): just enqueues jobs, serves status/results. Scale freely.
- Worker tier (`celery worker`): runs SQL. Scale based on queue depth.
- Run them as separate containers / deployments.

```yaml
# docker-compose sketch
services:
  web:
    build: .
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
  worker:
    build: .
    command: celery -A tasks worker --concurrency 8
  redis:
    image: redis:7-alpine
```

### 3.2 Per-tenant data isolation

If different customers should only see their own data:
- Add a `tenant_id` column to all tables
- Enforce via a query rewrite layer (wrap every submitted SQL in `SELECT * FROM (...) WHERE tenant_id = ?`)
- Or use PostgreSQL row-level security policies

### 3.3 Result pagination

For results > 10k rows, returning everything at once is bad UX. Add cursor-based pagination to `/jobs/{id}/result`:

```
GET /jobs/{id}/result?format=json&limit=1000&after=cursor_token
→ { rows: [...], next_cursor: "...", has_more: true }
```

### 3.4 Webhook / push notifications

Polling is fine for short queries. For long ones (> 30 s), add a `callback_url` field to `POST /query`:

```json
{ "sql": "...", "callback_url": "https://your-service.com/hooks/api-demo" }
```

When the job finishes, POST `{"job_id": "...", "status": "done", "result_url": "..."}` to the callback. Retry with exponential backoff on failure.

### 3.5 API versioning

Add a `/v1/` prefix before GA. FastAPI routers make this easy:

```python
v1 = APIRouter(prefix="/v1")
v1.include_router(query_router)
app.include_router(v1)
```

Keep `/` (root) unversioned. Deprecate old versions with a `Sunset` response header.

---

## Deployment options

| Option | Best for | Notes |
|--------|----------|-------|
| **Railway / Render** | Fastest to ship | Push-to-deploy, managed Redis available, cheap |
| **Fly.io** | Low-latency, multi-region | Good for global distribution, built-in secrets |
| **AWS ECS + ElastiCache** | Enterprise, existing AWS footprint | More ops overhead, more control |
| **GCP Cloud Run** | Serverless, pay-per-request | Cold starts may affect job polling UX |
| **Hetzner VPS + Caddy** | Lowest cost, full control | Single-server, fine until you need HA |

For a straightforward external API, **Railway** or **Fly.io** gets you HTTPS, secrets, Redis, and a domain in under an hour with zero infrastructure code.

---

## Recommended implementation order

1. Persistent jobs (Redis) + real key storage → ship to staging
2. HTTPS + domain
3. Rate limiting
4. Structured logging + health endpoints
5. Metrics + alerting
6. Result storage (S3) for large payloads
7. Webhook callbacks
8. Horizontal worker scaling (Celery)
9. Pagination, versioning, per-tenant isolation (as needed)
