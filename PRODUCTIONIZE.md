# Productionizing the SQL Query API

---

## What's already done

The codebase has been updated with the groundwork needed to deploy externally.
The following items are **complete** — no further code changes required:

| Item | What shipped |
|------|-------------|
| Config from env vars | `DB_PATH`, `ROW_LIMIT`, `WORKER_THREADS`, `QUERY_TIMEOUT_SECONDS`, `REDIS_URL`, `JOB_TTL_SECONDS`, `API_KEYS_JSON` — all read from environment with safe defaults |
| Persistent job storage | `RedisJobStore` used automatically when `REDIS_URL` is set; `MemoryJobStore` is the fallback for local dev |
| Dockerfile | Python 3.12-slim image, `uvicorn` entrypoint, ready to push to any registry |
| docker-compose | Wires web + Redis together; mounts `demo.db` as read-only volume |
| Health endpoints | `GET /healthz` (liveness) and `GET /readyz` (checks DB connection) |
| Smoke tests | 19 tests covering auth, query lifecycle, job isolation, write rejection, and delete. Run with `pytest` — no Redis required |
| seed.py data path | `--source` / `--db` flags so seeding works outside the sibling-repo layout |
| Portal key handling | Portal-issued keys (`/portal/register`) persist in Redis when configured (survive restarts, shared across workers), expire after `ISSUED_KEY_TTL_SECONDS`, are registration-rate-limited per IP/email, and are revocable via `DELETE /portal/key`. Open registration still needs real auth (SSO) before public exposure. |
| `.env.example` | Documents every config variable |

---

## How to deploy today

### 1. Seed the database

```bash
# Default: reads from the sibling krMaynard.github.io repo
python seed.py

# Or point at the JSON directly
python seed.py --source /path/to/google-government-removals.json --db demo.db
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` — at minimum replace the `API_KEYS_JSON` value with real keys:

```
API_KEYS_JSON={"sk-prod-abc123":{"name":"alice"},"sk-prod-def456":{"name":"bob"}}
```

### 3. Run with Docker Compose

```bash
# demo.db must exist first (step 1)
docker-compose up --build
```

The web service starts on port 8000 with Redis wired in automatically.
Check `GET /readyz` to confirm the DB connection is live before sending traffic.

### 4. Put HTTPS in front of it

Never expose uvicorn directly on 443. Options:

