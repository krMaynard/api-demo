# Product Requirements Document — api-demo

**Version:** 1.0  
**Date:** 2026-05-26  
**Status:** Draft  
**Owner:** Kieran Maynard

---

## 1. Overview

### 1.1 Purpose

`api-demo` is a FastAPI service that exposes a read-only SQLite database (seeded from the Google Government Content Removals dataset) as a queryable HTTP API. It is designed to demonstrate the **async-job / poll pattern** for long-running SQL queries without tying up HTTP connections.

### 1.2 Problem Statement

Analysts and researchers who need to run ad-hoc SQL against shared datasets face two pain points:

1. **Blocking HTTP** — long queries time out at proxies, load balancers, or client libraries before they complete.
2. **No isolation** — without per-user job scoping, users can see (or interfere with) each other's work.

`api-demo` solves both by introducing an asynchronous job model backed by a thread pool and a per-key access control layer.

### 1.3 Goals

| Goal | Success Metric |
|------|----------------|
| Demonstrate the 202 + poll pattern clearly | Demo script (`demo.py`) runs end-to-end without errors |
| Provide a safe, read-only SQL interface | Write attempts fail at the database layer; no SQL parsing required |
| Scope jobs to the submitting user | A foreign job ID always returns 404, not the job's data |
| Be horizontally scalable with minimal changes | Swap `REDIS_URL` env var to switch from in-memory to Redis backend |

### 1.4 Non-Goals

- Production authentication (keys are hard-coded for demo purposes)
- Write access to the database
- Real-time streaming of query results
- Multi-database or cross-database queries

---

## 2. Users & Personas

### 2.1 Alice — The Researcher

Alice wants to run exploratory SQL against a public dataset from a notebook or script. She submits a query, polls until it finishes, and downloads the result as CSV for further analysis. She cares about **result fidelity** and **response format flexibility** (JSON vs CSV).

### 2.2 Bob — The Second Researcher

Bob runs independently on the same API instance. He should never see Alice's jobs — not even whether a job ID exists. He cares about **data isolation**.

### 2.3 Demo Operator

A developer giving a live presentation who runs `demo.py --pause` to step through the API flow interactively. They care about **legibility** (colored output, truncated JSON) and **resilience** (the demo should recover gracefully from a server not being ready).

---

## 3. Functional Requirements

### 3.1 Public Endpoints (no auth)

| ID | Requirement |
|----|-------------|
| F-01 | `GET /` returns service metadata: API name, version, list of endpoints, current configuration (row limit, worker count, backing store type). The DB path is intentionally not exposed to avoid leaking internal server paths. |
| F-02 | `GET /healthz` always returns `{"status": "ok"}`. Used as a liveness probe. |
| F-03 | `GET /readyz` attempts a DB connection and returns `{"status": "ok"}` on success or `503` with an error detail on failure. Used as a readiness probe. |
| F-04 | Swagger UI is available at `/docs`; OpenAPI schema at `/openapi.json`. No auth required. |

### 3.2 Authentication

| ID | Requirement |
|----|-------------|
| F-05 | All data endpoints require an `X-API-Key` header. Missing or unrecognized keys receive `401 Unauthorized`. |
| F-06 | API keys map to a user name (metadata). Default demo keys are `alice` and `bob`; the key-to-name mapping is configurable via `API_KEYS_JSON` env var. |
| F-07 | Jobs are owned by the API key that created them. Requests for another owner's job ID return `404 Not Found` (not `403`), preventing job-existence timing attacks. |

### 3.3 Schema Discovery

| ID | Requirement |
|----|-------------|
| F-08 | `GET /tables` returns the list of all tables in the database. |
| F-09 | `GET /schema/{table}` returns column names, declared types, primary-key flag, and NOT NULL flag for the given table. |
| F-10 | A request for a non-existent table returns `404`. A table name containing invalid characters (e.g., semicolons) returns `400`. |

### 3.4 Query Submission

| ID | Requirement |
|----|-------------|
| F-11 | `POST /query` accepts a JSON body `{"sql": "<query>"}` and returns `202 Accepted` immediately with a job object and a `Location` header pointing to the job status URL. |
| F-12 | The submitted query is queued and executed on a background thread pool. The caller does not block waiting for execution. |
| F-13 | If the query result exceeds `ROW_LIMIT` rows (default 100,000), the job fails with a descriptive error asking the caller to add a `LIMIT` clause. |
| F-14 | Write operations (INSERT, UPDATE, DELETE, CREATE, DROP, etc.) are rejected by SQLite (database opened read-only). The job fails with the SQLite error message. No SQL parsing is required. |

### 3.5 Job Lifecycle

Jobs transition through the following states:

