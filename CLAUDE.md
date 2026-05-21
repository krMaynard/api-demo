# api-demo — Claude context

## What this is

A FastAPI demo service that accepts SQL queries via HTTP, runs them
asynchronously on background worker threads, and returns results as JSON or
CSV. Backed by a read-only SQLite database seeded from the Google Government
Content Removals dataset.

Built to demonstrate the **async-job / poll pattern**: `POST /query` returns
`202 + job_id` immediately; the client polls `/jobs/{id}` until `status=done`,
then fetches `/jobs/{id}/result`.

## Repo layout

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app — all endpoints, job runner, in-memory job registry |
| `seed.py` | Build `demo.db` from the source JSON in `../krMaynard.github.io/data/` |
| `demo.py` | Narrated walkthrough script (run after starting the server) |
| `requirements.txt` | `fastapi` + `uvicorn[standard]` |
| `demo.db` | SQLite DB (git-ignored, produced by `seed.py`) |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# seed.py reads from the sibling repo — clone both into the same parent dir
python seed.py          # creates demo.db

uvicorn main:app --port 8000
```

Repos are expected as siblings:
```
parent/
  api-demo/            ← this repo
  krMaynard.github.io/ ← source data lives at data/google-government-removals.json
```

## Running the demo

```bash
python demo.py           # auto-advance
python demo.py --pause   # press Enter between steps (live demo mode)
```

## Auth

Demo API keys are hard-coded in `main.py` as `alice` and `bob`.
Pass via `X-API-Key` header. Jobs are scoped per key — each user only sees
their own jobs (foreign IDs return 404, not 403).

In production these would come from a secret store.

## Database schema

Star schema seeded from `google-government-removals.json`:

- **`removals`** — fact table (period × country × requestor × product × reason + counts)
- **`periods`** — "January - June 2024" labels
- **`countries`** — ISO code + display name
- **`requestors`** — Court Order, Police, Government Officials, …
- **`products`** — YouTube, Web Search, Maps, …
- **`reasons`** — Defamation, National Security, Privacy, …

The DB is opened `mode=ro` — any write attempt surfaces as `status=failed`.

## Key design decisions

- **202 + polling** instead of blocking HTTP: lets long queries run without
  tying up connections or timing out at proxies.
- **In-memory job registry** (`_jobs` dict + `threading.Lock`): simple for a
  demo; restart clears all jobs. Production would need persistent storage.
- **`sqlite3.interrupt()`** on `DELETE /jobs/{id}` while running: aborts the
  in-flight query without parsing SQL.
- **100k row cap**: queries returning more rows fail with a helpful error
  asking the caller to add a `LIMIT`.
- **Swagger UI** at `/docs` works out of the box — click Authorize and paste
  a key.

## Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/` | — | Service info |
| GET | `/tables` | key | List tables |
| GET | `/schema/{table}` | key | Column info |
| POST | `/query` | key | Submit SQL → 202 + job_id |
| GET | `/jobs` | key | List your jobs |
| GET | `/jobs/{id}` | key | Job status |
| GET | `/jobs/{id}/result?format=json\|csv` | key | Result (status=done only) |
| DELETE | `/jobs/{id}` | key | Cancel or remove |
