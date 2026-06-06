"""Demo structured-query API for researchers, with async job execution.

Clients do NOT send SQL. They describe what they want with structured
parameters — boolean `and`/`or`/`not` clauses of `{operation, field_name,
field_values}`, plus optional `group_by`, `aggregates`, `sort`, and
`max_count` — modelled on the TikTok Research API. The server validates the
request against a fixed field registry and compiles it into a single
parameterised SELECT, so arbitrary SQL is never executed.

Long-running queries can't tie up the HTTP connection, so submitting a query
returns 202 + a job id immediately. The query runs on a background worker;
clients poll /jobs/{id} for status and fetch /jobs/{id}/result when done.

The database is additionally opened read-only as defence in depth.

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
    DOWNLOAD_URL_SECRET    HMAC secret for signing secure download URLs
                           (default: a random per-process secret — set this in
                           production so links survive restarts and span workers)
    DOWNLOAD_URL_TTL_SECONDS  how long a signed download URL stays valid (default: 3600)
"""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "demo.db"))
ROW_LIMIT = int(os.getenv("ROW_LIMIT", "100000"))
WORKER_THREADS = int(os.getenv("WORKER_THREADS", "4"))
QUERY_TIMEOUT_SECONDS = int(os.getenv("QUERY_TIMEOUT_SECONDS", "300"))
REDIS_URL = os.getenv("REDIS_URL")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "86400"))
# Secret for signing capability-style download URLs. A random per-process
# default keeps the demo zero-config, but signed links then break on restart and
# aren't shared across workers — set DOWNLOAD_URL_SECRET in any real deployment.
DOWNLOAD_URL_SECRET = os.getenv("DOWNLOAD_URL_SECRET") or secrets.token_hex(32)
DOWNLOAD_URL_TTL = int(os.getenv("DOWNLOAD_URL_TTL_SECONDS", "3600"))


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


# ── Secure download URLs ──────────────────────────────────────────────────────
#
# A finished job exposes a capability-style download URL: a signed, expiring link
# that streams the result without an API key (like an S3 presigned URL). The
# signature binds the job id, owner, format, and expiry with HMAC-SHA256, so the
# link can't be tampered with or repointed at another job, and stops working
# once it expires.

DOWNLOAD_FORMATS = ("json", "csv")


def _download_signature(job_id: str, owner_key: str, fmt: str, expires: int) -> str:
    msg = f"{job_id}:{owner_key}:{fmt}:{expires}".encode()
    return hmac.new(DOWNLOAD_URL_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _make_download_url(job_id: str, owner_key: str, fmt: str) -> str:
    expires = int(time.time()) + DOWNLOAD_URL_TTL
    sig = _download_signature(job_id, owner_key, fmt, expires)
    return f"/jobs/{job_id}/download?format={fmt}&expires={expires}&sig={sig}"


def _verify_download_signature(
    job_id: str, owner_key: str, fmt: str, expires: int, sig: str
) -> bool:
    expected = _download_signature(job_id, owner_key, fmt, expires)
    return hmac.compare_digest(expected, sig)


# ── Job model ─────────────────────────────────────────────────────────────────

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class Job:
    id: str
    sql: str  # compiled, parameterised SELECT (never user-authored SQL)
    params: list[Any]  # bound parameters for the compiled SQL
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
            "compiled_sql": self.sql,
            "status_url": f"/jobs/{self.id}",
            "result_url": f"/jobs/{self.id}/result" if self.status == "done" else None,
            # Signed, expiring links that download the result without an API key.
            "download_urls": (
                {fmt: _make_download_url(self.id, self.owner_key, fmt) for fmt in DOWNLOAD_FORMATS}
                if self.status == "done"
                else None
            ),
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
        # upstash-redis uses zadd(key, score, member); redis-py uses zadd(key, {member: score})
        self._is_upstash = client.__class__.__module__.startswith("upstash_redis")

    def _zadd(self, key: str, score: float, member: str) -> None:
        """Normalise zadd signature across redis-py and upstash-redis."""
        if self._is_upstash:
            self._r.zadd(key, score, member)
        else:
            self._r.zadd(key, {member: score})

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
            "params": json.dumps(job.params),
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
            params=json.loads(h.get("params") or "[]"),
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
        self._zadd(self._ok(job.owner_key), ts, job.id)

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
    title="Structured Query Demo API (async jobs)",
    description=(
        "Describe a query with structured parameters (no SQL), get a job id, "
        "poll for results as JSON or CSV. Query syntax follows the TikTok "
        "Research API: boolean and/or/not clauses of {operation, field_name, "
        "field_values}."
    ),
    version="0.4.0",
)


