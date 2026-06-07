# api-demo — Claude context

## What this is

A FastAPI service that accepts **structured query parameters** (not SQL) via
HTTP, runs the resulting query asynchronously on background worker threads, and
returns results as JSON or CSV. Backed by a read-only SQLite database seeded from
the aggregated **EU Digital Services Act (DSA) VLOP transparency reports** —
content-moderation statistics for 33 designated Very Large Online Platforms /
Search Engines (H2 2025), tables 3–11 of the DSA Implementing Regulation template.

Built to demonstrate two things:

1. The **async-job / poll pattern**: `POST /query` returns `202 + job_id`
   immediately; the client polls `/jobs/{id}` until `status=done`, then
   fetches `/jobs/{id}/result`.
2. A **safe, no-SQL query interface** modelled on the TikTok Research API: a
   query names a `table` (one of the 9 DSA report tables), then a boolean
   `and`/`or`/`not` clause of `{operation, field_name, field_values}`, plus
   `group_by`, `aggregates`, `sort`, and `max_count`. The server validates
   everything against that table's fixed field registry (`TABLES`/`TableSpec`)
   and compiles it into a single parameterised SELECT (`compile_query` in
   `main.py`). Arbitrary SQL is never accepted or executed.

## Repo layout

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app — all endpoints, job runner, in-memory job registry |
| `seed.py` | Build `demo.db` from `../krMaynard.github.io/data/vlop-dsa.json` (`build_db()` is reused by `conftest.py`) |
| `demo.py` | Narrated walkthrough script (run after starting the server) |
| `static/portal.html` | Researcher portal single-page app (served at `/portal`) |
| `scripts/_demo_server.py` | Shared helper: seed DB + run a temp server (used by the GIF generators) |
| `scripts/make_gifs.py` | Headless terminal-demo GIF generator (pyte + Pillow) → `docs/gifs/` |
| `scripts/make_portal_gifs.py` | Portal-workflow GIF generator (Playwright + Pillow) → `docs/gifs/portal-*.gif` |
| `requirements.txt` | `fastapi` + `uvicorn[standard]` |
| `demo.db` | SQLite DB (git-ignored, produced by `seed.py`) |
| `.github/workflows/ci.yml` | CI: `pyflakes` lint + `pytest` on every PR/push (Python 3.11 & 3.12) |

## CI

GitHub Actions runs `pyflakes main.py seed.py demo.py conftest.py test_api.py`
and `pytest test_api.py` on every pull request and push to `main`. Keep both
green — the suite is hermetic (no Redis/server/`demo.db` needed; `conftest.py`
builds a temp DB). Run them locally before pushing.

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
  krMaynard.github.io/ ← source data lives at data/vlop-dsa.json
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

Seeded from `vlop-dsa.json` (compact interned format → star schema). Shared
dimension tables `services` (with `platform` = parent company), `categories`
(code + label), `sections`, `indicators`, `scopes`, `surfaces`, plus a `meta`
key/value table (`period`, `generated`). One **fact table per DSA report table**:

- **`t3_member_state_orders`** — Art. 9 & 10 orders, by category × scope
- **`t4_notices`** — Art. 16 notices, by category (+ Trusted-Flagger `tf_*`)
- **`t5_own_initiative_illegal`** / **`t6_own_initiative_tos`** — own-initiative actions, by category × 16 restriction types (t6 + surface)
- **`t7_appeals_recidivism`** / **`t8_automated_means`** — section × indicator × scope × surface → value
- **`t9_human_resources`** — section × indicator × scope → value
- **`t10_amar`** — Average Monthly Active Recipients, by scope
- **`t11_qualitative`** — free-text descriptions, by indicator (`value_text`)

Fact-row leading values are indices into the lookup arrays (= the dimension row
id), so seeding is positional. The DB is opened `mode=ro` as defence in depth.

## Query model

Requests are structured (see `QueryRequest`/`compile_query`/`TableSpec` in
`main.py`). A query **must name a `table`**; that table's `TableSpec` fixes the
FROM/joins and the registry of:

- **Dimensions** (text, `EQ`/`IN`): always `service_name`, `platform`; plus
  per-table `category_code`/`category_label`, `section`, `indicator`, `scope`,
  `surface`, or `qualitative_text` (t11).
- **Measures** (numeric, `EQ`/`IN`/`GT`/`GTE`/`LT`/`LTE`): per-table count
  columns (e.g. t4 `notices`/`tf_notices`/…, t7–t10 `value`). t11 has none.
- **Aggregates**: `SUM`/`COUNT`/`AVG`/`MIN`/`MAX` over a measure, with an alias.
- `group_by`, `sort`, `max_count`, optional `callback_url` (webhook). `GET /tables`
  lists the tables; `GET /fields?table=…` and `GET /schema/{table}` document a
  table's fields.

`compile_query` is the single trust boundary — it resolves `req.table` to a
`TableSpec` and validates every field/operation against that table's registry.
Never build SQL by interpolating user values (always bind with `?`).

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
- **Per-key query rate limit**: `POST /query` is throttled per API key
  (`QUERY_RATE_MAX`/`QUERY_RATE_WINDOW`, default 60/60s) via `_key_store.incr` —
  the same counter primitive as portal registration. Over-limit → `429` +
  `Retry-After`, before a job is created.
- **Structured logging**: a dedicated `api_demo` logger emits JSON lines
  (`JsonLogFormatter`, `LOG_FORMAT=json` default; `text` for humans). An HTTP
  middleware logs each request (method/path/status/`duration_ms`/`request_id`,
  echoed as `X-Request-ID`); the job runner logs `job_submitted`/`job_started`/
  `job_done`/`job_failed`. Pass fields via `extra={"data": {...}}`; never log keys.
- **Webhook callbacks**: an optional `callback_url` on `POST /query`. When the
  job reaches `done`/`failed`, `_dispatch_callback` POSTs the job object (with
  absolute links if `PUBLIC_BASE_URL` is set) to that URL on a **bounded callback
  thread pool** (`_callback_executor`, `CALLBACK_WORKERS`) — off the query
  workers — HMAC-signed (`X-Webhook-Signature`, same secret as download URLs),
  retried with backoff. SSRF-guarded: `_validate_callback_url` blocks non-http(s)
  and private/loopback/link-local/metadata targets, **unwrapping IPv4-mapped/6to4
  IPv6** so they can't smuggle a private v4; enforced at submit *and* before each
  send (narrows DNS rebinding — full closure needs network egress filtering);
  redirects aren't followed. `CALLBACK_ALLOW_PRIVATE=1` bypasses for local dev.
- **Prometheus metrics** at `GET /metrics` (no auth): the same request middleware
  records `api_demo_http_requests_total` + `_http_request_duration_seconds`,
  labelled by the **route template** (`/jobs/{job_id}`) to bound cardinality; the
  job runner tracks `api_demo_jobs_in_flight`, `api_demo_jobs_total{status}`, and
  `api_demo_job_queue_depth` (inc'd on submit, dec'd when the worker picks the job
  up — no reliance on `ThreadPoolExecutor` internals).
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
| GET | `/tables` | key | List the DSA report tables + dataset period |
| GET | `/fields?table=…` | key | Fields + operations for a table (no arg → table overview) |
| GET | `/schema/{table}` | key | Field registry for a report table |
| POST | `/query` | key | Submit structured query (optional `callback_url`) → 202 + job_id |
| GET | `/jobs` | key | List your jobs |
| GET | `/jobs/{id}` | key | Job status |
| GET | `/jobs/{id}/result?format=json\|csv` | key | Result (status=done only) |
| GET | `/jobs/{id}/download?format=…&expires=…&sig=…` | signed URL | Secure download, no key needed |
| DELETE | `/jobs/{id}` | key | Cancel or remove |
| GET | `/metrics` | — | Prometheus metrics |