```
queued → running → done
                 → failed
       → cancelled   (DELETE while queued)
         running → cancelled   (DELETE while running; DB connection interrupted)
```

| ID | Requirement |
|----|-------------|
| F-15 | `GET /jobs` lists the authenticated user's jobs, newest first, with a configurable `limit` query param. |
| F-16 | `GET /jobs/{id}` returns full job metadata: status, timestamps, row count, error message (if any), and a `result_url` (only when `status=done`). |
| F-17 | `DELETE /jobs/{id}` on a queued job marks it `cancelled` before it starts. On a running job it calls `sqlite3.interrupt()` to abort the in-flight query. In all cases the job is **immediately removed from the registry** after the status update, so subsequent requests for that job ID return `404 Not Found` rather than a `cancelled` status. |
| F-18 | Completed jobs (done/failed/cancelled) expire automatically after `JOB_TTL_SECONDS` (default: 86,400 s / 24 h) when a Redis backend is in use. |

### 3.6 Result Retrieval

| ID | Requirement |
|----|-------------|
| F-19 | `GET /jobs/{id}/result` returns the query result when `status=done`. A `format` query param selects `json` (default) or `csv`. |
| F-20 | JSON format: `{"columns": [...], "rows": [[...], ...], "row_count": N}`. |
| F-21 | CSV format: standard RFC 4180 CSV with a header row. Response `Content-Type` is `text/csv`. |
| F-22 | If the job is not yet done, the endpoint returns `409 Conflict` with the current status. |
| F-23 | If the result has expired from the store, the endpoint returns `404` with a message indicating expiry. |

---

## 4. Non-Functional Requirements

### 4.1 Performance

| ID | Requirement |
|----|-------------|
| NF-01 | `POST /query` must return within **500 ms** regardless of query complexity (job is queued, not run inline). |
| NF-02 | The thread pool size is configurable via `WORKER_THREADS` (default: 4). Concurrent queries beyond pool capacity are queued, not rejected. |
| NF-03 | Query timeout is configurable via `QUERY_TIMEOUT_SECONDS` (default: 300 s). SQLite busy timeout enforces this limit. |

### 4.2 Reliability

| ID | Requirement |
|----|-------------|
| NF-04 | A database file not found at startup surfaces a clear error: `"demo.db not found — run python seed.py first."` |
| NF-05 | Unhandled exceptions in background threads are caught; the job transitions to `failed` with the exception type and message. No thread should crash silently. |

### 4.3 Security

| ID | Requirement |
|----|-------------|
| NF-06 | The SQLite database is opened with `mode=ro` (URI read-only flag). No app-layer SQL filtering is required or performed. |
| NF-07 | Job isolation (see F-07) prevents information leakage between users. |
| NF-08 | API keys in production deployments must be supplied via `API_KEYS_JSON` env var or a secret store, not hard-coded. (Demo keys are an explicit exception for local use.) |

### 4.4 Scalability

| ID | Requirement |
|----|-------------|
| NF-09 | When `REDIS_URL` (or `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`) is set, the service uses a Redis-backed job store, enabling multiple instances to share job state. |
| NF-10 | The in-memory store is the default for single-instance / dev use. A process restart clears all jobs (documented behavior, not a bug). |

### 4.5 Operability

| ID | Requirement |
|----|-------------|
| NF-11 | `/healthz` and `/readyz` are suitable as Kubernetes liveness and readiness probes respectively. |
| NF-12 | All timestamps are UTC ISO 8601 strings. |
| NF-13 | The service is 12-factor: all tunables (DB path, row limit, worker count, TTL, auth keys, Redis URL) are environment variables with sensible defaults. |

---

## 5. Data Model

### 5.1 SQLite Schema (Star Schema)

