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
| `seed.py` | Build `demo.db` from a `vlop-dsa.json` (`--source`/`SEED_SOURCE_JSON`; default = sibling repo) — `build_db()` is reused by `conftest.py` |
| `data/vlop-dsa.json` | Vendored dataset snapshot — what the Docker image is seeded from (refresh via `scripts/refresh-dataset.sh`) |
| `demo.py` | Narrated walkthrough script (run after starting the server) |
| `static/index.html` | Public VLOP dashboard (served at `/`) — Chart.js overview + interactive query builder + NL "Ask" box (`GET /api/overview`, `POST /api/explore`, `POST /api/ask`) |
| `static/portal.html` | Researcher portal single-page app (served at `/portal`) — Google sign-in + demo fallback |
| `Dockerfile` | Self-contained image: installs deps, seeds `demo.db` at build time, runs uvicorn on `$PORT` as non-root |
| `service.yaml` | Cloud Run (Knative) manifest — prod env + startup/liveness probes |
| `scripts/refresh-dataset.sh` | Re-vendor `data/vlop-dsa.json` from the canonical sibling-repo dataset |
| `scripts/_demo_server.py` | Shared helper: seed DB + run a temp server (used by the GIF generators) |
| `scripts/make_gifs.py` | Headless terminal-demo GIF generator (pyte + Pillow) → `docs/gifs/` |
| `scripts/make_portal_gifs.py` | Portal-workflow GIF generator (Playwright + Pillow) → `docs/gifs/portal-*.gif` |
| `requirements.txt` | `fastapi` + `uvicorn[standard]` + `anthropic` (NL queries) |
| `demo.db` | SQLite DB (git-ignored, produced by `seed.py`) |
| `.github/workflows/ci.yml` | CI: `pyflakes` lint + `pytest` on every PR/push (Python 3.11 & 3.12) |
| `.github/workflows/deploy.yml` | CD: build/push image + deploy to Cloud Run on push to `main` (WIF; skips until configured) |
| `.gcloudignore` | Trims the Cloud Build upload context (keeps Dockerfile + `data/`) |

## CI

GitHub Actions runs `pyflakes main.py seed.py demo.py conftest.py test_api.py`
and `pytest test_api.py` on every pull request and push to `main` (`ci.yml`).
Keep both green — the suite is hermetic (no Redis/server/`demo.db` needed;
`conftest.py` builds a temp DB). Run them locally before pushing.

`deploy.yml` builds + pushes the image and rolls a Cloud Run revision on push to
`main` via Workload Identity Federation, stamping the commit SHA as `APP_VERSION`.
It deploys with `--no-traffic`, smoke-tests the new revision's `/readyz`, then
promotes it with `update-traffic --to-latest`. Gated on the `GCP_PROJECT_ID` repo
variable, so it **skips** (not fails) until GCP is configured — see README →
"Continuous deployment". `.gcloudignore` keeps the Cloud Build upload lean.

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

Two mechanisms, both presented as `X-API-Key` to the rest of the app:

- **Google sign-in (production).** The frontend uses Google Identity Services
  (FedCM in supporting browsers) to get an ID token and POSTs it to
  `/auth/google`. `_verify_id_token` validates it against `GOOGLE_CLIENT_ID`.
  New accounts become a `pending` registration; an admin (`ADMIN_EMAILS`,
  comma-separated, implicitly approved) approves via `/admin/registrations/*`.
  An approved login mints a first-party **session key** (`gs_…`) into
  `_key_store` (TTL `GOOGLE_SESSION_TTL`). `_lookup_principal` re-checks the
  registration on every request, so an admin revoke kills live sessions at once.
  Durable approval state lives in `_registrations` (Redis-backed when configured,
  else in-memory — same pattern as `_key_store`).
- **Demo keys (dev).** Hard-coded `alice`/`bob` + the open `/portal/register`.
  Gated by `ALLOW_DEMO_KEYS` (default on); set `ALLOW_DEMO_KEYS=0` in production.

