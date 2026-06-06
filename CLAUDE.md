# api-demo — Claude context

## What this is

A FastAPI demo service that accepts **structured query parameters** (not SQL)
via HTTP, runs the resulting query asynchronously on background worker threads,
and returns results as JSON or CSV. Backed by a read-only SQLite database
seeded from the Google Government Content Removals dataset.

Built to demonstrate two things:

1. The **async-job / poll pattern**: `POST /query` returns `202 + job_id`
   immediately; the client polls `/jobs/{id}` until `status=done`, then
   fetches `/jobs/{id}/result`.
2. A **safe, no-SQL query interface** modelled on the TikTok Research API:
   boolean `and`/`or`/`not` clauses of `{operation, field_name, field_values}`,
   plus `group_by`, `aggregates`, `sort`, and `max_count`. The server
   validates everything against a fixed field registry and compiles it into a
   single parameterised SELECT (`compile_query` in `main.py`). Arbitrary SQL is
   never accepted or executed.

## Repo layout

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app — all endpoints, job runner, in-memory job registry |
| `seed.py` | Build `demo.db` from the source JSON in `../krMaynard.github.io/data/` |
| `demo.py` | Narrated walkthrough script (run after starting the server) |
| `static/portal.html` | Researcher portal single-page app (served at `/portal`) |
| `scripts/_demo_server.py` | Shared helper: seed DB + run a temp server (used by the GIF generators) |
| `scripts/make_gifs.py` | Headless terminal-demo GIF generator (pyte + Pillow) → `docs/gifs/` |
| `scripts/make_portal_gifs.py` | Portal-workflow GIF generator (Playwright + Pillow) → `docs/gifs/portal-*.gif` |
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

The DB is opened `mode=ro` as defence in depth.

## Query model

Requests are structured (see `QueryRequest`/`compile_query` in `main.py`):

- **Dimensions** (text, `EQ`/`IN`): `period_label`, `country_code`,
  `country_name`, `requestor_name`, `product_name`, `reason_name`.
- **Measures** (numeric, `EQ`/`IN`/`GT`/`GTE`/`LT`/`LTE`): `num_requests`,
  `items_requested`, `removed_legal`, `removed_policy`, `not_found`,
  `not_enough_info`, `no_action`, `already_removed`.
- **Aggregates**: `SUM`/`COUNT`/`AVG`/`MIN`/`MAX` over a measure, with an alias.
- `group_by`, `sort`, `max_count`. `GET /fields` documents all of this.

`compile_query` is the single trust boundary — keep all field/operation
validation there, and never build SQL by interpolating user values (always
bind with `?`).

## Key design decisions

- **Structured params, not SQL**: the only way to query is the validated
  parameter model, compiled to one parameterised SELECT — no caller SQL runs.
- **Researcher portal** (`/portal` + `POST /portal/register`): a demo onboarding
  UI. Registration mints a key into the **issued-key store** (`_key_store`:
  Redis-backed when configured, else in-memory — shares `_redis` with the job
  store), with an expiry (`ISSUED_KEY_TTL`) and per-IP/email rate limiting
  (`_key_store.incr`). `require_api_key` accepts configured keys *or* issued ones
  (`_lookup_principal`); `DELETE /portal/key` self-revokes. Still no real auth —
  production would front it with SSO.
- **202 + polling** instead of blocking HTTP: lets long queries run without
  tying up connections or timing out at proxies.
- **Signed download URLs**: a done job exposes `download_urls` (json/csv) —
  capability links carrying an HMAC-SHA256 of `job_id:format:expires`.
  `GET /jobs/{id}/download` verifies the signature (before any store lookup, so
  job existence isn't leaked) instead of an API key, so the URL alone authorises
  the download (presigned-URL style). Set `DOWNLOAD_URL_SECRET` in production so
  links survive restarts and span workers.
- **In-memory job registry** (`_jobs` dict + `threading.Lock`): simple for a
  demo; restart clears all jobs. Production would need persistent storage.
- **`sqlite3.interrupt()`** on `DELETE /jobs/{id}` while running: aborts the
  in-flight query without parsing SQL.
- **100k row cap**: queries returning more rows fail with a helpful error
  asking the caller to add a `LIMIT`.
- **Swagger UI** at `/docs` works out of the box — click Authorize and paste
  a key.

## Code Review Workflow

**After opening or updating a pull request, always self-review the diff** and
post a comment summarising what you checked and any issues found + fixed (run
the tests/linters and note the result). Never leave a PR without a self-review.

Whenever a pull request is created or updated, **always check for Gemini
code-review comments** (`gemini-code-assist[bot]`) using the GitHub MCP tools:

1. Call `pull_request_read` with `method=get_reviews` to find the Gemini review summary.
2. Call `pull_request_read` with `method=get_review_comments` to get inline thread details.
3. Verify each finding against the actual source files before acting.
4. Apply confirmed fixes, commit, and push on the same branch.
5. **Always reply to every Gemini (GCA) comment** with `add_reply_to_pull_request_comment` —
   either describing the fix applied, or explaining why the suggestion isn't
   being taken. Never leave a GCA review comment unacknowledged.

## Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/` | — | Service info |
| GET | `/portal` | — | Researcher portal web UI (sign in → key → schema) |
| POST | `/portal/register` | — | Issue a demo API key (`{name, email}`) — rate-limited, expiring |
| DELETE | `/portal/key` | key | Revoke your own portal-issued key |
| GET | `/fields` | key | Queryable fields + operations |
| GET | `/tables` | key | List tables |
| GET | `/schema/{table}` | key | Column info |
| POST | `/query` | key | Submit structured query → 202 + job_id |
| GET | `/jobs` | key | List your jobs |
| GET | `/jobs/{id}` | key | Job status |
| GET | `/jobs/{id}/result?format=json\|csv` | key | Result (status=done only) |
| GET | `/jobs/{id}/download?format=…&expires=…&sig=…` | signed URL | Secure download, no key needed |
| DELETE | `/jobs/{id}` | key | Cancel or remove |
