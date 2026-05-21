# api-demo

A small FastAPI service that lets a researcher submit a SQL query, runs it
asynchronously on a worker thread, and serves the results back as JSON or CSV.
Backed by a read-only SQLite database seeded from the **Google Government
Content Removals** dataset (`../krMaynard.github.io/data/google-government-removals.json`).

## Why an async job pattern?

A SQL query against a large dataset can take seconds or minutes. If the API
held the HTTP connection open the whole time, slow queries would tie up
workers, time out at intermediate proxies, and stall the service.

So `POST /query` does **not** return rows. It returns `202 Accepted` plus a
`job_id` immediately, runs the query on a background worker, and the client
polls `/jobs/{job_id}` until it sees `status="done"`. Then it fetches
`/jobs/{job_id}/result?format=json|csv`.

```
client                          server
  │                               │
  │── POST /query ───────────────▶│   enqueue
  │◀── 202 + {job_id, status_url} │
  │                               │   ┌─ worker thread
  │                               │   │   open ro conn
  │── GET /jobs/{id} ────────────▶│   │   execute SQL
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

In a real deployment these keys would come from a secret store, not be
hard-coded in `main.py`.

## Try the demo from your terminal

```bash
# 1. Install and seed
git clone https://github.com/krMaynard/api-demo.git
cd api-demo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed.py

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

# 4. Look around
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/tables
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/schema/removals

# 5. Submit a query — note the 202 + job_id
curl -i -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT c.name, SUM(r.items_requested) AS items FROM removals r JOIN countries c ON c.id = r.country_id GROUP BY c.name ORDER BY items DESC LIMIT 5"}'

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
  -d '{"sql":"SELECT c.name, SUM(r.items_requested) AS items FROM removals r JOIN countries c ON c.id=r.country_id GROUP BY c.name ORDER BY items DESC LIMIT 5"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
until [ "$(curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")" = "done" ]; do sleep 0.2; done
curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8000/jobs/$JOB/result?format=json"
```

### Things to try that demonstrate the design

```bash
# No key -> 401
curl -i http://127.0.0.1:8000/tables

# Write attempt -> job lands in status=failed (read-only DB rejects it)
curl -s -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"sql":"DELETE FROM countries"}'
# (then GET /jobs/<id> -> {"status":"failed","error":"SQL error: attempt to write a readonly database"})

# Bob cannot see Alice's job
curl -i -H 'X-API-Key: bob' "http://127.0.0.1:8000/jobs/$JOB"   # -> 404

# Or just open the Swagger UI in a browser:  http://127.0.0.1:8000/docs
# (click "Authorize" and paste a key)
```

## Endpoints

| Method | Path                                | Auth | Description                                    |
|--------|-------------------------------------|------|------------------------------------------------|
| GET    | `/`                                 | —    | Service info                                   |
| GET    | `/tables`                           | key  | List tables                                    |
| GET    | `/schema/{table}`                   | key  | Show a table's columns                         |
| POST   | `/query`                            | key  | Submit a SQL query — returns `202 + job_id`    |
| GET    | `/jobs`                             | key  | List **your** jobs                             |
| GET    | `/jobs/{job_id}`                    | key  | Job status (your jobs only)                    |
| GET    | `/jobs/{job_id}/result?format=…`    | key  | Result rows (only when `status=done`)          |
| DELETE | `/jobs/{job_id}`                    | key  | Cancel a running job, or remove a finished one |

## Job statuses

- `queued` — accepted, waiting for a worker
- `running` — a worker is executing the SQL
- `done` — finished successfully; result available at `/jobs/{id}/result`
- `failed` — SQL or row-limit error; see `error` field
- `cancelled` — client called `DELETE /jobs/{id}` before completion

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

## Sample queries

```sql
-- Top 10 countries by items requested for removal
SELECT c.name, SUM(r.items_requested) AS items
FROM removals r JOIN countries c ON c.id = r.country_id
GROUP BY c.name ORDER BY items DESC LIMIT 10;

-- Defamation requests by product
SELECT p.name AS product, SUM(r.num_requests) AS requests
FROM removals r
JOIN products p ON p.id = r.product_id
JOIN reasons rn ON rn.id = r.reason_id
WHERE rn.name = 'Defamation'
GROUP BY p.name ORDER BY requests DESC;

-- Trend of EU items requested over time
SELECT pr.label, SUM(r.items_requested) AS items
FROM removals r
JOIN periods pr ON pr.id = r.period_id
JOIN countries c ON c.id = r.country_id
WHERE c.code IN ('DE','FR','IT','ES','PL','NL','BE','SE','AT','IE')
GROUP BY pr.label ORDER BY pr.id;
```

## Safety notes

- The DB is opened with `mode=ro`, so any `INSERT`/`UPDATE`/`DELETE`/`DROP`
  surfaces as `status=failed` with a `readonly database` error — no SQL
  parsing required.
- Per-job results are capped at 100,000 rows; over that the job fails and the
  client is asked to add a `LIMIT`.
- Jobs and their result rows live in process memory. Restart = clear.
- This is a **demo**. Production would need: persistent job storage, real
  auth (OAuth/OIDC + rotating keys from a secret store), per-user quotas,
  query allow-listing or row-level filtering, and result pagination/streaming
  for large outputs.
