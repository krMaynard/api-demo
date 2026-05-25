"""Demo SQL-over-HTTP API for researchers, with async job execution.

Long-running queries can't tie up the HTTP connection, so submitting a query
returns 202 + a job id immediately. The query runs on a background worker;
clients poll /jobs/{id} for status and fetch /jobs/{id}/result when done.

The database is opened read-only so DDL/DML are rejected by SQLite itself.

Configuration (environment variables):
    DB_PATH                path to the SQLite database (default: demo.db beside this file)
    ROW_LIMIT              max rows returned per query (default: 100000)
    WORKER_THREADS         background worker count (default: 4)
    QUERY_TIMEOUT_SECONDS  SQLite busy timeout in seconds (default: 300)
    REDIS_URL              standard Redis URL (rediss://...); used with redis-py
    UPSTASH_REDIS_REST_URL    Upstash REST URL  } use these two together
    UPSTASH_REDIS_REST_TOKEN  Upstash REST token} instead of REDIS_URL
    JOB_TTL_SECONDS        how long to retain completed jobs in Redis (default: 86400)
    API_KEYS_JSON          JSON object mapping key→{name:str}; falls back to demo keys
"""
import csv
import io
import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "demo.db"))
ROW_LIMIT = int(os.getenv("ROW_LIMIT", "100000"))
WORKER_THREADS = int(os.getenv("WORKER_THREADS", "4"))
QUERY_TIMEOUT_SECONDS = int(os.getenv("QUERY_TIMEOUT_SECONDS", "300"))
REDIS_URL = os.getenv("REDIS_URL")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "86400"))


def _load_api_keys() -> dict[str, dict[str, str]]:
    raw = os.getenv("API_KEYS_JSON")
    if raw:
        return json.loads(raw)
    # Demo fallback — replace with a secret store in production.
    return {"alice": {"name": "alice"}, "bob": {"name": "bob"}}


API_KEYS = _load_api_keys()

# ── Auth ──────────────────────────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Depends(api_key_header)) -> dict[str, str]:
    if not key or key not in API_KEYS:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key. Set header `X-API-Key: <key>`.",
        )
    return {"key": key, **API_KEYS[key]}


# ── Job model ─────────────────────────────────────────────────────────────────

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class Job:
    id: str
    sql: str
    owner_key: str
    submitted_by: str
    status: JobStatus = "queued"
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    row_count: int | None = None
    # columns/rows live in the result store, not on the job object.
    columns: list[str] | None = field(default=None, repr=False)
    rows: list[list[Any]] | None = field(default=None, repr=False)

    def to_public(self) -> dict[str, Any]:
        rc = len(self.rows) if self.rows is not None else self.row_count
        return {
            "job_id": self.id,
            "status": self.status,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "row_count": rc,
            "status_url": f"/jobs/{self.id}",
            "result_url": f"/jobs/{self.id}/result" if self.status == "done" else None,
        }


# ── Job stores ────────────────────────────────────────────────────────────────

# Active sqlite connections live in memory regardless of which store is used —
# sqlite3.Connection cannot be serialised into Redis.
_active_conns: dict[str, sqlite3.Connection] = {}
_active_conns_lock = threading.Lock()