# ── Structured query model (TikTok-Research-API-style) ─────────────────────────
#
# Clients never send SQL. They describe what they want; the server validates
# every field/operation against a fixed registry and compiles the request into
# a single parameterised SELECT. Unknown fields, bad operations, or injection
# attempts in values can't reach the database as code — values are always bound.

# field_name → SQL expression. Dimensions are text columns from the joined
# lookup tables; measures are the numeric count columns on the fact table.
_DIMENSIONS: dict[str, str] = {
    "period_label": "p.label",
    "country_code": "c.code",
    "country_name": "c.name",
    "requestor_name": "rq.name",
    "product_name": "pr.name",
    "reason_name": "rn.name",
}
_MEASURES: dict[str, str] = {
    "num_requests": "r.num_requests",
    "items_requested": "r.items_requested",
    "removed_legal": "r.removed_legal",
    "removed_policy": "r.removed_policy",
    "not_found": "r.not_found",
    "not_enough_info": "r.not_enough_info",
    "no_action": "r.no_action",
    "already_removed": "r.already_removed",
}
_ALL_FIELDS: dict[str, str] = {**_DIMENSIONS, **_MEASURES}

_FROM = (
    "FROM removals r "
    "JOIN periods p ON p.id = r.period_id "
    "JOIN countries c ON c.id = r.country_id "
    "JOIN requestors rq ON rq.id = r.requestor_id "
    "JOIN products pr ON pr.id = r.product_id "
    "JOIN reasons rn ON rn.id = r.reason_id"
)

# operation → SQL comparator (numeric fields only)
_COMPARATORS = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
Operation = Literal["EQ", "IN", "GT", "GTE", "LT", "LTE"]
AggFunction = Literal["SUM", "COUNT", "AVG", "MIN", "MAX"]
SortOrder = Literal["asc", "desc"]


class Condition(BaseModel):
    """A single filter, e.g. {operation: IN, field_name: country_code, field_values: [DE, FR]}."""

    operation: Operation = Field(..., description="EQ, IN, GT, GTE, LT, LTE.")
    field_name: str = Field(..., description="A queryable field; see GET /fields.")
    field_values: list[str | int | float] = Field(
        ..., min_length=1, description="One or more values; always bound as parameters."
    )


class BooleanQuery(BaseModel):
    """Boolean combination of conditions, matching the TikTok Research API shape."""

    model_config = ConfigDict(populate_by_name=True)

    and_: list[Condition] = Field(default_factory=list, alias="and")
    or_: list[Condition] = Field(default_factory=list, alias="or")
    not_: list[Condition] = Field(default_factory=list, alias="not")


class Aggregate(BaseModel):
    """An aggregate column, e.g. {function: SUM, field_name: items_requested, alias: items}."""

    function: AggFunction
    field_name: str = Field(default="*", description="A measure field, or '*' for COUNT.")
    alias: str = Field(..., description="Output column name (letters, digits, underscore).")


class Sort(BaseModel):
    field_name: str = Field(..., description="An output column (a group_by field or aggregate alias).")
    order: SortOrder = "desc"


class QueryRequest(BaseModel):
    """Structured query. No SQL is accepted."""

    query: BooleanQuery = Field(default_factory=BooleanQuery, description="Filters.")
    fields: list[str] | None = Field(
        default=None,
        description="Columns to return for a raw (non-aggregated) query. Defaults to all fields.",
    )
    group_by: list[str] = Field(default_factory=list, description="Dimension fields to group by.")
    aggregates: list[Aggregate] = Field(default_factory=list, description="Aggregate columns.")
    sort: list[Sort] = Field(default_factory=list, description="Result ordering.")
    max_count: int = Field(default=100, ge=1, description="Row limit (capped at ROW_LIMIT).")


class QueryCompileError(ValueError):
    """Raised when a structured query is invalid; surfaced to the client as 400."""


def _require_number(value: Any, field_name: str) -> None:
    # bool is an int subclass — reject it explicitly so true/false isn't treated as 1/0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QueryCompileError(f"Field '{field_name}' requires numeric values.")


def _require_string(value: Any, field_name: str) -> None:
    # Dimensions are TEXT columns; a numeric value would silently match nothing
    # under SQLite type affinity rules, so reject it up front.
    if not isinstance(value, str):
        raise QueryCompileError(f"Field '{field_name}' requires string values.")