The database is seeded from the [Google Government Content Removals](https://transparencyreport.google.com/government-removals/overview) dataset.

**Dimension tables** (each has `id INTEGER PRIMARY KEY` as surrogate key, plus table-specific descriptive columns):

| Table | Columns | Description |
|-------|---------|-------------|
| `periods` | `id`, `label` | Reporting period labels, e.g. "January – June 2024" |
| `countries` | `id`, `code`, `name` | ISO country code + display name |
| `requestors` | `id`, `name` | Type of requesting entity (Court Order, Police, etc.) |
| `products` | `id`, `name` | Google product (YouTube, Web Search, Maps, etc.) |
| `reasons` | `id`, `name` | Removal reason (Defamation, National Security, Privacy, etc.) |

**Fact table — `removals`**:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Surrogate key |
| `period_id` | INTEGER FK | → periods |
| `country_id` | INTEGER FK | → countries |
| `requestor_id` | INTEGER FK | → requestors |
| `product_id` | INTEGER FK | → products |
| `reason_id` | INTEGER FK | → reasons |
| `num_requests` | INTEGER | Number of legal requests |
| `items_requested` | INTEGER | Items flagged for removal |
| `removed_legal` | INTEGER | Removed for legal reasons |
| `removed_policy` | INTEGER | Removed for policy violations |
| `not_found` | INTEGER | Items not found |
| `not_enough_info` | INTEGER | Requests with insufficient info |
| `no_action` | INTEGER | Items with no action taken |
| `already_removed` | INTEGER | Items already removed |

Indexes exist on all foreign key columns in `removals` to accelerate joins.

### 5.2 Job Object

```
Job {
  id:            UUID (hex)
  sql:           string
  owner_key:     string        # API key (not exposed in responses)
  submitted_by:  string        # Display name from key metadata
  status:        enum { queued | running | done | failed | cancelled }
  submitted_at:  ISO 8601 UTC
  started_at:    ISO 8601 UTC | null
  finished_at:   ISO 8601 UTC | null
  error:         string | null
  row_count:     integer | null
}
```

Results (columns + rows) are stored separately from the job object, keyed by `job_id`, and expire with the job.

---

## 6. API Contract Summary

### 6.1 Submit a Query

**Request:**
```http
POST /query
X-API-Key: alice
Content-Type: application/json

{"sql": "SELECT country_id, SUM(items_requested) AS total FROM removals GROUP BY 1 ORDER BY 2 DESC LIMIT 5"}
```

**Response `202 Accepted`:**
```http
Location: /jobs/a1b2c3d4...
Content-Type: application/json

{
  "job_id": "a1b2c3d4...",
  "status": "queued",
  "submitted_by": "alice",
  "submitted_at": "2026-05-26T12:00:00Z",
  "started_at": null,
  "finished_at": null,
  "error": null,
  "row_count": null,
  "status_url": "/jobs/a1b2c3d4..."
}
```

### 6.2 Poll for Completion

```http
GET /jobs/a1b2c3d4...
X-API-Key: alice
```

Returns the same job object with updated `status`, timestamps, and `row_count`. When `status=done`, a `result_url` field is also present.

### 6.3 Fetch Result

```http
GET /jobs/a1b2c3d4.../result?format=json
X-API-Key: alice
```

```json
{
  "columns": ["country_id", "total"],
  "rows": [[42, 182345], [7, 90123], ...],
  "row_count": 5
}
```

---

## 7. Configuration Reference

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `DB_PATH` | `demo.db` | Path to the SQLite file |
| `ROW_LIMIT` | `100000` | Max rows per query result |
| `WORKER_THREADS` | `4` | Background thread pool size |
| `QUERY_TIMEOUT_SECONDS` | `300` | SQLite busy timeout (seconds) |
| `JOB_TTL_SECONDS` | `86400` | Completed job expiry (Redis only) |
| `API_KEYS_JSON` | (demo keys) | JSON map of `{key: {name: ...}}` |
| `REDIS_URL` | _(unset)_ | Redis connection URL |
| `UPSTASH_REDIS_REST_URL` | _(unset)_ | Upstash HTTP endpoint (alternative to REDIS_URL) |
| `UPSTASH_REDIS_REST_TOKEN` | _(unset)_ | Upstash auth token |

---

## 8. Error Reference

| HTTP Status | Condition |
|-------------|-----------|
| 400 | Invalid table name in `/schema/{table}` |
| 401 | Missing or invalid `X-API-Key` |
| 404 | Job not found (or belongs to another user) |
| 404 | Table not found in `/schema/{table}` |
| 404 | Result not found / expired |
| 409 | Result requested but job not in `done` state |
| 503 | Database unavailable at `/readyz` |

SQL errors and row-limit violations surface as `status=failed` with a human-readable `error` field on the job, not as HTTP errors.

---

## 9. Future Considerations

The following items are out of scope for this demo but are natural next steps for a production version:

| Item | Notes |
|------|-------|
| **Persistent auth** | Replace hard-coded keys with a secret store (Vault, AWS Secrets Manager, etc.) |
| **Rate limiting** | Per-key query concurrency or per-minute limits |
| **Query cost estimation** | EXPLAIN QUERY PLAN before running to reject expensive queries |
| **Streaming results** | Server-Sent Events or chunked transfer for very large result sets |
| **Result pagination** | Cursor-based pagination on `/jobs/{id}/result` |
| **Audit logging** | Structured logs of who ran what SQL and when |
| **Multiple databases** | Route queries to different DB files based on path or header |
| **Webhook callbacks** | POST to a caller-supplied URL when a job finishes (eliminates polling) |
| **Admin endpoints** | Metrics, job queue depth, active connection count |
