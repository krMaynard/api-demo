# research-api

[![CI](https://github.com/krMaynard/research-api/actions/workflows/ci.yml/badge.svg)](https://github.com/krMaynard/research-api/actions/workflows/ci.yml)

A FastAPI service that lets a researcher describe a query with **structured
parameters** (no SQL), runs it asynchronously on a worker thread, and serves the
results back as JSON or CSV. Backed by a read-only SQLite database seeded from the
aggregated **EU Digital Services Act (DSA) VLOP transparency reports** —
content-moderation statistics for 33 designated Very Large Online Platforms /
Search Engines (H2 2025), tables 3–11 of the DSA Implementing Regulation template
(`../krMaynard.github.io/data/vlop-dsa.json`).

A query names one of the 9 DSA **report tables** (`GET /api/tables`) and then
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

Open `http://127.0.0.1:8000/portal` after starting the server. Issued keys/sessions
are **persisted in Redis** when `REDIS_URL`/Upstash is configured (surviving restarts,
shared across workers), falling back to in-memory — same model as the job store.

### Sign in with Google (production)

Set `GOOGLE_CLIENT_ID` (an OAuth 2.0 **Web** client ID) and `ADMIN_EMAILS`, and the
service authenticates real Google accounts via **Google Identity Services** — which
runs over [**FedCM**](https://developers.google.com/identity/gsi/web/guides/fedcm-migration)
in supporting browsers, with non-FedCM fallback elsewhere:

- The frontend gets a Google **ID token** and POSTs it to `POST /api/auth/google`; the
  server verifies it (signature, `aud=GOOGLE_CLIENT_ID`, issuer, expiry, verified email).
- A new account becomes a **`pending`** registration (`202`); an **admin** (`ADMIN_EMAILS`,
  implicitly approved) approves it via `POST /api/admin/registrations/{email}/approve`.
- An approved login mints a first-party **session key** (`gs_…`, TTL
  `GOOGLE_SESSION_TTL_SECONDS`) used as `X-API-Key` like any other.
- Sessions are **revocable**: `POST /api/admin/registrations/{email}/revoke` (or
  `DELETE /api/portal/key` to sign yourself out) takes effect on the next request, because
  Google sessions are re-checked against the registration each call.

### Demo keys (local dev)

With `ALLOW_DEMO_KEYS=1` (default), the built-in `alice`/`bob` keys and the open,
no-auth `POST /api/portal/register` flow (rate-limited per IP/email, keys expiring after
`ISSUED_KEY_TTL_SECONDS`) remain available. Set `ALLOW_DEMO_KEYS=0` in production so
only Google sign-in works.

## No SQL — structured query parameters

Clients never send SQL. They describe what they want with structured
parameters modelled on the [TikTok Research API](https://developers.tiktok.com/doc/research-api-specs-query-videos/):
a boolean `query` of `and` / `or` / `not` clauses, where each clause is a
`{operation, field_name, field_values}` condition, plus optional `group_by`,
`aggregates`, `sort`, and `max_count`.

A query names a `table` (one of the 9 DSA report tables). The server validates
every field and operation against **that table's** fixed registry
(`GET /api/fields?table=…`) and compiles the request into a **single parameterised
SELECT** — values are always bound, never interpolated. Unknown fields, bad
operations, or injection attempts in values are rejected with `400` (or, for
values, bound harmlessly as data). There is no code path that executes
caller-authored SQL.

```jsonc
// POST /api/query — "top 5 platforms by Art. 16 notices received" (table t4_notices)
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

- **`table`** — which DSA report table to query (required). See `GET /api/tables`.
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

Call `GET /api/fields` for the full list of queryable dimensions, measures, and
operations.

### Ask in plain English (LLM → structured query)

`POST /api/ask` lets anyone ask a question in natural language ("Which platforms
received the most Article 16 notices?"). An LLM (**Claude**, `ANTHROPIC_MODEL`,
default `claude-opus-4-8`) translates it into the **structured `QueryRequest`** —
using JSON-schema structured outputs, so the model returns a constrained object,
**never SQL**. That object is then run through the *same* `compile_query` trust
boundary as every other query: the model only *proposes*, and the existing
validator disposes — an invalid field is a `422` (the response includes the
model's attempted query), and no model-authored SQL can ever reach the database.

It's **off unless `ANTHROPIC_API_KEY` is set** (LLM calls cost money), more tightly
IP-rate-limited than `/api/explore`, and the dashboard only shows the "Ask in plain
English" box when the server reports the feature enabled. The translation call is
isolated behind one function, so tests mock it and never hit the network.
Repeated questions are served from an in-process cache (`ASK_CACHE_SIZE`) so they
skip the LLM. The dashboard surfaces a plain-English restatement of the generated
query, suggested-question chips, a "Refine in builder" hand-off, and CSV/JSON
export of any result (explore + ask alike).

## Why an async job pattern?

A query against a large dataset can take seconds or minutes. If the API held
the HTTP connection open the whole time, slow queries would tie up workers,
time out at intermediate proxies, and stall the service.

So `POST /api/query` does **not** return rows. It validates the structured query,
returns `202 Accepted` plus a `job_id` immediately, runs the compiled query on
a background worker, and the client polls `/api/jobs/{job_id}` until it sees
`status="done"`. Then it fetches `/api/jobs/{job_id}/result?format=json|csv`.

```
client                          server
  │                               │
  │── POST /api/query ───────────────▶│   validate + compile, enqueue
  │◀── 202 + {job_id, status_url} │   (invalid query → 400, no job)
  │                               │   ┌─ worker thread
  │                               │   │   open ro conn
  │── GET /api/jobs/{id} ────────────▶│   │   execute parameterised SQL
  │◀── {status: running}          │   │   buffer rows
  │── GET /api/jobs/{id} ────────────▶│   │
  │◀── {status: done, result_url} │◀──┘
  │── GET /api/jobs/{id}/result ─────▶│
  │◀── rows (json or csv)         │
```

### Webhook callbacks (poll *or* get notified)

Polling is the default, but you can also include a `callback_url` in the
`POST /api/query` body. When the job reaches a terminal state the server POSTs the
job object (including signed `download_urls`) to that URL, so you don't have to
poll:

```jsonc
POST /api/query
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
git clone https://github.com/krMaynard/research-api.git
cd research-api
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
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/tables
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/schema/t4_notices
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/fields?table=t4_notices"

# 5. Submit a structured query — note the 202 + job_id
curl -i -X POST http://127.0.0.1:8000/api/query \
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
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB"

# 7. Fetch the result as JSON or CSV
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB/result?format=json"
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB/result?format=csv" -o result.csv
```

### One-liner (capture id, poll, fetch)

```bash
KEY='alice'
JOB=$(curl -s -X POST http://127.0.0.1:8000/api/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"category_code","field_values":["TOTAL"]}]},"group_by":["service_name"],"aggregates":[{"function":"SUM","field_name":"notices","alias":"notices"}],"sort":[{"field_name":"notices","order":"desc"}],"max_count":5}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
until [ "$(curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")" = "done" ]; do sleep 0.2; done
curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB/result?format=json"
```

### Things to try that demonstrate the design

```bash
# No key -> 401
curl -i http://127.0.0.1:8000/api/tables

# Arbitrary SQL is impossible — there's no `sql` field. An unknown field is
# rejected synchronously with 400 (the request never becomes a job):
curl -s -X POST http://127.0.0.1:8000/api/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"secrets","field_values":["x"]}]}}'
# -> 400 {"detail":"Unknown field 'secrets' for this table. ..."}

# A SQL-looking string in field_values is bound as data, not code: the job
# succeeds and simply matches nothing.
curl -s -X POST http://127.0.0.1:8000/api/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table":"t4_notices","query":{"and":[{"operation":"EQ","field_name":"service_name","field_values":["X%27%3B DROP TABLE services"]}]}}'
# (then GET /api/jobs/<id> -> {"status":"done","row_count":0})

# Bob cannot see Alice's job
curl -i -H 'X-API-Key: bob' "http://127.0.0.1:8000/api/jobs/$JOB"   # -> 404

# Or just open the Swagger UI in a browser:  http://127.0.0.1:8000/docs
# (click "Authorize" and paste a key)
```

## Endpoints

The dashboard is served at `/` and the JSON API lives under `/api/*` on the same
origin (no CORS). Operational endpoints and pages stay at the root.

| Method | Path                                | Auth | Description                                    |
|--------|-------------------------------------|------|------------------------------------------------|
| GET    | `/`                                 | —    | Public VLOP transparency **dashboard** (web UI) |
| GET    | `/api/overview`                     | —    | Public headline aggregates powering the dashboard |
| GET    | `/api/explore/options`              | —    | Public: tables + their dimensions/measures (query-builder metadata) |
| POST   | `/api/explore`                      | —    | Public: run a bounded structured query **inline** (row-capped, IP-rate-limited) |
| POST   | `/api/ask`                          | —    | Public: ask in natural language — an LLM writes the structured query (if `ANTHROPIC_API_KEY` set) |
| GET    | `/api`                              | —    | API service info                               |
| GET    | `/portal`                           | —    | Researcher portal (web UI)                      |
| POST   | `/api/auth/google`                  | —    | Verify a Google ID token → session key, or `202` pending approval |
| POST   | `/api/portal/register`              | —    | Demo: issue a key without auth (disabled when `ALLOW_DEMO_KEYS=0`) |
| DELETE | `/api/portal/key`                   | key  | Revoke your own session / portal-issued key    |
| GET    | `/api/admin/registrations`          | admin| List researcher registrations (`?status=`)     |
| POST   | `/api/admin/registrations/{email}/approve` | admin | Approve an account                  |
| POST   | `/api/admin/registrations/{email}/revoke`  | admin | Revoke an account (kills live sessions) |
| GET    | `/healthz`                          | —    | Liveness probe                                 |
| GET    | `/readyz`                           | —    | Readiness probe (checks DB connection)         |
| GET    | `/version`                          | —    | Deployed build (commit SHA); also `X-Version` header |
| GET    | `/metrics`                          | —    | Prometheus metrics (scrape over internal net)  |
| GET    | `/api/tables`                       | key  | List the DSA report tables + dataset period    |
| GET    | `/api/fields?table=…`               | key  | A table's queryable fields and operations      |
| GET    | `/api/schema/{table}`               | key  | A report table's field registry                |
| POST   | `/api/query`                        | key  | Submit a structured query over a `table` (optional `callback_url`) — returns `202 + job_id` |
| GET    | `/api/jobs`                         | key  | List **your** jobs                             |
| GET    | `/api/jobs/{job_id}`                | key  | Job status (your jobs only)                    |
| GET    | `/api/jobs/{job_id}/result?format=…`| key  | Result rows (only when `status=done`)          |
| GET    | `/api/jobs/{job_id}/download?…`      | —    | Secure result download via a signed, expiring URL |
| DELETE | `/api/jobs/{job_id}`                | key  | Cancel a running job, or remove a finished one |

## Job statuses

- `queued` — accepted, waiting for a worker
- `running` — a worker is executing the compiled query
- `done` — finished successfully; result available at `/api/jobs/{id}/result`
- `failed` — row-limit or runtime error; see `error` field
- `cancelled` — client called `DELETE /api/jobs/{id}` before completion

Invalid queries (unknown fields, illegal operations, bad aliases) are rejected
synchronously with `400` at `POST /api/query` and never become jobs.

`DELETE` while running calls SQLite's `interrupt()` to abort the in-flight
query, then drops the job from the registry.

## Secure download URLs

When a job reaches `status=done`, its job object includes a `download_urls`
map with a signed, expiring link for each format:

```jsonc
{
  "status": "done",
  "result_url": "/api/jobs/<id>/result",
  "download_urls": {
    "json": "/api/jobs/<id>/download?format=json&expires=1780767547&sig=ff9e1b…",
    "csv":  "/api/jobs/<id>/download?format=csv&expires=1780767547&sig=3f1e4e…"
  }
}
```

These are **capability URLs** (like an S3 presigned link): the `sig` is an
HMAC-SHA256 over the job id, format, and expiry, so the link authorises that
exact download and nothing else. `GET /api/jobs/{id}/download` therefore needs
**no `X-API-Key`** — possession of a valid, unexpired URL is sufficient — and
serves the result as a file attachment. You can hand the URL to a browser, a
`curl` without headers, or a download manager. The signature is checked before
any job lookup, so an invalid signature always returns `403` whether or not the
job id exists (no existence probing).

```bash
# Fetch the signed CSV link from the job status, then download with no key:
URL=$(curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/api/jobs/$JOB" \
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
`GET /api/tables`, then `GET /api/schema/{table}` for a table's exact dimension/measure
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

`POST /api/query` spawns background work, so it's throttled per API key — by default
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

- `research_api_http_requests_total{method, path, status}`
- `research_api_http_request_duration_seconds{method, path}` (histogram)
- `research_api_jobs_in_flight` / `research_api_job_queue_depth` (gauges)
- `research_api_jobs_total{status}` (`done` / `failed`)

```
research_api_http_requests_total{method="GET",path="/api/jobs/{job_id}",status="200"} 1.0
research_api_jobs_total{status="done"} 1.0
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
| `GOOGLE_CLIENT_ID` | _(unset — sign-in disabled)_ | OAuth 2.0 Web client ID; the `aud` Google ID tokens are verified against |
| `ADMIN_EMAILS` | _(empty)_ | Comma-separated admin allowlist — implicitly approved, can approve/revoke others |
| `GOOGLE_SESSION_TTL_SECONDS` | `604800` | Lifetime of a first-party session minted after Google sign-in (7 days) |
| `ALLOW_DEMO_KEYS` | `1` | Demo `alice`/`bob` keys + open `/api/portal/register`; set `0` in production |
| `ALLOWED_ORIGINS` | _(empty — same-origin only)_ | Comma-separated browser origins allowed for cross-origin API calls (CORS) |
| `DOWNLOAD_URL_SECRET` | _(random per process)_ | HMAC secret for signing download URLs — set a stable value in production |
| `DOWNLOAD_URL_TTL_SECONDS` | `3600` | How long a signed download URL stays valid |
| `ISSUED_KEY_TTL_SECONDS` | `2592000` | Lifetime of a portal-issued API key (30 days) |
| `PORTAL_REGISTER_MAX_PER_WINDOW` | `10` | Max registrations per IP/email per window |
| `PORTAL_REGISTER_WINDOW_SECONDS` | `3600` | Registration rate-limit window |
| `TRUST_PROXY_HEADERS` | `0` | Trust `X-Forwarded-For` for the client IP (set only behind a trusted proxy) |
| `QUERY_RATE_MAX_PER_WINDOW` | `60` | Max `POST /api/query` submissions per API key per window |
| `QUERY_RATE_WINDOW_SECONDS` | `60` | Query rate-limit window |
| `EXPLORE_MAX_ROWS` | `500` | Hard row cap for the public `POST /api/explore` endpoint |
| `EXPLORE_RATE_MAX_PER_WINDOW` | `60` | Max public `/api/explore` queries per IP per window |
| `EXPLORE_RATE_WINDOW_SECONDS` | `60` | Public explore rate-limit window |
| `ANTHROPIC_API_KEY` | _(unset — `/api/ask` disabled)_ | Set to enable natural-language queries (Claude); read by the SDK |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Model used to translate questions into structured queries |
| `ASK_RATE_MAX_PER_WINDOW` | `10` | Max public `/api/ask` (LLM) calls per IP per window |
| `ASK_RATE_WINDOW_SECONDS` | `60` | Public ask rate-limit window |
| `ASK_CACHE_SIZE` | `256` | In-process cache of question→query, so a repeat question skips the LLM call |
| `LOG_LEVEL` | `INFO` | Log level for the `research_api` logger |
| `LOG_FORMAT` | `json` | `json` for structured logs, `text` for human-readable |
| `PUBLIC_BASE_URL` | _(unset — relative links)_ | Base URL to make callback payload links absolute |
| `CALLBACK_TIMEOUT_SECONDS` | `10` | Per-attempt webhook delivery timeout |
| `CALLBACK_MAX_ATTEMPTS` | `3` | Webhook delivery attempts before giving up |
| `CALLBACK_WORKERS` | `8` | Size of the bounded webhook delivery thread pool |
| `CALLBACK_ALLOW_PRIVATE` | `0` | Allow callbacks to private/loopback hosts (dev only) |

Copy `.env.example` to `.env` and edit before running Docker Compose.

## Deploying

The image is **self-contained**: `demo.db` is seeded at build time from the
vendored dataset snapshot (`data/vlop-dsa.json`), so the running container needs
no external data source. Refresh the snapshot with `scripts/refresh-dataset.sh`
when the upstream dataset changes, then rebuild.

### Docker Compose (with Redis)

```bash
cp .env.example .env        # edit secrets (DOWNLOAD_URL_SECRET at minimum)
docker-compose up --build   # web on :8000 (container $PORT 8080) + Redis

curl http://localhost:8000/readyz
```

### Deploy to Cloud Run

Production config: Google sign-in only (`ALLOW_DEMO_KEYS=0`), a stable
`DOWNLOAD_URL_SECRET`, and Redis for cross-instance job/session/registration
state. A ready-to-edit `service.yaml` (Knative manifest with startup/liveness
probes) is included.

```bash
PROJECT_ID=your-project; REGION=us-central1

# 1. Build & push the image (Cloud Build → Artifact Registry)
gcloud artifacts repositories create research-api --repository-format=docker --location="$REGION" 2>/dev/null || true
gcloud builds submit --tag "$REGION-docker.pkg.dev/$PROJECT_ID/research-api/research-api:latest"

# 2. Create the secrets it references
printf '%s' "$(openssl rand -hex 32)" | gcloud secrets create research-api-download-secret --data-file=- 2>/dev/null || true
printf '%s' "redis://default:PASSWORD@HOST:6379" | gcloud secrets create research-api-redis-url --data-file=- 2>/dev/null || true

# 3. Edit service.yaml — image, GOOGLE_CLIENT_ID, ADMIN_EMAILS, PUBLIC_BASE_URL — then deploy
gcloud run services replace service.yaml --region "$REGION"
gcloud run services add-iam-policy-binding research-api --region "$REGION" \
  --member=allUsers --role=roles/run.invoker        # public; omit for IAM-gated
```

Then create an **OAuth 2.0 Web client ID** in Google Cloud and add the Cloud Run
URL (and any custom domain) to its *Authorized JavaScript origins* — that's the
value of `GOOGLE_CLIENT_ID`. Sign in at `/portal`; the first `ADMIN_EMAILS`
account is auto-approved and can approve others.

### Continuous deployment (GitHub Actions)

`.github/workflows/deploy.yml` builds the image and rolls a new Cloud Run
revision on every push to `main`, using **Workload Identity Federation** (no
service-account keys). It **skips automatically** until you configure it, so it
never red-Xes an un-provisioned repo. One-time setup:

```bash
PROJECT_ID=your-project; REGION=us-central1; REPO=your-user/research-api
SA=cloud-run-deployer@$PROJECT_ID.iam.gserviceaccount.com

# 1. Deployer service account + roles (build, push, deploy, act-as runtime SA)
gcloud iam service-accounts create cloud-run-deployer --project "$PROJECT_ID"
for role in run.admin cloudbuild.builds.editor artifactregistry.writer \
            iam.serviceAccountUser storage.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" --role="roles/$role"; done

# 2. Workload Identity pool + GitHub provider, scoped to this repo
gcloud iam workload-identity-pools create github --location=global --project "$PROJECT_ID"
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'" \
  --project "$PROJECT_ID"
POOL=$(gcloud iam workload-identity-pools describe github --location=global --project "$PROJECT_ID" --format='value(name)')
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project "$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL}/attribute.repository/${REPO}"
```

Then add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
|----------|-------|
| `GCP_PROJECT_ID` | your project id (presence of this enables the workflow) |
| `GCP_REGION` | e.g. `us-central1` (default if unset) |
| `CLOUD_RUN_SERVICE` | e.g. `research-api` (default if unset) |
| `ARTIFACT_REPO` | Artifact Registry repo name (default `research-api`) |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/NUM/locations/global/workloadIdentityPools/github/providers/github` |
| `GCP_SERVICE_ACCOUNT` | the deployer SA email above |
| `DEPLOY` | optional — set to `false` to build & push only (no deploy) |

Do the **first** deploy with `service.yaml` (it sets env/secrets/scaling); the
Action thereafter just ships new image revisions, preserving that config.

Each run deploys the new revision with **no traffic**, **smoke-tests** its
`/readyz` on the revision's own URL, and only then **routes traffic** to it — so
a revision that fails to come up never receives requests. It also stamps the
commit SHA as `APP_VERSION`, surfaced at `GET /version` and the `X-Version`
header (the smoke test assumes a public service; for an IAM-gated one, add an
identity-token header).

### Custom domain

Map a domain to the service (or use Cloud Run's built-in domain mapping / a
load balancer):

```bash
gcloud beta run domain-mappings create --service research-api \
  --domain api.example.com --region "$REGION" --project "$PROJECT_ID"
# Then add the shown DNS records at your registrar.
```

After it resolves, also: add the domain to the OAuth client's *Authorized
JavaScript origins*, and set `PUBLIC_BASE_URL=https://api.example.com` (so
callback/download links are absolute). Note that the domain must be a real,
registrable public TLD — pick a delegated TLD (e.g. `.org`, `.eu`, `.dev`) or a
subdomain of one; non-delegated names like `.data` won't resolve publicly.

For other targets (Railway, Fly.io) and deeper hardening, see `PRODUCTIONIZE.md`.

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
- **Browser hardening.** Every response carries a set of hardening headers —
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (signed
  download URLs carry their HMAC in the query string, so the full URL is never
  leaked via `Referer`), `X-Frame-Options: DENY`, `Permissions-Policy`
  (geolocation/camera/mic/payment off), and `Strict-Transport-Security` (HSTS).
  The two HTML pages (`/`, `/portal`) send a **Content-Security-Policy** that
  locks `script-src` to `'self'` plus each page's own inline `<script>` (allowlisted
  by sha256 hash — computed from the file, so never stale). Chart.js is **vendored
  same-origin** (`static/vendor/chart.umd.js`), so the dashboard needs no
  third-party script origin at all; the portal allows only `accounts.google.com`
  for Google sign-in. No `'unsafe-inline'` for scripts; `frame-ancestors 'none'`
  blocks clickjacking. DB values are HTML-escaped before render. If Chart.js fails
  to load, the dashboard degrades to data tables rather than blank panels.
- When `REDIS_URL` is set, jobs and results persist across restarts and are
  shared across multiple processes. Without it, everything lives in memory
  and a restart clears all jobs.
- See `PRODUCTIONIZE.md` for what's still needed before handling real traffic
  (rate limiting, structured logging, WAL mode, metrics, etc.).
