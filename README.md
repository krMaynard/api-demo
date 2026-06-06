# api-demo

A small FastAPI service that lets a researcher describe a query with
**structured parameters** (no SQL), runs it asynchronously on a worker thread,
and serves the results back as JSON or CSV. Backed by a read-only SQLite
database seeded from the **Google Government Content Removals** dataset
(`../krMaynard.github.io/data/google-government-removals.json`).

## No SQL — structured query parameters

Clients never send SQL. They describe what they want with structured
parameters modelled on the [TikTok Research API](https://developers.tiktok.com/doc/research-api-specs-query-videos/):
a boolean `query` of `and` / `or` / `not` clauses, where each clause is a
`{operation, field_name, field_values}` condition, plus optional `group_by`,
`aggregates`, `sort`, and `max_count`.

The server validates every field and operation against a fixed registry
(`GET /fields`) and compiles the request into a **single parameterised
SELECT** — values are always bound, never interpolated. Unknown fields, bad
operations, or injection attempts in values are rejected with `400` (or, for
values, bound harmlessly as data). There is no code path that executes
caller-authored SQL.

```jsonc
// POST /query — "top 5 EU countries by items requested for removal"
{
  "query": {
    "and": [
      { "operation": "IN", "field_name": "country_code",
        "field_values": ["DE", "FR", "IT", "ES", "NL"] }
    ]
  },
  "group_by": ["country_name"],
  "aggregates": [
    { "function": "SUM", "field_name": "items_requested", "alias": "items" }
  ],
  "sort": [{ "field_name": "items", "order": "desc" }],
  "max_count": 5
}
```

### Query language

- **`query`** — `{ "and": [...], "or": [...], "not": [...] }`. Each list holds
  conditions; `and` are ANDed, `or` are ORed together, `not` are negated, and
  the three groups are combined with AND. All optional.
- **Condition** — `{ "operation", "field_name", "field_values" }`.
  - Operations: `EQ`, `IN` (all fields); `GT`, `GTE`, `LT`, `LTE` (numeric
    measures only). `field_values` is always a list.
- **`fields`** — columns to return for a raw (non-aggregated) query. Defaults
  to every field. Cannot be combined with `group_by`/`aggregates`.
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

# seed.py reads from the sibling krMaynard.github.io repo by default;
# override with --source if the JSON lives elsewhere
python seed.py
# python seed.py --source /path/to/google-government-removals.json --db demo.db

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

# 4. Look around — list tables, columns, and queryable fields
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/tables
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/schema/removals
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/fields

# 5. Submit a structured query — note the 202 + job_id
curl -i -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{
        "group_by": ["country_name"],
        "aggregates": [{"function":"SUM","field_name":"items_requested","alias":"items"}],
        "sort": [{"field_name":"items","order":"desc"}],
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
  -d '{"group_by":["country_name"],"aggregates":[{"function":"SUM","field_name":"items_requested","alias":"items"}],"sort":[{"field_name":"items","order":"desc"}],"max_count":5}' \
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
  -d '{"query":{"and":[{"operation":"EQ","field_name":"secrets","field_values":["x"]}]}}'
# -> 400 {"detail":"Unknown field 'secrets'. See GET /fields."}

# A SQL-looking string in field_values is bound as data, not code: the job
# succeeds and simply matches nothing.
curl -s -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query":{"and":[{"operation":"EQ","field_name":"country_code","field_values":["US%27%3B DROP TABLE countries"]}]}}'
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
| GET    | `/healthz`                          | —    | Liveness probe                                 |
| GET    | `/readyz`                           | —    | Readiness probe (checks DB connection)         |
| GET    | `/fields`                           | key  | List queryable fields and operations           |
| GET    | `/tables`                           | key  | List tables                                    |
| GET    | `/schema/{table}`                   | key  | Show a table's columns                         |
| POST   | `/query`                            | key  | Submit a structured query — returns `202 + job_id` |
| GET    | `/jobs`                             | key  | List **your** jobs                             |
| GET    | `/jobs/{job_id}`                    | key  | Job status (your jobs only)                    |
| GET    | `/jobs/{job_id}/result?format=…`    | key  | Result rows (only when `status=done`)          |
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

## Schema (Google Government Content Removals)

Star schema — one fact table plus five small dimension tables:

- `removals(period_id, country_id, requestor_id, product_id, reason_id, num_requests, items_requested, removed_legal, removed_policy, not_found, not_enough_info, no_action, already_removed)`
- `periods(id, label)` — e.g. "January - June 2024"
- `countries(id, code, name)` — ISO code + display name
- `requestors(id, name)` — Court Order, Police, Government Officials, …
- `products(id, name)` — YouTube, Web Search, Maps, …
- `reasons(id, name)` — Defamation, National security, Privacy, …

### Queryable fields

- **Dimensions** (text; `EQ`/`IN`; usable in `query`, `fields`, `group_by`,
  `sort`): `period_label`, `country_code`, `country_name`, `requestor_name`,
  `product_name`, `reason_name`.
- **Measures** (numeric; `EQ`/`IN`/`GT`/`GTE`/`LT`/`LTE`; usable in `query`,
  `fields`, `aggregates`): `num_requests`, `items_requested`, `removed_legal`,
  `removed_policy`, `not_found`, `not_enough_info`, `no_action`,
  `already_removed`.

## Sample queries

```jsonc
// Top 10 countries by items requested for removal
{
  "group_by": ["country_name"],
  "aggregates": [{"function":"SUM","field_name":"items_requested","alias":"items"}],
  "sort": [{"field_name":"items","order":"desc"}],
  "max_count": 10
}

// Defamation requests by product
{
  "query": {"and": [{"operation":"EQ","field_name":"reason_name","field_values":["Defamation"]}]},
  "group_by": ["product_name"],
  "aggregates": [{"function":"SUM","field_name":"num_requests","alias":"requests"}],
  "sort": [{"field_name":"requests","order":"desc"}]
}

// Trend of EU items requested over time
{
  "query": {"and": [{"operation":"IN","field_name":"country_code",
    "field_values":["DE","FR","IT","ES","PL","NL","BE","SE","AT","IE"]}]},
  "group_by": ["period_label"],
  "aggregates": [{"function":"SUM","field_name":"items_requested","alias":"items"}],
  "sort": [{"field_name":"period_label","order":"asc"}]
}
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
- When `REDIS_URL` is set, jobs and results persist across restarts and are
  shared across multiple processes. Without it, everything lives in memory
  and a restart clears all jobs.
- See `PRODUCTIONIZE.md` for what's still needed before handling real traffic
  (rate limiting, structured logging, WAL mode, metrics, etc.).