def _compile_condition(cond: Condition) -> tuple[str, list[Any]]:
    field = cond.field_name
    if field not in _ALL_FIELDS:
        raise QueryCompileError(f"Unknown field '{field}'. See GET /fields.")
    col = _ALL_FIELDS[field]
    is_measure = field in _MEASURES
    op = cond.operation
    values = cond.field_values

    if op in _COMPARATORS:
        if not is_measure:
            raise QueryCompileError(f"Operation {op} is only valid on numeric fields, not '{field}'.")
        if len(values) != 1:
            raise QueryCompileError(f"Operation {op} takes exactly one value.")
        _require_number(values[0], field)
        return f"{col} {_COMPARATORS[op]} ?", [values[0]]

    if op == "EQ":
        if len(values) != 1:
            raise QueryCompileError("Operation EQ takes exactly one value; use IN for multiple.")
        if is_measure:
            _require_number(values[0], field)
        else:
            _require_string(values[0], field)
        return f"{col} = ?", [values[0]]

    if op == "IN":
        for v in values:
            if is_measure:
                _require_number(v, field)
            else:
                _require_string(v, field)
        placeholders = ", ".join(["?"] * len(values))
        return f"{col} IN ({placeholders})", list(values)

    raise QueryCompileError(f"Unsupported operation '{op}'.")  # pragma: no cover


def _compile_where(q: BooleanQuery) -> tuple[str, list[Any]]:
    groups: list[str] = []
    params: list[Any] = []

    def _and(conditions: list[Condition]) -> str:
        frags = []
        for c in conditions:
            frag, p = _compile_condition(c)
            frags.append(frag)
            params.extend(p)
        return " AND ".join(frags)

    if q.and_:
        groups.append(_and(q.and_))
    if q.or_:
        frags = []
        for c in q.or_:
            frag, p = _compile_condition(c)
            frags.append(frag)
            params.extend(p)
        groups.append("(" + " OR ".join(frags) + ")")
    if q.not_:
        frags = []
        for c in q.not_:
            frag, p = _compile_condition(c)
            frags.append(f"NOT ({frag})")
            params.extend(p)
        groups.append(" AND ".join(frags))

    return " AND ".join(g for g in groups if g), params


def _safe_alias(alias: str) -> str:
    if not alias or not all(ch.isalnum() or ch == "_" for ch in alias):
        raise QueryCompileError(f"Invalid alias '{alias}'. Use letters, digits, and underscores.")
    return alias