- **Caddy** (recommended for simplicity — automatic Let's Encrypt):

  ```
  api.yourdomain.com {
      reverse_proxy web:8000
  }
  ```

  Add a `caddy` service to `docker-compose.yml` and mount a `Caddyfile`.

- **AWS ALB / GCP Load Balancer**: terminate TLS at the load balancer, forward HTTP to the container.

- **Railway / Fly.io**: HTTPS is automatic — they provision the cert. See [Deployment options](#deployment-options) below.

### 5. Use a real secret manager for API keys

The `API_KEYS_JSON` env var is a stop-gap. In production, load keys from a secret store at startup and refresh on a schedule so keys can be rotated without a redeploy:

```python
# Example: AWS Secrets Manager
import boto3, json

def _load_api_keys():
    secret = boto3.client("secretsmanager").get_secret_value(SecretId="api-demo/keys")
    return json.loads(secret["SecretString"])
```

Each key record should carry: `owner_id`, `name`, `created_at`, `expires_at`, `scopes`, `last_used_at`.

---

## What's still left to do

These are ordered by priority — stop when you've reached the level of hardening you need.

### Priority 1 — Before accepting external traffic

#### Rate limiting

`POST /query` is throttled per API key (`QUERY_RATE_MAX_PER_WINDOW` per
`QUERY_RATE_WINDOW_SECONDS`, default 60/60s) using the same counter primitive as
portal registration — over-limit requests get `429` + a `Retry-After` header
before any job is spawned. The counter is Redis-backed when configured (shared
across workers) and in-memory otherwise. For multi-endpoint or burst policies
you may still want an edge limiter (nginx / Caddy / API Gateway) or `slowapi`.

#### Structured logging

JSON logs are emitted out of the box (`LOG_FORMAT=json`, the default; set
`LOG_FORMAT=text` for human-readable lines). Each HTTP request logs method,
path, status, `duration_ms`, and a `request_id` (also returned as the
`X-Request-ID` response header); the job runner logs `job_submitted` /
`job_started` / `job_done` / `job_failed` with `job_id`, row count, and
`duration_ms`. API keys are never logged. Point your collector
(CloudWatch / Datadog / Loki) at stdout.

### Priority 2 — Once you have users

#### Enable SQLite WAL mode

Allows multiple uvicorn workers to read simultaneously without blocking:

```bash
# Run once after seeding, before starting the service
sqlite3 demo.db "PRAGMA journal_mode=WAL;"
```

#### Query sandboxing

The read-only connection already blocks writes. Add on top:

- Per-key timeout (default 30 s; trusted keys get longer)
- Query length cap (reject SQL > ~10 KB)
- Pragma denylist (block `PRAGMA` statements that leak internal schema)

#### Metrics

```bash
pip install prometheus-fastapi-instrumentator
```

```python
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)
```

Exposes `/metrics` in Prometheus format. Add custom gauges for `jobs_in_flight` and `job_queue_depth`.

#### Result offload for large payloads

Results are buffered in Redis. For rows > ~1 MB serialized, write to S3/GCS and return a pre-signed URL instead:

```
job done
  ├── result < 1 MB  →  Redis, served via /jobs/{id}/result as today
  └── result ≥ 1 MB  →  S3 + pre-signed URL (expires 1 h)
```

### Priority 3 — At scale

#### Horizontal worker scaling (Celery)

The current `ThreadPoolExecutor` is in-process. To scale workers independently:

```yaml
# docker-compose addition
worker:
  build: .
  command: celery -A tasks worker --concurrency 8
  environment:
    - REDIS_URL=redis://redis:6379/0
```

`_execute_job` maps cleanly onto a Celery task — swap `_executor.submit(...)` for `execute_job.delay(...)`.

#### Result pagination

```
GET /jobs/{id}/result?format=json&limit=1000&after=<cursor>
→ { rows: [...], next_cursor: "...", has_more: true }
```

#### Webhook callbacks

Add `callback_url` to `POST /query`. When the job finishes, POST the result URL to the callback with exponential-backoff retries.

#### API versioning

```python
v1 = APIRouter(prefix="/v1")
app.include_router(v1)
```

Deprecate old versions with a `Sunset` response header.

---

## Deployment options

| Option | Best for | Notes |
|--------|----------|-------|
| **Railway** | Fastest to ship | Push-to-deploy, managed Redis, free tier available |
| **Fly.io** | Low-latency, multi-region | Built-in secrets, automatic HTTPS, good free tier |
| **AWS ECS + ElastiCache** | Existing AWS footprint | More ops overhead, more control |
| **GCP Cloud Run** | Serverless | Cold starts can affect polling UX |
| **Hetzner VPS + Caddy** | Lowest cost, full control | Single server, fine until you need HA |

**Railway** and **Fly.io** are the fastest paths: push the repo, set the env vars in their dashboard, provision a Redis add-on, and you have HTTPS + a domain in under an hour.

#### Railway quickstart

```bash
npm install -g @railway/cli
railway login
railway init
railway add --plugin redis
railway up
# Set env vars in the Railway dashboard under Variables
```

#### Fly.io quickstart

```bash
brew install flyctl
fly auth login
fly launch          # detects Dockerfile automatically
fly secrets set API_KEYS_JSON='{"sk-...":{"name":"alice"}}'
fly redis create    # provision Redis, copy the URL
fly secrets set REDIS_URL=redis://...
fly deploy
```
