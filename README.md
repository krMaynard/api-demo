# api-demo

[![CI](https://github.com/krMaynard/api-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/krMaynard/api-demo/actions/workflows/ci.yml)

A FastAPI service that lets a researcher describe a query with **structured
parameters** (no SQL), runs it asynchronously on a worker thread, and serves the
results back as JSON or CSV. Backed by a read-only SQLite database seeded from the
aggregated **EU Digital Services Act (DSA) VLOP transparency reports** —
content-moderation statistics for 33 designated Very Large Online Platforms /
Search Engines (H2 2025), tables 3–11 of the DSA Implementing Regulation template
(`../krMaynard.github.io/data/vlop-dsa.json`).

A query names one of the 9 DSA **report tables** (`GET /tables`) and then
describes filters, group-bys, and aggregates over that table's fields.

## Demo walkthrough

The full `demo.py` walkthrough — auth, discovery, structured query, polling,
result, secure download, validation, and isolation:

![Full walkthrough](docs/gifs/full.gif)

### Main steps

| Submit a structured query | Poll until done |
|---|---|
| ![Submit](docs/gifs/step-05-submit-a-structured-query-post-query-ret.gif) | ![Poll](docs/gifs/step-06-poll-get-jobs-job-id-until-status-done.gif) |
| **Fetch the result (JSON)** | **Secure download — signed URL, no API key** |
| ![Result](docs/gifs/step-07-fetch-the-result-as-json.gif) | ![Download](docs/gifs/step-08-secure-download-signed-url-no-api-key.gif) |
| **Discover queryable fields** | **Invalid query → 400 (no arbitrary SQL)** |
| ![Fields](docs/gifs/step-04-discover-a-table-s-fields-get-fields-tab.gif) | ![Invalid](docs/gifs/step-10-invalid-query-400-no-sql-no-unknown-fiel.gif) |

A GIF for every step lives in [`docs/gifs/`](docs/gifs/). They're generated
headlessly from `demo.py` — see [Regenerating the showcase GIFs](#regenerating-the-showcase-gifs).

## Researcher portal

A self-service web portal at **`/portal`**: a researcher signs in with their
name and email, is issued a working API key, and the page browses the dataset
schema (queryable fields + tables/columns) using that key.

![Researcher portal workflow](docs/gifs/portal-full.gif)

| Sign in | API key issued | Schema |
|---|---|---|
| ![Login](docs/gifs/portal-1-login.gif) | ![Key](docs/gifs/portal-2-key.gif) | ![Schema](docs/gifs/portal-3-schema.gif) |

Open `http://127.0.0.1:8000/portal` after starting the server. It's a demo
onboarding flow — there's no real authentication (production would sit behind
SSO) — but the key handling is production-shaped:

- `POST /portal/register` issues a key that **expires** after
  `ISSUED_KEY_TTL_SECONDS` (default 30 days) and works on every API endpoint,
  exactly like the built-in `alice`/`bob` keys.
- Issued keys are **persisted in Redis** when `REDIS_URL`/Upstash is configured
  (so they survive restarts and are shared across workers), falling back to
  in-memory otherwise — same model as the job store.
- Registration is **rate-limited** per client IP and per email
  (`PORTAL_REGISTER_MAX_PER_WINDOW` per `PORTAL_REGISTER_WINDOW_SECONDS`).
- `DELETE /portal/key` lets a holder **revoke** their own issued key.

## No SQL — structured query parameters

Clients never send SQL. They describe what they want with structured
parameters modelled on the [TikTok Research API](https://developers.tiktok.com/doc/research-api-specs-query-videos/):
a boolean `query` of `and` / `or` / `not` clauses, where each clause is a
`{operation, field_name, field_values}` condition, plus optional `group_by`,
`aggregates`, `sort`, and `max_count`.

A query names a `table` (one of the 9 DSA report tables). The server validates
every field and operation against **that table's** fixed registry
(`GET /fields?table=…`) and compiles the request into a **single parameterised
SELECT** — values are always bound, never interpolated. Unknown fields, bad
operations, or injection attempts in values are rejected with `400` (or, for
values, bound harmlessly as data). There is no code path that executes
caller-authored SQL.

```jsonc
// POST /query — "top 5 platforms by Art. 16 notices received" (table t4_notices)
{
  "table": "t4_notices",
  "query": {
    "and": [
      { "operation": "EQ", "field_name": "category_code", "field_values": ["TOTAL"] }
    ]
  },
  "group_by": ["service_name"],
  "aggregates": [
    { "function": "SUM", "field_name": "notices", "alias": "notices" }
  ],
  "sort": [{ "field_name": "notices", "order": "desc" }],
  "max_count": 5
}
```

### Query language

- **`table`** — which DSA report table to query (required). See `GET /tables`.
- **`query`** — `{ "and": [...], "or": [...], "not": [...] }`. Each list holds
  conditions; `and` are ANDed, `or` are ORed together, `not` are negated, and
  the three groups are combined with AND. All optional.
- **Condition** — `{ "operation", "field_name", "field_values" }`.
  - Operations: `EQ`, `IN` (all fields); `GT`, `GTE`, `LT`, `LTE` (numeric
    measures only). `field_values` is always a list.
- **`fields`** — columns to return for a raw (non-aggregated) query. Defaults
  to every field of the table. Cannot be combined with `group_by`/`aggregates`.
- **`group_by`** — dimension fields to group on.
- **`aggregates`** — `{ "function": SUM|COUNT|AVG|MIN|MAX, "field_name", "alias" }`.
- **`sort`** — `[{ "field_name", "order": asc|desc }]` over output columns.
- **`max_count`** — row cap (default 100, capped at `ROW_LIMIT`).

Call `GET /fields` for the full list of queryable dimensions, measures, and
operations.

## Why an async job pattern?

A query against a large dataset can take seconds or minutes. If the API held
the HTTP connection open the whole time, slow queries would tie up workers,
time out at intermediate proxies, and stall the service.

So `POST /query` does **not** return rows. It validates the structured query,
returns `202 Accepted` plus a `job_id` immediately, runs the compiled query on
a background worker, and the client polls `/jobs/{job_id}` until it sees
`status="done"`. Then it fetches `/jobs/{job_id}/result?format=json|csv`.

```
client                          server
  │                               │
  │── POST /query ───────────────▶│   validate + compile, enqueue
  │◀── 202 + {job_id, status_url} │   (invalid query → 400, no job)
  │                               │   ┌─ worker thread
  │                               │   │   open ro conn
  │── GET /jobs/{id} ────────────▶│   │   execute parameterised SQL
  │◀── {status: running}          │   │   buffer rows
  │── GET /jobs/{id} ────────────▶│   │
  │◀── {status: done, result_url} │◀──┘
  │── GET /jobs/{id}/result ─────▶│
  │◀── rows (json or csv)         │
```

### Webhook callbacks (poll *or* get notified)

Polling is the default, but you can also include a `callback_url` in the
`POST /query` body. When the job reaches a terminal state the server POSTs the
job object (including signed `download_urls`) to that URL, so you don't have to
poll:

```jsonc
POST /query
{ "aggregates": [{"function": "COUNT", "alias": "n"}],
  "callback_url": "https://your-service.example.com/hooks/jobs" }

// later, server → your URL:
POST /hooks/jobs
X-Webhook-Signature: sha256=<hmac of the raw body>
{ "event": "job.done", "job": { "job_id": "…", "status": "done", "download_urls": {…} } }
```

- **Verify the signature**: recompute `HMAC-SHA256(DOWNLOAD_URL_SECRET, raw_body)`
  and compare to the `X-Webhook-Signature` header before trusting the payload.
- **Delivery** is retried with backoff (`CALLBACK_MAX_ATTEMPTS`) off the query
  worker threads; `done` and `failed` jobs notify, cancellations don't.
- **SSRF-guarded**: the URL must be plain `http(s)` to a public host. Callbacks to
  loopback / private / link-local / cloud-metadata addresses are rejected with
  `400` at submit and re-checked before each send (set `CALLBACK_ALLOW_PRIVATE=1`
  only for local development).
- Set `PUBLIC_BASE_URL` so the links in the payload are absolute.

## Authentication

Every endpoint except `/`, `/docs`, and `/openapi.json` requires a key in the
`X-API-Key` header. To keep the demo obviously-not-production, the keys are
just the two researcher names: `alice` and `bob`.

Jobs are scoped per key — `bob` cannot list, view, fetch, or cancel jobs
submitted with `alice`'s key (foreign job ids return `404`, not `403`, so the
existence of other researchers' jobs isn't leaked).

In production, set `API_KEYS_JSON` to a JSON object loaded from a secret
manager rather than using the demo fallback. See `PRODUCTIONIZE.md`.

## Try the demo from your terminal

```bash
# 1. Install and seed
git clone https://github.com/krMaynard/api-demo.git
cd api-demo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# seed.py reads ../krMaynard.github.io/data/vlop-dsa.json by default;
# override with --source if the JSON lives elsewhere
python seed.py
# python seed.py --source /path/to/vlop-dsa.json --db demo.db

# 2. Run the server in one terminal (leave it running)
uvicorn main:app --port 8000
```

### Automated walkthrough (recommended)

`demo.py` is a narrated script that walks through every major feature —
auth, table listing, query submission, polling, result fetching, job
isolation, read-only rejection, and cleanup — with coloured output showing
each request and response.

```bash
# In a second terminal (server must be running):
python demo.py           # auto-advance with a short pause between steps
python demo.py --pause   # press Enter to advance each step (live demo mode)
```

### Manual curl walkthrough

```bash
# 3. Set your key once so you don't have to repeat it
export KEY='alice'

# 4. Look around — list report tables and a table's queryable fields
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/tables
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/schema/t4_notices
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/fields?table=t4_notices"

# 5. Submit a structured query — note the 202 + job_id
curl -i -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{
        "table": "t4_notices",
        "query": {"and": [{"operation":"EQ","field_name":"category_code","field_values":["TOTAL"]}]},
        "group_by": ["service_name"],
        "aggregates": [{"function":"SUM","field_name":"notices","alias":"notices"}],
        "sort": [{"field_name":"notices","order":"desc"}],
        "max_count": 5
      }'

# Capture the id from the response, then:
export JOB='<paste-job_id-here>'

# 6. Poll until done
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB"

# 7. Fetch the result as JSON or CSV
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB/result?format=json"
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB/result?format=csv" -o result.csv
```

### One-liner (capture id, poll, fetch)

```bash
KEY='alice'
JOB=$(curl -s -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"category_code","field_values":["TOTAL"]}]},"group_by":["service_name"],"aggregates":[{"function":"SUM","field_name":"notices","alias":"notices"}],"sort":[{"field_name":"notices","order":"desc"}],"max_count":5}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
until [ "$(curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")" = "done" ]; do sleep 0.2; done
curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB/result?format=json"
```

### Things to try that demonstrate the design

```bash
# No key -> 401
curl -i http://127.0.0.1:8000/tables

# Arbitrary SQL is impossible — there's no `sql` field. An unknown field is
# rejected synchronously with 400 (the request never becomes a job):
curl -s -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"secrets","field_values":["x"]}]}}'
# -> 400 {"detail":"Unknown field 'secrets' for this table. ..."}

# A SQL-looking string in field_values is bound as data, not code: the job
# succeeds and simply matches nothing.
curl -s -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"service_name","field_values":["X%27%3B DROP TABLE services"]}]}}'
# (then GET /jobs/<id> -> {"status":"done","row_count":0})

# Bob cannot see Alice's job
curl -i -H 'X-API-Key: bob' "http://127.0.0.1:8000/jobs/$JOB"   # -> 404

# Or just open the Swagger UI in a browser:  http://127.0.0.1:8000/docs
# (click "Authorize" and paste a key)
```

## Endpoints

| Method | Path                                | Auth | Description                                    |
|--------|-------------------------------------|------|------------------------------------------------|
| GET    | `/`                                 | —    | Service info                                   |
| GET    | `/portal`                           | —    | Researcher portal (web UI)                     |
| POST   | `/portal/register`                  | —    | Issue a demo API key (`{name, email}`) — rate-limited, expiring |
| DELETE | `/portal/key`                       | key  | Revoke your own portal-issued key              |
| GET    | `/healthz`                          | —    | Liveness probe                                 |
| GET    | `/readyz`                           | —    | Readiness probe (checks DB connection)         |
| GET    | `/metrics`                          | —    | Prometheus metrics (scrape over internal net)  |
| GET    | `/tables`                           | key  | List the DSA report tables + dataset period    |
| GET    | `/fields?table=…`                   | key  | A table's queryable fields and operations      |
| GET    | `/schema/{table}`                   | key  | A report table's field registry                |
| POST   | `/query`                            | key  | Submit a structured query over a `table` (optional `callback_url`) — returns `202 + job_id` |
| GET    | `/jobs`                             | key  | List **your** jobs                             |
| GET    | `/jobs/{job_id}`                    | key  | Job status (your jobs only)                    |
| GET    | `/jobs/{job_id}/result?format=…`    | key  | Result rows (only when `status=done`)          |
| GET    | `/jobs/{job_id}/download?…`          | —    | Secure result download via a signed, expiring URL |
| DELETE | `/jobs/{job_id}`                    | key  | Cancel a running job, or remove a finished one |

## Job statuses

- `queued` — accepted, waiting for a worker
- `running` — a worker is executing the compiled query
- `done` — finished successfully; result available at `/jobs/{id}/result`
- `failed` — row-limit or runtime error; see `error` field
- `cancelled` — client called `DELETE /jobs/{id}` before completion

Invalid queries (unknown fields, illegal operations, bad aliases) are rejected
synchronously with `400` at `POST /query` and never become jobs.

`DELETE` while running calls SQLite's `interrupt()` to abort the in-flight
query, then drops the job from the registry.

## Secure download URLs

When a job reaches `status=done`, its job object includes a `download_urls`
map with a signed, expiring link for each format:

```jsonc
{
  "status": "done",
  "result_url": "/jobs/<id>/result",
  "download_urls": {
    "json": "/jobs/<id>/download?format=json&expires=1780767547&sig=ff9e1b…",
    "csv":  "/jobs/<id>/download?format=csv&expires=1780767547&sig=3f1e4e…"
  }
}
```

These are **capability URLs** (like an S3 presigned link): the `sig` is an
HMAC-SHA256 over the job id, format, and expiry, so the link authorises that
exact download and nothing else. `GET /jobs/{id}/download` therefore needs
**no `X-API-Key`** — possession of a valid, unexpired URL is sufficient — and
serves the result as a file attachment. You can hand the URL to a browser, a
`curl` without headers, or a download manager. The signature is checked before
any job lookup, so an invalid signature always returns `403` whether or not the
job id exists (no existence probing).

```bash
# Fetch the signed CSV link from the job status, then download with no key:
URL=$(curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['download_urls']['csv'])")
curl -OJ "http://127.0.0.1:8000$URL"     # writes <job_id>.csv
```

Tampering with the URL (changing the job id, format, or expiry) invalidates
the signature → `403`; an expired link → `410`. Links last
`DOWNLOAD_URL_TTL_SECONDS` (default 1 h). The `/result` endpoint (API-key
auth) remains available for clients that prefer header auth.

> **Production note:** set `DOWNLOAD_URL_SECRET` to a stable secret. The
> zero-config default is a random per-process key, so signed links would
> otherwise break on restart and wouldn't validate across multiple workers.

## Schema (EU DSA VLOP transparency reports)

Star schema — shared dimension tables plus one fact table per DSA report table
(H2 2025, 33 services). Dimensions: `services(id, name, platform)` (platform =
parent company), `categories(id, code, label)`, `sections`, `indicators`,
`scopes`, `surfaces`. Report tables (queried via `table`):

| `table` | DSA report | Key fields |
|---------|-----------|-----------|
| `t3_member_state_orders` | Member-State orders (Art. 9 & 10) | category, scope → `orders_to_act`, `items`, `orders_to_provide_info` |
| `t4_notices` | Notices (Art. 16) | category → `notices`, `tf_notices`, `actions_law`, `actions_tos`, … |
| `t5_own_initiative_illegal` | Own-initiative (illegal) | category → `measures`, `automated`, `vis_*`, `monetary_*`, `account_*` |
| `t6_own_initiative_tos` | Own-initiative (ToS) | as t5, plus `surface` |
| `t7_appeals_recidivism` | Appeals & recidivism | section, indicator, scope, surface → `value` |
| `t8_automated_means` | Automated means | section, indicator, scope, surface → `value` |
| `t9_human_resources` | Human resources | section, indicator, scope → `value` |
| `t10_amar` | Avg Monthly Active Recipients | scope → `value` |
| `t11_qualitative` | Qualitative descriptions | indicator → `qualitative_text` (free text, no measures) |

Every table also has the `service_name` and `platform` dimensions. Run
`GET /tables`, then `GET /schema/{table}` for a table's exact dimension/measure
fields and a runnable example.

## Sample queries

```jsonc
// Top 10 platforms by total Art. 16 notices received
{
  "table": "t4_notices",
  "query": {"and": [{"operation":"EQ","field_name":"category_code","field_values":["TOTAL"]}]},
  "group_by": ["service_name"],
  "aggregates": [{"function":"SUM","field_name":"notices","alias":"notices"}],
  "sort": [{"field_name":"notices","order":"desc"}],
  "max_count": 10
}

// Appeals upheld vs reversed for one platform
{
  "table": "t7_appeals_recidivism",
  "query": {"and": [{"operation":"EQ","field_name":"service_name","field_values":["YouTube"]}]},
  "group_by": ["indicator","scope"],
  "aggregates": [{"function":"SUM","field_name":"value","alias":"value"}]
}

// Platforms ranked by EU monthly active recipients (AMAR)
{
  "table": "t10_amar",
  "group_by": ["service_name","platform"],
  "aggregates": [{"function":"MAX","field_name":"value","alias":"amar"}],
  "sort": [{"field_name":"amar","order":"desc"}]
}

// Qualitative moderation descriptions for one platform (free text)
{
  "table": "t11_qualitative",
  "query": {"and": [{"operation":"EQ","field_name":"service_name","field_values":["TikTok"]}]},
  "fields": ["service_name","indicator","qualitative_text"]
}
```

## Rate limiting & logging

`POST /query` spawns background work, so it's throttled per API key — by default
60 submissions per 60 s (`QUERY_RATE_MAX_PER_WINDOW` / `QUERY_RATE_WINDOW_SECONDS`).
Over the limit returns `429` with a `Retry-After` header, before any job is
created. The counter shares the Redis-backed (or in-memory) store used for portal
registration limits, so it holds across workers when Redis is configured.

Logs are structured JSON by default (`LOG_FORMAT=json`; use `text` for
human-readable lines). Every request logs `method`, `path`, `status`,
`duration_ms`, and a `request_id` — also returned as the `X-Request-ID` response
header so clients can correlate. The job runner logs `job_submitted` /
`job_started` / `job_done` / `job_failed` with `job_id`, row count, and
`duration_ms`. API keys are never logged.

```json
{"ts": "2026-06-06T22:30:00+00:00", "level": "INFO", "event": "job_done", "job_id": "f68b…", "rows": 1, "duration_ms": 11.97}
```

`GET /metrics` exposes Prometheus metrics (no auth — scrape it over an internal
network). Request metrics are labelled by the **matched route template**, so job
ids never explode label cardinality:

- `api_demo_http_requests_total{method, path, status}`
- `api_demo_http_request_duration_seconds{method, path}` (histogram)
- `api_demo_jobs_in_flight` / `api_demo_job_queue_depth` (gauges)
- `api_demo_jobs_total{status}` (`done` / `failed`)

```
api_demo_http_requests_total{method="GET",path="/jobs/{job_id}",status="200"} 1.0
api_demo_jobs_total{status="done"} 1.0
```

## Configuration

All tuneable values are read from environment variables at startup:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `demo.db` beside `main.py` | Path to the SQLite database |
| `ROW_LIMIT` | `100000` | Max rows returned per query |
| `WORKER_THREADS` | `4` | Background worker thread count |
| `QUERY_TIMEOUT_SECONDS` | `300` | SQLite busy timeout |
| `REDIS_URL` | _(unset — uses memory)_ | Redis connection URL for persistent job storage |
| `JOB_TTL_SECONDS` | `86400` | How long to retain jobs in Redis (24 h) |
| `API_KEYS_JSON` | `alice` / `bob` demo keys | JSON object: `{"<key>": {"name": "<name>"}, …}` |
| `DOWNLOAD_URL_SECRET` | _(random per process)_ | HMAC secret for signing download URLs — set a stable value in production |
| `DOWNLOAD_URL_TTL_SECONDS` | `3600` | How long a signed download URL stays valid |
| `ISSUED_KEY_TTL_SECONDS` | `2592000` | Lifetime of a portal-issued API key (30 days) |
| `PORTAL_REGISTER_MAX_PER_WINDOW` | `10` | Max registrations per IP/email per window |
| `PORTAL_REGISTER_WINDOW_SECONDS` | `3600` | Registration rate-limit window |
| `TRUST_PROXY_HEADERS` | `0` | Trust `X-Forwarded-For` for the client IP (set only behind a trusted proxy) |
| `QUERY_RATE_MAX_PER_WINDOW` | `60` | Max `POST /query` submissions per API key per window |
| `QUERY_RATE_WINDOW_SECONDS` | `60` | Query rate-limit window |
| `LOG_LEVEL` | `INFO` | Log level for the `api_demo` logger |
| `LOG_FORMAT` | `json` | `json` for structured logs, `text` for human-readable |
| `PUBLIC_BASE_URL` | _(unset — relative links)_ | Base URL to make callback payload links absolute |
| `CALLBACK_TIMEOUT_SECONDS` | `10` | Per-attempt webhook delivery timeout |
| `CALLBACK_MAX_ATTEMPTS` | `3` | Webhook delivery attempts before giving up |
| `CALLBACK_WORKERS` | `8` | Size of the bounded webhook delivery thread pool |
| `CALLBACK_ALLOW_PRIVATE` | `0` | Allow callbacks to private/loopback hosts (dev only) |

Copy `.env.example` to `.env` and edit before running Docker Compose.

## Deploying

The service ships with a `Dockerfile` and `docker-compose.yml`. To run it
with Redis-backed job persistence:

```bash
# 1. Build the database (once)
python seed.py --db demo.db

# 2. Configure
cp .env.example .env   # edit API_KEYS_JSON at minimum

# 3. Start
docker-compose up --build

# Verify
curl http://localhost:8000/readyz
```

For HTTPS and a public domain, see `PRODUCTIONIZE.md` — Railway and Fly.io
are the fastest paths (automatic HTTPS, managed Redis, ~1 hour to live).

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest test_api.py -v
```

No running server or Redis needed — the test suite uses FastAPI's in-process
`TestClient` and a temporary SQLite database created in `conftest.py`.

The same `pyflakes` lint + `pytest` run on every pull request and push to `main`
via [GitHub Actions](.github/workflows/ci.yml) (Python 3.11 and 3.12).

## Regenerating the showcase GIFs

The GIFs in [`docs/gifs/`](docs/gifs/) are generated headlessly from `demo.py`
— no `ffmpeg`, `ttyd`, or screen recorder needed. `scripts/make_gifs.py` seeds
the DB if necessary, starts a temporary server, captures the demo's ANSI
output, replays it through a [`pyte`](https://github.com/selectel/pyte)
terminal emulator, and renders each step (plus a full walkthrough) to an
animated GIF with Pillow.

```bash
pip install -r requirements-dev.txt   # adds pyte + Pillow
make gifs                             # or: python scripts/make_gifs.py

python scripts/make_gifs.py --only 5 8   # just steps 5 and 8 (+ full)
python scripts/make_gifs.py --no-full    # per-step only
```

Per-step clips are detected from the demo's `── Step N:` headers, so they stay
in sync with the script automatically — add a step to `demo.py` and it gets its
own GIF on the next run.

The **portal** GIFs come from `scripts/make_portal_gifs.py`, which drives the
real `/portal` page in headless Chromium (Playwright) and assembles the frames
the same way:

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium   # one-time browser download
make portal-gifs                        # → docs/gifs/portal-*.gif
```

## Safety notes

- **No arbitrary SQL.** Clients send structured parameters, not SQL. Every
  field name, operation, aggregate function, and alias is validated against a
  fixed registry; the server compiles the request into one parameterised
  SELECT with all values bound. There is no path that runs caller-authored
  SQL, so classic SQL injection is structurally impossible.
- Invalid queries are rejected with `400` at submit time and never run.
- The DB is also opened with `mode=ro` as defence in depth — even a bug in the
  compiler couldn't write to it.
- Per-job results are capped at `ROW_LIMIT` rows (default 100k); over that the
  job fails and the client is asked to lower `max_count`.
- Download URLs are signed capabilities: the HMAC binds job id, format, and
  expiry, so a link can't be tampered with or repointed and stops working after
  `DOWNLOAD_URL_TTL_SECONDS`. The signature is verified before any store lookup,
  so invalid signatures get a uniform `403` and can't probe which job ids exist.
  Set `DOWNLOAD_URL_SECRET` in production.
- When `REDIS_URL` is set, jobs and results persist across restarts and are
  shared across multiple processes. Without it, everything lives in memory
  and a restart clears all jobs.
- See `PRODUCTIONIZE.md` for what's still needed before handling real traffic
  (rate limiting, structured logging, WAL mode, metrics, etc.).