Jobs are scoped per key — each principal only sees their own jobs (foreign IDs
return 404, not 403). `require_admin` gates the admin endpoints on the principal's
email being in `ADMIN_EMAILS`.

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
- **NL→query via LLM, same trust boundary** (`POST /api/ask`): an LLM (Claude;
  `ANTHROPIC_MODEL`, default `claude-opus-4-8`) translates a question into the
  *structured* `QueryRequest` using JSON-schema structured outputs — never SQL —
  which then goes through the exact same `compile_query` validation as everything
  else. The model only proposes; `compile_query` disposes (bad field → `422`).
  `_translate_question` is the single, lazily-imported, monkeypatchable seam (tests
  never call the API); off unless `ANTHROPIC_API_KEY` is set; IP-rate-limited.
  Before changing the LLM call, confirm the current model ID + Messages API schema
  (use the `claude-api` skill) — never hardcode a model ID from memory.
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
- **Browser hardening**: every response gets `X-Content-Type-Options: nosniff`
  (request middleware); the two HTML pages (`/`, `/portal`) get a per-page
  **Content-Security-Policy** (`_serve_page`/`_page_csp`) — `script-src 'self'` +
  the page's inline-`<script>` **sha256 hash** (computed from the file, never
  stale) + the one CDN it needs (jsDelivr / `accounts.google.com`); no
  `'unsafe-inline'` for scripts, `frame-ancestors 'none'`. DB values are
  HTML-escaped in the dashboard JS (`esc()`).

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

Combined-site layout: the **dashboard is served at `/`** and the JSON API lives
under **`/api/*`** on the same origin (no CORS). Operational endpoints
(`/healthz`, `/readyz`, `/metrics`, `/version`) and the `/portal` page stay at the
root. The API endpoints are registered on an `APIRouter` included with
`prefix=API_PREFIX` (`/api`); link builders (`status_url`/`result_url`/signed
`download_urls`/`Location`) are prefixed via `API_PREFIX`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/` | — | Public VLOP transparency dashboard (web UI) |
| GET | `/api/overview` | — | Public headline aggregates powering the dashboard |
| GET | `/api/explore/options` | — | Public: tables + dimensions/measures for the query builder |
| POST | `/api/explore` | — | Public: run a bounded structured query inline (row-capped, IP-rate-limited) |
| POST | `/api/ask` | — | Public: NL→query via an LLM (Claude) → structured `QueryRequest` → `compile_query`; off unless `ANTHROPIC_API_KEY` set |
| GET | `/api` | — | API service info |
| GET | `/portal` | — | Researcher portal web UI (sign in → key → schema) |
| POST | `/api/auth/google` | — | Verify a Google ID token → session key, or `202` pending approval |
| POST | `/api/portal/register` | — | Demo: issue a key without auth (`ALLOW_DEMO_KEYS`) |
| DELETE | `/api/portal/key` | key | Revoke your own session / portal-issued key |
| GET | `/api/admin/registrations` | admin | List researcher registrations (`?status=`) |
| POST | `/api/admin/registrations/{email}/approve` | admin | Approve an account |
| POST | `/api/admin/registrations/{email}/revoke` | admin | Revoke an account |
| GET | `/api/tables` | key | List the DSA report tables + dataset period |
| GET | `/api/fields?table=…` | key | Fields + operations for a table (no arg → table overview) |
| GET | `/api/schema/{table}` | key | Field registry for a report table |
| POST | `/api/query` | key | Submit structured query (optional `callback_url`) → 202 + job_id |
| GET | `/api/jobs` | key | List your jobs |
| GET | `/api/jobs/{id}` | key | Job status |
| GET | `/api/jobs/{id}/result?format=json\|csv` | key | Result (status=done only) |
| GET | `/api/jobs/{id}/download?format=…&expires=…&sig=…` | signed URL | Secure download, no key needed |
| DELETE | `/api/jobs/{id}` | key | Cancel or remove |
| GET | `/healthz` `/readyz` | — | Liveness / readiness probes (root) |
| GET | `/metrics` | — | Prometheus metrics |
| GET | `/version` | — | Deployed build (commit SHA via `APP_VERSION`); also the `X-Version` header |