class MemoryJobStore:
    """Single-process in-memory store. Default for local dev and tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def put(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update_fields(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                for k, v in fields.items():
                    setattr(job, k, v)

    def save_result(self, job_id: str, columns: list[str], rows: list[list[Any]]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.columns = columns
                job.rows = rows
                job.row_count = len(rows)

    def get_result(self, job_id: str) -> tuple[list[str], list[list[Any]]] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.columns is None:
                return None
            return job.columns, job.rows or []

    def remove(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.pop(job_id, None)

    def list_for_owner(self, owner_key: str, limit: int) -> list[Job]:
        with self._lock:
            mine = [j for j in self._jobs.values() if j.owner_key == owner_key]
            return sorted(mine, key=lambda j: j.submitted_at, reverse=True)[:limit]


class RedisJobStore:
    """Redis-backed store. Enables multiple web/worker processes and survives restarts.

    Accepts any client with a redis-py-compatible interface — works with
    both redis-py (via REDIS_URL) and upstash-redis (via UPSTASH_REDIS_REST_*).
    """

    def __init__(self, client: Any, ttl: int = JOB_TTL) -> None:
        self._r = client
        self._ttl = ttl

    # key helpers
    def _jk(self, job_id: str) -> str:
        return f"job:{job_id}"

    def _rk(self, job_id: str) -> str:
        return f"job_result:{job_id}"

    def _ok(self, owner_key: str) -> str:
        return f"owner_jobs:{owner_key}"

    def _to_hash(self, job: Job) -> dict[str, str]:
        return {
            "id": job.id,
            "sql": job.sql,
            "owner_key": job.owner_key,
            "submitted_by": job.submitted_by,
            "status": job.status,
            "submitted_at": job.submitted_at,
            "started_at": job.started_at or "",
            "finished_at": job.finished_at or "",
            "error": job.error or "",
            "row_count": "" if job.row_count is None else str(job.row_count),
        }

    def _from_hash(self, h: dict[str, str]) -> Job:
        return Job(
            id=h["id"],
            sql=h["sql"],
            owner_key=h["owner_key"],
            submitted_by=h["submitted_by"],
            status=h["status"],  # type: ignore[arg-type]
            submitted_at=h["submitted_at"],
            started_at=h.get("started_at") or None,
            finished_at=h.get("finished_at") or None,
            error=h.get("error") or None,
            row_count=int(h["row_count"]) if h.get("row_count") else None,
        )

    def put(self, job: Job) -> None:
        key = self._jk(job.id)
        self._r.hset(key, mapping=self._to_hash(job))
        self._r.expire(key, self._ttl)
        ts = datetime.fromisoformat(job.submitted_at).timestamp()
        self._r.zadd(self._ok(job.owner_key), {job.id: ts})

    def get(self, job_id: str) -> Job | None:
        h = self._r.hgetall(self._jk(job_id))
        return self._from_hash(h) if h else None

    def update_fields(self, job_id: str, **fields: Any) -> None:
        mapping = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        if mapping:
            self._r.hset(self._jk(job_id), mapping=mapping)
            self._r.expire(self._jk(job_id), self._ttl)

    def save_result(self, job_id: str, columns: list[str], rows: list[list[Any]]) -> None:
        payload = json.dumps({"columns": columns, "rows": rows})
        self._r.set(self._rk(job_id), payload, ex=self._ttl)
        self.update_fields(job_id, row_count=len(rows))

    def get_result(self, job_id: str) -> tuple[list[str], list[list[Any]]] | None:
        raw = self._r.get(self._rk(job_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return data["columns"], data["rows"]

    def remove(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        self._r.delete(self._jk(job_id))
        self._r.delete(self._rk(job_id))
        self._r.zrem(self._ok(job.owner_key), job_id)
        return job

    def list_for_owner(self, owner_key: str, limit: int) -> list[Job]:
        job_ids = self._r.zrevrange(self._ok(owner_key), 0, limit - 1)
        return [j for jid in job_ids if (j := self.get(jid)) is not None]


def _make_store() -> MemoryJobStore | RedisJobStore:
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        from upstash_redis import Redis as UpstashRedis
        client = UpstashRedis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
        return RedisJobStore(client)
    if REDIS_URL:
        import redis
        return RedisJobStore(redis.from_url(REDIS_URL, decode_responses=True))
    return MemoryJobStore()


_store: MemoryJobStore | RedisJobStore = _make_store()
_executor = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="sql-worker")

app = FastAPI(
    title="SQL Query Demo API (async jobs)",
    description="Submit a SQL query, get a job id, poll for results as JSON or CSV.",
    version="0.3.0",
)


class QueryRequest(BaseModel):
    sql: str = Field(..., description="A single SELECT statement.", min_length=1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_ro() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError("demo.db not found — run `python seed.py` first.")
    return sqlite3.connect(
        f"file:{DB_PATH}?mode=ro",
        uri=True,
        timeout=QUERY_TIMEOUT_SECONDS,
        check_same_thread=False,
    )


def _execute_job(job_id: str) -> None:
    job = _store.get(job_id)
    if job is None or job.status == "cancelled":
        return

    _store.update_fields(job_id, status="running", started_at=_now())

    try:
        conn = _connect_ro()
        with _active_conns_lock:
            _active_conns[job_id] = conn

        try:
            cur = conn.execute(job.sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(ROW_LIMIT + 1)
        finally:
            with _active_conns_lock:
                _active_conns.pop(job_id, None)
            conn.close()

        refreshed = _store.get(job_id)
        if refreshed is None or refreshed.status == "cancelled":
            return

        if len(rows) > ROW_LIMIT:
            raise ValueError(f"Result exceeds {ROW_LIMIT} rows; add a LIMIT clause.")

        _store.save_result(job_id, cols, [list(r) for r in rows])
        _store.update_fields(job_id, status="done", finished_at=_now())

    except sqlite3.OperationalError as exc:
        refreshed = _store.get(job_id)
        if refreshed and refreshed.status != "cancelled":
            _store.update_fields(
                job_id, status="failed", error=f"SQL error: {exc}", finished_at=_now()
            )
    except Exception as exc:
        refreshed = _store.get(job_id)
        if refreshed and refreshed.status != "cancelled":
            _store.update_fields(
                job_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=_now(),
            )


def _job_for_owner(job_id: str, owner_key: str) -> Job:
    """Return the job or 404. Foreign job IDs always return 404 to avoid leaking existence."""
    job = _store.get(job_id)
    if job is None or job.owner_key != owner_key:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "SQL Query Demo API",
        "pattern": "async-job",
        "auth": "X-API-Key header required for all endpoints except `/`, `/docs`, `/openapi.json`",
        "endpoints": {
            "POST /query": "Submit a SQL query, returns 202 + job_id",
            "GET /jobs": "List your jobs",
            "GET /jobs/{job_id}": "Job status (your jobs only)",
            "GET /jobs/{job_id}/result?format=json|csv": "Result (only when status=done)",
            "DELETE /jobs/{job_id}": "Cancel a queued/running job, or remove a finished one",
            "GET /tables": "List tables in the demo database",
            "GET /schema/{table}": "Show columns for a table",
            "GET /healthz": "Liveness probe",
            "GET /readyz": "Readiness probe (checks DB connection)",
            "GET /docs": "Interactive Swagger UI",
        },
        "row_limit": ROW_LIMIT,
        "worker_threads": WORKER_THREADS,
        "store": "upstash" if UPSTASH_REDIS_REST_URL else ("redis" if REDIS_URL else "memory"),
    }


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def ready() -> dict[str, str]:
    try:
        _connect_ro().close()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok"}


@app.get("/tables")
def list_tables(_: dict = Depends(require_api_key)) -> dict[str, list[str]]:
    with _connect_ro() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return {"tables": [r[0] for r in rows]}


@app.get("/schema/{table}")
def table_schema(table: str, _: dict = Depends(require_api_key)) -> dict[str, Any]:
    if not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid table name.")
    with _connect_ro() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Table '{table}' not found.")
    return {
        "table": table,
        "columns": [
            {"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])}
            for r in rows
        ],
    }


@app.post("/query", status_code=202)
def submit_query(
    body: QueryRequest,
    response: Response,
    principal: dict = Depends(require_api_key),
) -> dict[str, Any]:
    job = Job(
        id=uuid.uuid4().hex,
        sql=body.sql,
        owner_key=principal["key"],
        submitted_by=principal["name"],
    )
    _store.put(job)
    _executor.submit(_execute_job, job.id)
    response.headers["Location"] = f"/jobs/{job.id}"
    return job.to_public()


@app.get("/jobs")
def list_jobs(limit: int = 50, principal: dict = Depends(require_api_key)) -> dict[str, Any]:
    return {"jobs": [j.to_public() for j in _store.list_for_owner(principal["key"], limit)]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, principal: dict = Depends(require_api_key)) -> dict[str, Any]:
    return _job_for_owner(job_id, principal["key"]).to_public()


@app.get("/jobs/{job_id}/result", response_model=None)
def get_job_result(
    job_id: str,
    format: Literal["json", "csv"] = "json",
    principal: dict = Depends(require_api_key),
) -> JSONResponse | PlainTextResponse:
    job = _job_for_owner(job_id, principal["key"])
    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job not ready (status={job.status}). Poll {job.id} again.",
        )

    result = _store.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found (may have expired).")
    cols, rows = result

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        writer.writerows(rows)
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.csv"'},
        )
    return JSONResponse({"columns": cols, "rows": rows, "row_count": len(rows)})


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str, principal: dict = Depends(require_api_key)) -> dict[str, Any]:
    job = _job_for_owner(job_id, principal["key"])
    prior = job.status

    if prior in ("queued", "running"):
        _store.update_fields(job_id, status="cancelled", finished_at=_now())
        with _active_conns_lock:
            conn = _active_conns.get(job_id)
        if conn is not None:
            try:
                conn.interrupt()
            except sqlite3.Error:
                pass

    _store.remove(job_id)
    return {"job_id": job_id, "previous_status": prior, "deleted": True}