def compile_query(req: QueryRequest) -> tuple[str, list[Any], list[str]]:
    """Validate a structured query and compile it to (sql, params, output_columns)."""
    where, params = _compile_where(req.query)
    aggregating = bool(req.aggregates) or bool(req.group_by)

    if aggregating and req.fields:
        raise QueryCompileError("`fields` cannot be combined with `group_by`/`aggregates`.")

    select_parts: list[str] = []
    columns: list[str] = []
    col_expr: dict[str, str] = {}  # output column name → expression (for ORDER BY)

    if aggregating:
        for gb in req.group_by:
            if gb not in _DIMENSIONS:
                raise QueryCompileError(f"group_by field '{gb}' must be a dimension. See GET /fields.")
            if gb in col_expr:
                raise QueryCompileError(f"Duplicate group_by field '{gb}'.")
            expr = _DIMENSIONS[gb]
            select_parts.append(f"{expr} AS {gb}")
            columns.append(gb)
            col_expr[gb] = expr
        for agg in req.aggregates:
            alias = _safe_alias(agg.alias)
            if alias in col_expr:
                raise QueryCompileError(f"Duplicate or clashing output column '{alias}'.")
            if agg.function == "COUNT" and agg.field_name in ("*", ""):
                expr = "COUNT(*)"
            elif agg.field_name not in _MEASURES:
                raise QueryCompileError(
                    f"Aggregate field '{agg.field_name}' must be a numeric measure. See GET /fields."
                )
            else:
                expr = f"{agg.function}({_MEASURES[agg.field_name]})"
            select_parts.append(f"{expr} AS {alias}")
            columns.append(alias)
            col_expr[alias] = expr
    else:
        fields = req.fields if req.fields is not None else list(_ALL_FIELDS)
        if not fields:
            raise QueryCompileError("`fields` must name at least one column.")
        for f in fields:
            if f not in _ALL_FIELDS:
                raise QueryCompileError(f"Unknown field '{f}'. See GET /fields.")
            if f in col_expr:
                raise QueryCompileError(f"Duplicate field '{f}' in fields list.")
            expr = _ALL_FIELDS[f]
            select_parts.append(f"{expr} AS {f}")
            columns.append(f)
            col_expr[f] = expr

    order_parts = []
    for s in req.sort:
        if s.field_name not in col_expr:
            raise QueryCompileError(
                f"Cannot sort by '{s.field_name}'; it is not a selected output column."
            )
        order_parts.append(f"{col_expr[s.field_name]} {'DESC' if s.order == 'desc' else 'ASC'}")

    limit = min(req.max_count, ROW_LIMIT)

    sql = f"SELECT {', '.join(select_parts)} {_FROM}"
    if where:
        sql += f" WHERE {where}"
    if req.group_by:
        sql += " GROUP BY " + ", ".join(_DIMENSIONS[g] for g in req.group_by)
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += f" LIMIT {limit}"

    return sql, params, columns


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
            cur = conn.execute(job.sql, job.params)
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
        "name": "Structured Query Demo API",
        "pattern": "async-job",
        "query_style": "TikTok-Research-API-style structured parameters (no SQL accepted)",
        "auth": "X-API-Key header required for all endpoints except `/`, `/docs`, `/openapi.json`",
        "endpoints": {
            "POST /query": "Submit a structured query, returns 202 + job_id",
            "GET /jobs": "List your jobs",
            "GET /jobs/{job_id}": "Job status (your jobs only)",
            "GET /jobs/{job_id}/result?format=json|csv": "Result (only when status=done)",
            "GET /jobs/{job_id}/download?...": "Secure result download via a signed, expiring URL (no key)",
            "DELETE /jobs/{job_id}": "Cancel a queued/running job, or remove a finished one",
            "GET /fields": "List queryable fields and operations",
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


@app.get("/fields")
def list_fields(_: dict = Depends(require_api_key)) -> dict[str, Any]:
    """Document the queryable fields and the operations each one supports."""
    return {
        "dimensions": {
            "fields": sorted(_DIMENSIONS),
            "operations": ["EQ", "IN"],
            "usable_in": ["query", "fields", "group_by", "sort"],
            "note": "Text fields from the lookup tables.",
        },
        "measures": {
            "fields": sorted(_MEASURES),
            "operations": ["EQ", "IN", "GT", "GTE", "LT", "LTE"],
            "usable_in": ["query", "fields", "aggregates"],
            "note": "Numeric count columns on the removals fact table.",
        },
        "aggregate_functions": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
        "example": {
            "query": {
                "and": [
                    {"operation": "IN", "field_name": "country_code", "field_values": ["DE", "FR"]},
                    {"operation": "EQ", "field_name": "reason_name", "field_values": ["Defamation"]},
                ]
            },
            "group_by": ["product_name"],
            "aggregates": [
                {"function": "SUM", "field_name": "num_requests", "alias": "requests"}
            ],
            "sort": [{"field_name": "requests", "order": "desc"}],
            "max_count": 10,
        },
    }


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
    try:
        sql, params, _columns = compile_query(body)
    except QueryCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job = Job(
        id=uuid.uuid4().hex,
        sql=sql,
        params=params,
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


def _render_result(
    job_id: str, fmt: str, *, as_attachment: bool
) -> JSONResponse | PlainTextResponse:
    """Fetch a done job's result and render it as JSON or CSV (404 if it's gone)."""
    result = _store.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found (may have expired).")
    cols, rows = result

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        writer.writerows(rows)
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.csv"'},
        )

    headers = (
        {"Content-Disposition": f'attachment; filename="{job_id}.json"'} if as_attachment else None
    )
    return JSONResponse(
        {"columns": cols, "rows": rows, "row_count": len(rows)}, headers=headers
    )


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
    return _render_result(job_id, format, as_attachment=False)


@app.get("/jobs/{job_id}/download", response_model=None)
def download_job_result(
    job_id: str,
    expires: int,
    sig: str,
    format: Literal["json", "csv"] = "json",
) -> JSONResponse | PlainTextResponse:
    """Secure download via a signed, expiring URL — no API key needed.

    The link is a capability: the HMAC signature authorises this exact job +
    format + expiry, so possession of a valid, unexpired URL is sufficient.
    """
    job = _store.get(job_id)
    # Bind the signature to the job's owner; if the job is gone, there's nothing
    # to verify against, so treat it as a 404 either way.
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not _verify_download_signature(job_id, job.owner_key, format, expires, sig):
        raise HTTPException(status_code=403, detail="Invalid download signature.")
    if expires < int(time.time()):
        raise HTTPException(status_code=410, detail="Download link has expired.")

    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job not ready (status={job.status}).",
        )
    return _render_result(job_id, format, as_attachment=True)


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
