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
    QUERY_RATE_MAX_PER_WINDOW  max POST /query submissions per API key per window (default: 60)
    QUERY_RATE_WINDOW_SECONDS  the query rate-limit window in seconds (default: 60)
    LOG_LEVEL             log level for the api_demo logger (default: INFO)
    LOG_FORMAT            json (default) for structured logs, or text for human-readable
    PUBLIC_BASE_URL       base URL used to make callback payload links absolute (default: relative)
    CALLBACK_TIMEOUT_SECONDS  per-attempt webhook timeout (default: 10)
    CALLBACK_MAX_ATTEMPTS     webhook delivery attempts before giving up (default: 3)
    CALLBACK_WORKERS          size of the bounded webhook delivery pool (default: 8)
    CALLBACK_ALLOW_PRIVATE    allow callbacks to private/loopback hosts — dev only (default: off)
"""
import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# ── Config ────────────────────────────────────────────────────────────────────

HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "demo.db"))
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
# Researcher-portal issued keys: how long they last, and how many a single
# client/email may mint within a rolling window.
ISSUED_KEY_TTL = int(os.getenv("ISSUED_KEY_TTL_SECONDS", str(30 * 24 * 3600)))
REGISTER_MAX_PER_WINDOW = int(os.getenv("PORTAL_REGISTER_MAX_PER_WINDOW", "10"))
REGISTER_WINDOW = int(os.getenv("PORTAL_REGISTER_WINDOW_SECONDS", "3600"))
# Google sign-in (FedCM via Google Identity Services). GOOGLE_CLIENT_ID is your
# OAuth 2.0 Web client ID (the `aud` we verify ID tokens against). ADMIN_EMAILS is
# a comma-separated allowlist of accounts that are implicitly approved and may
# approve/revoke other researchers. A successful login mints a first-party session
# key into the issued-key store, living GOOGLE_SESSION_TTL seconds.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
ADMIN_EMAILS = frozenset(
    e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
)
GOOGLE_SESSION_TTL = int(os.getenv("GOOGLE_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
# Demo auth (hardcoded alice/bob keys + the open /portal/register flow). Handy for
# local dev; set ALLOW_DEMO_KEYS=0 in production so only Google sign-in works.
ALLOW_DEMO_KEYS = os.getenv("ALLOW_DEMO_KEYS", "1").lower() in ("1", "true", "yes")
# Deployed build identifier — the CD workflow injects the commit SHA as APP_VERSION
# on each Cloud Run revision; defaults to "dev" locally. Surfaced at GET /version
# and in the X-Version response header so you can confirm what's actually live.
APP_VERSION = os.getenv("APP_VERSION") or "dev"
# Combined-site layout: the dashboard is served at "/", and the JSON API lives
# under this prefix on the same origin (no CORS). Operational endpoints
# (/healthz, /readyz, /metrics, /version) and pages (/portal) stay at the root.
API_PREFIX = "/api"
api_router = APIRouter()
# Only honour X-Forwarded-For for the client IP when behind a trusted proxy that
# overwrites it. Off by default: trusting it unconditionally would let any client
# spoof the header to dodge the registration rate limit.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
# Per-API-key throttle on query submission (the expensive, job-spawning path).
QUERY_RATE_MAX = int(os.getenv("QUERY_RATE_MAX_PER_WINDOW", "60"))
QUERY_RATE_WINDOW = int(os.getenv("QUERY_RATE_WINDOW_SECONDS", "60"))
# Public interactive query path (POST /api/explore, no auth): runs the same
# validated structured query inline, but hard-caps rows and is IP-rate-limited so
# the unauthenticated endpoint can't be abused.
EXPLORE_MAX_ROWS = int(os.getenv("EXPLORE_MAX_ROWS", "500"))
EXPLORE_RATE_MAX = int(os.getenv("EXPLORE_RATE_MAX_PER_WINDOW", "60"))
EXPLORE_RATE_WINDOW = int(os.getenv("EXPLORE_RATE_WINDOW_SECONDS", "60"))
# Natural-language → structured-query (POST /api/ask). An LLM (Claude) translates
# a question into the validated QueryRequest model — never SQL — which then runs
# through the same compile_query path as /api/explore. Off unless ANTHROPIC_API_KEY
# is set (LLM calls cost money); IP-rate-limited more tightly than /api/explore.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
NL_QUERY_ENABLED = bool(os.getenv("ANTHROPIC_API_KEY"))
ASK_RATE_MAX = int(os.getenv("ASK_RATE_MAX_PER_WINDOW", "10"))
ASK_RATE_WINDOW = int(os.getenv("ASK_RATE_WINDOW_SECONDS", "60"))
# Cache translated questions in-process so a repeated question skips the LLM call.
ASK_CACHE_SIZE = int(os.getenv("ASK_CACHE_SIZE", "256"))
# Structured logging: JSON lines by default, or LOG_FORMAT=text for humans.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()
# Webhook callbacks (optional callback_url on POST /query). Absolute links in the
# payload need PUBLIC_BASE_URL; delivery retries with backoff; SSRF guard blocks
# private/loopback/link-local targets unless CALLBACK_ALLOW_PRIVATE is set.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
CALLBACK_TIMEOUT = int(os.getenv("CALLBACK_TIMEOUT_SECONDS", "10"))
CALLBACK_MAX_ATTEMPTS = int(os.getenv("CALLBACK_MAX_ATTEMPTS", "3"))
CALLBACK_WORKERS = int(os.getenv("CALLBACK_WORKERS", "8"))
CALLBACK_ALLOW_PRIVATE = os.getenv("CALLBACK_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


# ── Structured logging ──────────────────────────────────────────────────────────
#
# One JSON object per line (event + context), so logs are greppable in dev and
# ingestible by a log pipeline in prod. Pass structured fields via
# `logger.info("event_name", extra={"data": {...}})`. Secrets (API keys) are
# never logged. Set LOG_FORMAT=text for plain human-readable lines.


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    if LOG_FORMAT == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("api_demo")
    log.handlers[:] = [handler]
    try:
        log.setLevel(LOG_LEVEL)
    except ValueError:  # unknown LOG_LEVEL — don't crash startup
        log.setLevel(logging.INFO)
    log.propagate = False  # don't double-emit through uvicorn's root handler
    return log


logger = _configure_logging()


# ── Prometheus metrics ──────────────────────────────────────────────────────────
#
# Exposed at GET /metrics (no auth — scrape it over an internal network). Request
# metrics are labelled by the matched *route template* (e.g. "/jobs/{job_id}"),
# not the raw path, so job ids don't blow up label cardinality.

HTTP_REQUESTS = Counter(
    "api_demo_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "api_demo_http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
JOBS_IN_FLIGHT = Gauge("api_demo_jobs_in_flight", "Jobs currently executing")
JOBS_TOTAL = Counter("api_demo_jobs_total", "Jobs by terminal status", ["status"])
JOB_QUEUE_DEPTH = Gauge("api_demo_job_queue_depth", "Queued jobs not yet started")
CALLBACKS_TOTAL = Counter("api_demo_callbacks_total", "Webhook callback deliveries", ["result"])


def _load_api_keys() -> dict[str, dict[str, str]]:
    raw = os.getenv("API_KEYS_JSON")
    if raw:
        return json.loads(raw)
    if ALLOW_DEMO_KEYS:
        # Demo fallback — disabled when ALLOW_DEMO_KEYS=0 (production).
        return {"alice": {"name": "alice"}, "bob": {"name": "bob"}}
    return {}


API_KEYS = _load_api_keys()


def _lookup_principal(key: str) -> dict[str, str] | None:
    """Resolve a key to its principal: a configured key, an issued portal key, or
    a Google session. Google sessions are re-checked against the registration on
    every use, so an admin revoke takes effect immediately (not just at TTL)."""
    if key in API_KEYS:
        return API_KEYS[key]
    rec = _key_store.get(key)  # None if unknown or expired
    if rec is None:
        return None
    if rec.get("auth") == "google":
        email = rec.get("email")
        if not email:
            return None
        reg = _registrations.get(email)
        if reg is None or reg.get("status") != "approved":
            return None
    return rec


# ── Auth ──────────────────────────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Depends(api_key_header)) -> dict[str, str]:
    principal = _lookup_principal(key) if key else None
    if principal is None or key is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key. Set header `X-API-Key: <key>`.",
        )
    return {"key": key, **principal}


def require_admin(principal: dict = Depends(require_api_key)) -> dict[str, str]:
    """Admin = an authenticated principal whose email is in ADMIN_EMAILS."""
    if str(principal.get("email", "")).lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return principal


# ── Secure download URLs ──────────────────────────────────────────────────────
#
# A finished job exposes a capability-style download URL: a signed, expiring link
# that streams the result without an API key (like an S3 presigned URL). The
# signature binds the job id, owner, format, and expiry with HMAC-SHA256, so the
# link can't be tampered with or repointed at another job, and stops working
# once it expires.

DOWNLOAD_FORMATS = ("json", "csv")


def _download_signature(job_id: str, fmt: str, expires: int) -> str:
    # The job_id is an unguessable UUID, so it alone scopes the link; no need to
    # mix in owner_key (which would force a store lookup before we could verify).
    msg = f"{job_id}:{fmt}:{expires}".encode()
    return hmac.new(DOWNLOAD_URL_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _make_download_url(job_id: str, fmt: str) -> str:
    expires = int(time.time()) + DOWNLOAD_URL_TTL
    sig = _download_signature(job_id, fmt, expires)
    return f"{API_PREFIX}/jobs/{job_id}/download?format={fmt}&expires={expires}&sig={sig}"


def _verify_download_signature(job_id: str, fmt: str, expires: int, sig: str) -> bool:
    expected = _download_signature(job_id, fmt, expires)
    return hmac.compare_digest(expected, sig)


class CallbackUrlError(ValueError):
    """Raised when a webhook callback_url is malformed or points somewhere unsafe."""


def _validate_callback_url(url: str) -> None:
    """Reject callback URLs that aren't plain http(s) to a routable public host.

    A caller-supplied URL that the server then fetches is a classic SSRF vector:
    without this guard it could be aimed at cloud metadata (169.254.169.254),
    localhost, or other internal services. We resolve the host and reject any
    private/loopback/link-local/reserved address. Set CALLBACK_ALLOW_PRIVATE=1 to
    bypass for local development. This is re-checked just before delivery, so DNS
    rebinding between submit and send is also caught.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CallbackUrlError("callback_url must be an http(s) URL.")
    host = parsed.hostname
    if not host:
        raise CallbackUrlError("callback_url has no host.")
    if CALLBACK_ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise CallbackUrlError(f"callback_url host did not resolve: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # IPv4-mapped (::ffff:127.0.0.1) and 6to4 IPv6 addresses report
        # is_private/is_loopback=False even when they embed a private IPv4 — unwrap
        # to the embedded v4 so the guard can't be bypassed through them.
        if isinstance(ip, ipaddress.IPv6Address):
            ip = ip.ipv4_mapped or ip.sixtofour or ip
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise CallbackUrlError("callback_url resolves to a non-public address.")


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
    callback_url: str | None = None  # optional webhook notified on terminal state
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
            "status_url": f"{API_PREFIX}/jobs/{self.id}",
            "result_url": f"{API_PREFIX}/jobs/{self.id}/result" if self.status == "done" else None,
            # Signed, expiring links that download the result without an API key.
            "download_urls": (
                {fmt: _make_download_url(self.id, fmt) for fmt in DOWNLOAD_FORMATS}
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
            "callback_url": job.callback_url or "",
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
            callback_url=h.get("callback_url") or None,
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


def _make_redis_client() -> Any | None:
    """Build a redis-py / upstash-redis client from env, or None for in-memory mode."""
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        from upstash_redis import Redis as UpstashRedis
        return UpstashRedis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    if REDIS_URL:
        import redis
        return redis.from_url(REDIS_URL, decode_responses=True)
    return None


# ── Issued-key store (researcher portal) ───────────────────────────────────────
#
# Keys minted by /portal/register, with expiry and a registration rate limiter.
# Backed by the same Redis client as the job store when configured, so issued
# keys survive restarts and are shared across workers; otherwise in-memory.


class MemoryKeyStore:
    """Single-process store for issued keys + rate-limit counters."""

    def __init__(self) -> None:
        self._recs: dict[str, dict[str, Any]] = {}
        self._exp: dict[str, float] = {}
        self._hits: dict[str, list[float]] = {}
        self._last_sweep = 0.0
        self._lock = threading.Lock()

    def put(self, key: str, record: dict[str, Any], ttl: int) -> None:
        with self._lock:
            self._recs[key] = record
            self._exp[key] = time.time() + ttl

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            exp = self._exp.get(key)
            if exp is None:
                return None
            if exp < time.time():  # expired — drop it
                self._recs.pop(key, None)
                self._exp.pop(key, None)
                return None
            return self._recs.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            self._exp.pop(key, None)
            return self._recs.pop(key, None) is not None

    def incr(self, bucket: str, window: int) -> int:
        """Count hits for `bucket` within the trailing `window` seconds."""
        with self._lock:
            now = time.time()
            cutoff = now - window
            # Lazily drop fully-stale buckets (at most once per window) so the
            # dict doesn't grow unbounded with one entry per IP/email ever seen.
            if now - self._last_sweep > window:
                self._hits = {b: ts for b, ts in self._hits.items() if ts and ts[-1] > cutoff}
                self._last_sweep = now
            hits = [t for t in self._hits.get(bucket, []) if t > cutoff]
            hits.append(now)
            self._hits[bucket] = hits
            return len(hits)


class RedisKeyStore:
    """Redis-backed issued-key store; expiry via key TTL, rate limit via INCR+EXPIRE."""

    def __init__(self, client: Any) -> None:
        self._r = client

    def put(self, key: str, record: dict[str, Any], ttl: int) -> None:
        self._r.set(f"issued_key:{key}", json.dumps(record), ex=ttl)

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._r.get(f"issued_key:{key}")
        return json.loads(raw) if raw else None

    def delete(self, key: str) -> bool:
        return bool(self._r.delete(f"issued_key:{key}"))

    def incr(self, bucket: str, window: int) -> int:
        rk = f"reg_rate:{bucket}"
        n = int(self._r.incr(rk))
        if n == 1:
            self._r.expire(rk, window)
        return n


# Researcher registrations (Google sign-in + admin approval). Durable approval
# state keyed by lowercased email — NOT expiring like session keys. Listable so an
# admin can see who's pending.


class MemoryRegistrationStore:
    """Single-process registration store."""

    def __init__(self) -> None:
        self._recs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def upsert(self, email: str, record: dict[str, Any]) -> None:
        # Copy on the way in/out so callers can't alias and mutate stored state
        # (the Redis backend serialises, giving the same isolation for free).
        with self._lock:
            self._recs[email] = record.copy()

    def get(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._recs.get(email)
            return rec.copy() if rec is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [rec.copy() for rec in self._recs.values()]

    def delete(self, email: str) -> bool:
        with self._lock:
            return self._recs.pop(email, None) is not None


class RedisRegistrationStore:
    """Redis-backed registrations; an index set makes them listable."""

    def __init__(self, client: Any) -> None:
        self._r = client

    def upsert(self, email: str, record: dict[str, Any]) -> None:
        self._r.set(f"reg:{email}", json.dumps(record))
        self._r.sadd("reg:index", email)

    def get(self, email: str) -> dict[str, Any] | None:
        raw = self._r.get(f"reg:{email}")
        return json.loads(raw) if raw else None

    def list(self) -> list[dict[str, Any]]:
        emails = [e.decode() if isinstance(e, bytes) else e for e in self._r.smembers("reg:index")]
        if not emails:
            return []
        # Single round-trip rather than one GET per registration.
        raw_recs = self._r.mget([f"reg:{email}" for email in emails])
        return [json.loads(raw) for raw in raw_recs if raw]

    def delete(self, email: str) -> bool:
        self._r.srem("reg:index", email)
        return bool(self._r.delete(f"reg:{email}"))


def _make_store() -> MemoryJobStore | RedisJobStore:
    return RedisJobStore(_redis) if _redis is not None else MemoryJobStore()


def _make_key_store() -> MemoryKeyStore | RedisKeyStore:
    return RedisKeyStore(_redis) if _redis is not None else MemoryKeyStore()


def _make_registration_store() -> MemoryRegistrationStore | RedisRegistrationStore:
    return RedisRegistrationStore(_redis) if _redis is not None else MemoryRegistrationStore()


_redis = _make_redis_client()
_store: MemoryJobStore | RedisJobStore = _make_store()
_key_store: MemoryKeyStore | RedisKeyStore = _make_key_store()
_registrations: MemoryRegistrationStore | RedisRegistrationStore = _make_registration_store()
_executor = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="sql-worker")
# Webhook delivery runs on its own bounded pool so a flood of callbacks (or slow
# receivers during retry backoff) can't exhaust threads or starve query workers.
_callback_executor = ThreadPoolExecutor(max_workers=CALLBACK_WORKERS, thread_name_prefix="callback-worker")

app = FastAPI(
    title="DSA VLOP Transparency Query API (async jobs)",
    description=(
        "Query the aggregated EU Digital Services Act VLOP transparency reports "
        "(tables 3–11) with structured parameters (no SQL). Pick a `table` "
        "(GET /tables), describe filters/group_by/aggregates, get a job id, then "
        "poll for results as JSON or CSV. Query syntax follows the TikTok Research "
        "API: boolean and/or/not clauses of {operation, field_name, field_values}."
    ),
    version="0.5.0",
)


def _cors_origins() -> list[str]:
    """Allowed browser origins for cross-origin API calls, from ALLOWED_ORIGINS
    (comma-separated). Empty by default: the bundled portal is same-origin, so no
    CORS headers are emitted unless a separate frontend origin is configured."""
    return [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]


_CORS_ORIGINS = _cors_origins()
if _CORS_ORIGINS:
    # Auth is via the X-API-Key header (no cookies), so credentials aren't needed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emit one structured log line + Prometheus metrics per request."""
    start = time.perf_counter()
    request_id = secrets.token_hex(8)
    request.state.request_id = request_id
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        logger.exception(
            "request_error",
            extra={"data": {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            }},
        )
        raise
    finally:
        # Label by route template ("/jobs/{job_id}") to bound cardinality; bare
        # 404s (no matched route) collapse into "unmatched".
        route = request.scope.get("route")
        path_label = getattr(route, "path", None) or "unmatched"
        elapsed = time.perf_counter() - start
        HTTP_REQUESTS.labels(request.method, path_label, str(status)).inc()
        HTTP_LATENCY.labels(request.method, path_label).observe(elapsed)
    logger.info(
        "request",
        extra={"data": {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": round(elapsed * 1000, 2),
        }},
    )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Version"] = APP_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ── Structured query model (TikTok-Research-API-style) ─────────────────────────
#
# Clients never send SQL. They describe what they want; the server validates
# every field/operation against a fixed registry and compiles the request into
# a single parameterised SELECT. Unknown fields, bad operations, or injection
# attempts in values can't reach the database as code — values are always bound.

# The dataset is the aggregated EU DSA VLOP transparency data — one queryable
# table per DSA report table (t3–t11). A query names a `table`; that table's
# TableSpec fixes the FROM/joins and the registry of dimension (text, EQ/IN) and
# measure (numeric) fields. field_name → SQL expression is the per-table trust
# boundary: only registered fields reach SQL, and every value is bound with ?.


@dataclass(frozen=True)
class TableSpec:
    description: str
    from_sql: str
    dimensions: dict[str, str]  # field_name → SQL expr (text; EQ/IN)
    measures: dict[str, str]    # field_name → SQL expr (numeric; EQ/IN/GT/GTE/LT/LTE, aggregates)

    @property
    def all_fields(self) -> dict[str, str]:
        return {**self.dimensions, **self.measures}


# Dimensions common to every report table (every fact joins `services s`).
_SVC = {"service_name": "s.name", "platform": "s.platform"}
# The 16 own-initiative restriction-type measures shared by t5 and t6.
_OWN_INIT_MEASURES = {
    "measures": "f.measures", "automated": "f.automated",
    "vis_removal": "f.vis_removal", "vis_disable": "f.vis_disable",
    "vis_demoted": "f.vis_demoted", "vis_age_restricted": "f.vis_age_restricted",
    "vis_interaction_restricted": "f.vis_interaction_restricted",
    "vis_labelled": "f.vis_labelled", "vis_other": "f.vis_other",
    "monetary_suspension": "f.monetary_suspension",
    "monetary_termination": "f.monetary_termination", "monetary_other": "f.monetary_other",
    "service_suspension": "f.service_suspension", "service_termination": "f.service_termination",
    "account_suspension": "f.account_suspension", "account_termination": "f.account_termination",
}
_J_SVC = "JOIN services s ON s.id = f.service_id"
_J_CAT = "JOIN categories c ON c.id = f.category_id"
_J_SEC = "JOIN sections se ON se.id = f.section_id"
_J_IND = "JOIN indicators i ON i.id = f.indicator_id"
_J_SCOPE = "JOIN scopes sc ON sc.id = f.scope_id"
_J_SURF = "JOIN surfaces su ON su.id = f.surface_id"
_CAT_DIMS = {"category_code": "c.code", "category_label": "c.label"}

TABLES: dict[str, TableSpec] = {
    "t3_member_state_orders": TableSpec(
        "Member-State orders to act on illegal content / to provide information (Art. 9 & 10), by category and scope.",
        f"FROM t3_member_state_orders f {_J_SVC} {_J_CAT} {_J_SCOPE}",
        {**_SVC, **_CAT_DIMS, "scope": "sc.name"},
        {"orders_to_act": "f.orders_to_act", "items": "f.items",
         "orders_to_provide_info": "f.orders_to_provide_info"},
    ),
    "t4_notices": TableSpec(
        "Notices submitted under Art. 16, by category, with Trusted-Flagger (tf_) breakdowns.",
        f"FROM t4_notices f {_J_SVC} {_J_CAT}",
        {**_SVC, **_CAT_DIMS},
        {"notices": "f.notices", "tf_notices": "f.tf_notices", "items": "f.items",
         "tf_items": "f.tf_items", "median_time": "f.median_time", "tf_median_time": "f.tf_median_time",
         "actions_law": "f.actions_law", "tf_actions_law": "f.tf_actions_law",
         "actions_tos": "f.actions_tos", "tf_actions_tos": "f.tf_actions_tos"},
    ),
    "t5_own_initiative_illegal": TableSpec(
        "Own-initiative actions on illegal content, by category × restriction type.",
        f"FROM t5_own_initiative_illegal f {_J_SVC} {_J_CAT}",
        {**_SVC, **_CAT_DIMS},
        dict(_OWN_INIT_MEASURES),
    ),
    "t6_own_initiative_tos": TableSpec(
        "Own-initiative actions on ToS violations, by category × restriction type × surface.",
        f"FROM t6_own_initiative_tos f {_J_SVC} {_J_CAT} {_J_SURF}",
        {**_SVC, **_CAT_DIMS, "surface": "su.name"},
        dict(_OWN_INIT_MEASURES),
    ),
    "t7_appeals_recidivism": TableSpec(
        "Appeals & recidivism (internal complaints, out-of-court disputes, repeat-offender suspensions), by section × indicator × scope × surface.",
        f"FROM t7_appeals_recidivism f {_J_SVC} {_J_SEC} {_J_IND} {_J_SCOPE} {_J_SURF}",
        {**_SVC, "section": "se.name", "indicator": "i.name", "scope": "sc.name", "surface": "su.name"},
        {"value": "f.value"},
    ),
    "t8_automated_means": TableSpec(
        "Use of automated means for content moderation, by section × indicator × scope × surface.",
        f"FROM t8_automated_means f {_J_SVC} {_J_SEC} {_J_IND} {_J_SCOPE} {_J_SURF}",
        {**_SVC, "section": "se.name", "indicator": "i.name", "scope": "sc.name", "surface": "su.name"},
        {"value": "f.value"},
    ),
    "t9_human_resources": TableSpec(
        "Human resources dedicated to content moderation, by section × indicator × scope.",
        f"FROM t9_human_resources f {_J_SVC} {_J_SEC} {_J_IND} {_J_SCOPE}",
        {**_SVC, "section": "se.name", "indicator": "i.name", "scope": "sc.name"},
        {"value": "f.value"},
    ),
    "t10_amar": TableSpec(
        "Average Monthly Active Recipients (AMAR) in the EU, by scope.",
        f"FROM t10_amar f {_J_SVC} {_J_SCOPE}",
        {**_SVC, "scope": "sc.name"},
        {"value": "f.value"},
    ),
    "t11_qualitative": TableSpec(
        "Qualitative description (free text), by indicator. No numeric measures — request `qualitative_text` in `fields`.",
        f"FROM t11_qualitative f {_J_SVC} {_J_IND}",
        {**_SVC, "indicator": "i.name", "qualitative_text": "f.value_text"},
        {},
    ),
}

# operation → SQL comparator (numeric fields only)
_COMPARATORS = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
Operation = Literal["EQ", "IN", "GT", "GTE", "LT", "LTE"]
AggFunction = Literal["SUM", "COUNT", "AVG", "MIN", "MAX"]
SortOrder = Literal["asc", "desc"]


class Condition(BaseModel):
    """A single filter, e.g. {operation: IN, field_name: service_name, field_values: [YouTube, TikTok]}."""

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
    """An aggregate column, e.g. {function: SUM, field_name: notices, alias: notices}."""

    function: AggFunction
    field_name: str = Field(default="*", description="A measure field, or '*' for COUNT.")
    alias: str = Field(..., description="Output column name (letters, digits, underscore).")


class Sort(BaseModel):
    field_name: str = Field(..., description="An output column (a group_by field or aggregate alias).")
    order: SortOrder = "desc"


class QueryRequest(BaseModel):
    """Structured query. No SQL is accepted."""

    table: str | None = Field(
        default=None, description="Which DSA report table to query (see GET /tables)."
    )
    query: BooleanQuery = Field(default_factory=BooleanQuery, description="Filters.")
    fields: list[str] | None = Field(
        default=None,
        description="Columns to return for a raw (non-aggregated) query. Defaults to all fields.",
    )
    group_by: list[str] = Field(default_factory=list, description="Dimension fields to group by.")
    aggregates: list[Aggregate] = Field(default_factory=list, description="Aggregate columns.")
    sort: list[Sort] = Field(default_factory=list, description="Result ordering.")
    max_count: int = Field(default=100, ge=1, description="Row limit (capped at ROW_LIMIT).")
    callback_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Optional http(s) webhook POSTed (HMAC-signed) when the job finishes.",
    )


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


def _compile_condition(cond: Condition, spec: TableSpec) -> tuple[str, list[Any]]:
    field = cond.field_name
    if field not in spec.all_fields:
        raise QueryCompileError(f"Unknown field '{field}' for this table. See GET /fields?table=…")
    col = spec.all_fields[field]
    is_measure = field in spec.measures
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


def _compile_where(q: BooleanQuery, spec: TableSpec) -> tuple[str, list[Any]]:
    groups: list[str] = []
    params: list[Any] = []

    def _and(conditions: list[Condition]) -> str:
        frags = []
        for c in conditions:
            frag, p = _compile_condition(c, spec)
            frags.append(frag)
            params.extend(p)
        return " AND ".join(frags)

    if q.and_:
        groups.append(_and(q.and_))
    if q.or_:
        frags = []
        for c in q.or_:
            frag, p = _compile_condition(c, spec)
            frags.append(frag)
            params.extend(p)
        groups.append("(" + " OR ".join(frags) + ")")
    if q.not_:
        frags = []
        for c in q.not_:
            frag, p = _compile_condition(c, spec)
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
    if not req.table:
        raise QueryCompileError(
            "`table` is required. Choose one of: " + ", ".join(TABLES) + ". See GET /tables."
        )
    spec = TABLES.get(req.table)
    if spec is None:
        raise QueryCompileError(f"Unknown table '{req.table}'. See GET /tables.")

    where, params = _compile_where(req.query, spec)
    aggregating = bool(req.aggregates) or bool(req.group_by)

    if aggregating and req.fields:
        raise QueryCompileError("`fields` cannot be combined with `group_by`/`aggregates`.")

    select_parts: list[str] = []
    columns: list[str] = []
    col_expr: dict[str, str] = {}  # output column name → expression (for ORDER BY)

    if aggregating:
        for gb in req.group_by:
            if gb not in spec.dimensions:
                raise QueryCompileError(f"group_by field '{gb}' must be a dimension of '{req.table}'. See GET /fields?table={req.table}")
            if gb in col_expr:
                raise QueryCompileError(f"Duplicate group_by field '{gb}'.")
            expr = spec.dimensions[gb]
            select_parts.append(f"{expr} AS {gb}")
            columns.append(gb)
            col_expr[gb] = expr
        for agg in req.aggregates:
            alias = _safe_alias(agg.alias)
            if alias in col_expr:
                raise QueryCompileError(f"Duplicate or clashing output column '{alias}'.")
            if agg.function == "COUNT" and agg.field_name in ("*", ""):
                expr = "COUNT(*)"
            elif agg.field_name not in spec.measures:
                raise QueryCompileError(
                    f"Aggregate field '{agg.field_name}' must be a numeric measure of '{req.table}'. See GET /fields?table={req.table}"
                )
            else:
                expr = f"{agg.function}({spec.measures[agg.field_name]})"
            select_parts.append(f"{expr} AS {alias}")
            columns.append(alias)
            col_expr[alias] = expr
    else:
        fields = req.fields if req.fields is not None else list(spec.all_fields)
        if not fields:
            raise QueryCompileError("`fields` must name at least one column.")
        for f in fields:
            if f not in spec.all_fields:
                raise QueryCompileError(f"Unknown field '{f}' for table '{req.table}'. See GET /fields?table={req.table}")
            if f in col_expr:
                raise QueryCompileError(f"Duplicate field '{f}' in fields list.")
            expr = spec.all_fields[f]
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

    sql = f"SELECT {', '.join(select_parts)} {spec.from_sql}"
    if where:
        sql += f" WHERE {where}"
    if req.group_by:
        sql += " GROUP BY " + ", ".join(spec.dimensions[g] for g in req.group_by)
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += f" LIMIT {limit}"

    return sql, params, columns


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Webhook callbacks ───────────────────────────────────────────────────────────

# Opener that refuses to follow redirects — a 3xx could otherwise bounce a
# validated public URL to an internal one, sidestepping the SSRF guard.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_callback_opener = urllib.request.build_opener(_NoRedirect)


def _absolutise(public: dict[str, Any]) -> dict[str, Any]:
    """Prefix relative status/result/download links with PUBLIC_BASE_URL (if set)."""
    if not PUBLIC_BASE_URL:
        return public
    for key in ("status_url", "result_url"):
        if public.get(key):
            public[key] = PUBLIC_BASE_URL + public[key]
    if public.get("download_urls"):
        public["download_urls"] = {k: PUBLIC_BASE_URL + v for k, v in public["download_urls"].items()}
    return public


def _send_callback(job_id: str, callback_url: str, payload: dict[str, Any]) -> None:
    """POST the job result to callback_url, HMAC-signed, with bounded retries."""
    body = json.dumps(payload).encode()
    signature = hmac.new(DOWNLOAD_URL_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={signature}",
        "X-Job-Id": job_id,
    }
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            _validate_callback_url(callback_url)  # re-check (narrows DNS rebinding) before each send
            req = urllib.request.Request(callback_url, data=body, headers=headers, method="POST")
            with _callback_opener.open(req, timeout=CALLBACK_TIMEOUT) as resp:
                status = resp.status  # the opener only returns for 2xx; 3xx/4xx/5xx raise HTTPError
            CALLBACKS_TOTAL.labels("delivered").inc()
            logger.info("callback_delivered", extra={"data": {
                "job_id": job_id, "status": status, "attempt": attempt}})
            return
        except CallbackUrlError as exc:
            CALLBACKS_TOTAL.labels("blocked").inc()
            logger.warning("callback_blocked", extra={"data": {"job_id": job_id, "error": str(exc)}})
            return  # don't retry a blocked target
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
        except Exception as exc:  # network error, timeout, …
            reason = f"{type(exc).__name__}: {exc}"
        if attempt < CALLBACK_MAX_ATTEMPTS:
            time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s, …
    CALLBACKS_TOTAL.labels("failed").inc()
    logger.warning("callback_failed", extra={"data": {
        "job_id": job_id, "attempts": CALLBACK_MAX_ATTEMPTS, "error": reason}})


def _dispatch_callback(job: Job) -> None:
    """Fire-and-forget callback delivery on the bounded callback pool (off query workers)."""
    if not job.callback_url or job.status not in ("done", "failed"):
        return
    payload = {"event": f"job.{job.status}", "job": _absolutise(job.to_public())}
    _callback_executor.submit(_send_callback, job.id, job.callback_url, payload)


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
    JOB_QUEUE_DEPTH.dec()  # dequeued — now running (or about to early-return)
    job = _store.get(job_id)
    if job is None or job.status == "cancelled":
        return

    _store.update_fields(job_id, status="running", started_at=_now())
    started = time.perf_counter()
    logger.info("job_started", extra={"data": {"job_id": job_id, "user": job.submitted_by}})

    def _elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    JOBS_IN_FLIGHT.inc()
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
        JOBS_TOTAL.labels("done").inc()
        logger.info(
            "job_done",
            extra={"data": {"job_id": job_id, "rows": len(rows), "duration_ms": _elapsed_ms()}},
        )

    except sqlite3.OperationalError as exc:
        refreshed = _store.get(job_id)
        if refreshed and refreshed.status != "cancelled":
            _store.update_fields(
                job_id, status="failed", error=f"SQL error: {exc}", finished_at=_now()
            )
            JOBS_TOTAL.labels("failed").inc()
            logger.warning(
                "job_failed",
                extra={"data": {"job_id": job_id, "error": f"SQL error: {exc}", "duration_ms": _elapsed_ms()}},
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
            JOBS_TOTAL.labels("failed").inc()
            # Unexpected failure — keep the traceback (JsonLogFormatter puts it in "exc").
            logger.exception(
                "job_failed",
                extra={"data": {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}", "duration_ms": _elapsed_ms()}},
            )
    finally:
        JOBS_IN_FLIGHT.dec()

    # Notify the caller's webhook, if any (done/failed only; skips cancelled).
    final = _store.get(job_id)
    if final is not None:
        _dispatch_callback(final)


def _job_for_owner(job_id: str, owner_key: str) -> Job:
    """Return the job or 404. Foreign job IDs always return 404 to avoid leaking existence."""
    job = _store.get(job_id)
    if job is None or job.owner_key != owner_key:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


# ── Endpoints ─────────────────────────────────────────────────────────────────

@api_router.get("")
def root() -> dict[str, Any]:
    meta = _dataset_meta()
    return {
        "name": "DSA VLOP Transparency Query API",
        "dataset": "EU Digital Services Act VLOP/VLOSE transparency reports (tables 3–11)",
        "period": meta.get("period"),
        "pattern": "async-job",
        "query_style": "TikTok-Research-API-style structured parameters (no SQL accepted); pick a `table` (GET /api/tables)",
        "auth": "X-API-Key header required for the job API; public: `/`, `/api`, `/api/overview`, `/api/explore`, `/docs`, `/openapi.json`",
        "endpoints": {
            "GET /": "Public VLOP transparency dashboard (web UI)",
            "GET /api/overview": "Public headline aggregates powering the dashboard (no auth)",
            "GET /api/explore/options": "Public: tables + their dimensions/measures for the query builder",
            "POST /api/explore": "Public: run a bounded structured query inline (no auth, row-capped, rate-limited)",
            "POST /api/ask": "Public: ask in natural language — an LLM writes the structured query (if enabled)",
            "GET /portal": "Researcher portal (web UI: sign in, get a key, browse the schema)",
            "POST /api/auth/google": "Sign in with a Google ID token (FedCM/GIS) → session key, or pending approval",
            "POST /api/portal/register": "Demo: issue an API key without auth (disabled when ALLOW_DEMO_KEYS=0)",
            "DELETE /api/portal/key": "Revoke your session / portal-issued key",
            "GET /api/admin/registrations": "Admin: list researcher registrations",
            "POST /api/admin/registrations/{email}/approve": "Admin: approve an account",
            "POST /api/admin/registrations/{email}/revoke": "Admin: revoke an account",
            "POST /api/query": "Submit a structured query over a `table` (optional callback_url webhook), returns 202 + job_id",
            "GET /api/jobs": "List your jobs",
            "GET /api/jobs/{job_id}": "Job status (your jobs only)",
            "GET /api/jobs/{job_id}/result?format=json|csv": "Result (only when status=done)",
            "GET /api/jobs/{job_id}/download?...": "Secure result download via a signed, expiring URL (no key)",
            "DELETE /api/jobs/{job_id}": "Cancel a queued/running job, or remove a finished one",
            "GET /api/tables": "List the queryable DSA report tables",
            "GET /api/fields?table=…": "Fields and operations for a table",
            "GET /api/schema/{table}": "Field registry for a report table",
            "GET /healthz": "Liveness probe",
            "GET /readyz": "Readiness probe (checks DB connection)",
            "GET /version": "Deployed build identifier (commit SHA)",
            "GET /metrics": "Prometheus metrics (no auth)",
            "GET /docs": "Interactive Swagger UI",
        },
        "tables": list(TABLES),
        "row_limit": ROW_LIMIT,
        "worker_threads": WORKER_THREADS,
        "store": "upstash" if UPSTASH_REDIS_REST_URL else ("redis" if REDIS_URL else "memory"),
        "auth_config": {
            "google_signin": bool(GOOGLE_CLIENT_ID),
            "google_client_id": GOOGLE_CLIENT_ID or None,
            "demo_keys": ALLOW_DEMO_KEYS,
        },
        "features": {"nl_query": NL_QUERY_ENABLED},
    }


# ── Researcher portal ─────────────────────────────────────────────────────────
#
# A tiny self-service portal: a researcher "signs in" with a name + email and is
# issued a working API key, then the page browses the schema. There is no real
# auth here — it's a demo of the onboarding flow. In production this would sit
# behind SSO and persist issued keys in a secret store / database.


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Researcher's name.")
    email: str = Field(..., min_length=3, max_length=254, description="Contact email.")


class GoogleAuthRequest(BaseModel):
    credential: str = Field(
        ..., min_length=1, max_length=8192,
        description="Google ID token (JWT) from Google Identity Services / FedCM.",
    )


def _verify_id_token(credential: str) -> dict[str, Any]:
    """Verify a Google ID token against GOOGLE_CLIENT_ID; return its claims.

    Raises if the signature/audience/issuer/expiry don't check out. Imported
    lazily so the dependency (and any network) is only touched when Google
    sign-in is actually used; tests monkeypatch this function."""
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests

    return google_id_token.verify_oauth2_token(
        credential, google_requests.Request(), GOOGLE_CLIENT_ID
    )


def _mint_session(email: str, name: str) -> tuple[str, str]:
    """Issue a first-party session key for an approved Google account."""
    key = "gs_" + secrets.token_hex(24)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=GOOGLE_SESSION_TTL)).isoformat()
    _key_store.put(
        key,
        {"name": name, "email": email, "auth": "google",
         "created_at": now.isoformat(), "expires_at": expires_at},
        GOOGLE_SESSION_TTL,
    )
    return key, expires_at


def _client_ip(request: Request) -> str:
    """The caller's IP. Honours X-Forwarded-For only when TRUST_PROXY_HEADERS is set
    (i.e. a trusted proxy that overwrites it) — otherwise it's client-spoofable.
    With `uvicorn --proxy-headers --forwarded-allow-ips=…`, request.client.host is
    already correct and this flag isn't needed."""
    if TRUST_PROXY_HEADERS:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Content-Security-Policy for the served HTML pages. Inline <script> blocks are
# allowlisted by sha256 hash (computed from the file, so never stale) rather than
# 'unsafe-inline', which keeps script injection locked down. Inline styles use
# 'unsafe-inline' (lower risk, and Chart.js/GSI set element styles). CSPs are
# cached per file since the static files don't change at runtime.
_csp_cache: dict[str, str] = {}


def _page_csp(html: str, *, script_hosts=(), connect_hosts=(), frame_hosts=(), img_hosts=()) -> str:
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
        for s in inline
    ]
    directives = [
        "default-src 'self'",
        " ".join(["script-src", "'self'", *hashes, *script_hosts]),
        "style-src 'self' 'unsafe-inline'",
        " ".join(["img-src", "'self'", "data:", *img_hosts]),
        " ".join(["connect-src", "'self'", *connect_hosts]),
        " ".join(["frame-src", *(frame_hosts or ["'none'"])]),
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


def _serve_page(filename: str, label: str, **csp_hosts) -> FileResponse:
    path = os.path.join(STATIC_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    csp = _csp_cache.get(filename)
    if csp is None:
        # Read raw bytes (not text mode) so the hash is over exactly what
        # FileResponse streams — text-mode newline translation (CRLF→LF) would
        # otherwise produce a hash that doesn't match the served bytes on a
        # CRLF checkout (Windows / git autocrlf), silently breaking the page.
        with open(path, "rb") as f:
            csp = _page_csp(f.read().decode("utf-8"), **csp_hosts)
        _csp_cache[filename] = csp
    return FileResponse(path, media_type="text/html", headers={"Content-Security-Policy": csp})


# Third-party assets we vendor and serve ourselves (filename → media type).
# Self-hosting Chart.js (instead of a CDN) keeps the dashboard working
# air-gapped and lets its CSP stay `script-src 'self'` with no third-party
# origin. Allowlisted by exact name so user input never builds a filesystem path.
_VENDOR_ASSETS = {"chart.umd.js": "text/javascript"}


@app.get("/static/vendor/{filename}")
def vendored_asset(filename: str) -> FileResponse:
    """Serve a vendored third-party asset (e.g. Chart.js) from static/vendor."""
    media_type = _VENDOR_ASSETS.get(filename)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Asset not found.")
    path = os.path.join(STATIC_DIR, "vendor", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Asset not found.")
    # Versioned, immutable content — let browsers/CDNs cache it for a year.
    return FileResponse(
        path, media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> FileResponse:
    """Serve the public VLOP transparency dashboard (reads GET /api/overview)."""
    # Chart.js is vendored same-origin (/static/vendor/chart.umd.js), so the CSP
    # needs no third-party script origin — `script-src 'self'` + inline hashes.
    return _serve_page("index.html", "Dashboard page")


@app.get("/portal", response_class=HTMLResponse)
def portal_page() -> FileResponse:
    """Serve the researcher portal single-page app."""
    # Portal loads Google Identity Services (script + sign-in iframe + avatar imgs).
    return _serve_page(
        "portal.html", "Portal page",
        script_hosts=["https://accounts.google.com"],
        connect_hosts=["https://accounts.google.com"],
        frame_hosts=["https://accounts.google.com"],
        img_hosts=["https://*.googleusercontent.com", "https://*.gstatic.com"],
    )


# The dashboard aggregates never change at runtime (the DB is opened mode=ro and
# baked into the image), so compute them once and memoise — this public endpoint
# then serves from memory instead of re-querying on every hit.
_overview_cache: dict[str, Any] | None = None
_overview_cache_lock = threading.Lock()


def _compute_overview() -> dict[str, Any]:
    conn = _connect_ro()
    try:
        meta = _dataset_meta()
        services = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        platforms = conn.execute("SELECT COUNT(DISTINCT platform) FROM services").fetchone()[0]
        total_notices = conn.execute("SELECT COALESCE(SUM(notices), 0) FROM t4_notices").fetchone()[0]
        top_platforms = [
            {"platform": p, "notices": n}
            for p, n in conn.execute(
                "SELECT s.platform, COALESCE(SUM(t.notices), 0) AS n "
                "FROM t4_notices t JOIN services s ON s.id = t.service_id "
                "GROUP BY s.platform ORDER BY n DESC LIMIT 10"
            ).fetchall()
        ]
        by_category = [
            {"category": c, "notices": n}
            for c, n in conn.execute(
                "SELECT cat.label, COALESCE(SUM(t.notices), 0) AS n "
                "FROM t4_notices t JOIN categories cat ON cat.id = t.category_id "
                "GROUP BY cat.label ORDER BY n DESC LIMIT 8"
            ).fetchall()
        ]
        return {
            "period": meta.get("period"),
            "generated": meta.get("generated"),
            "services": services,
            "platforms": platforms,
            "total_notices": total_notices,
            "top_platforms": top_platforms,
            "by_category": by_category,
        }
    finally:
        conn.close()


@api_router.get("/overview")
def overview() -> dict[str, Any]:
    """Public headline aggregates for the dashboard — no auth. Memoised: the
    read-only DB is static, so we compute the fixed queries once (no user input
    reaches SQL) and serve from memory thereafter."""
    global _overview_cache
    if _overview_cache is None:
        with _overview_cache_lock:
            if _overview_cache is None:
                _overview_cache = _compute_overview()
    return _overview_cache


@api_router.get("/explore/options")
def explore_options() -> dict[str, Any]:
    """Public metadata for the dashboard's query builder: each table's queryable
    dimensions and measures, from the fixed registry (no DB, no secrets)."""
    return {
        "tables": [
            {
                "table": name,
                "description": spec.description,
                "dimensions": list(spec.dimensions),
                "measures": list(spec.measures),
            }
            for name, spec in TABLES.items()
        ],
        "aggregates": ["SUM", "AVG", "MAX", "MIN", "COUNT"],
        "max_rows": EXPLORE_MAX_ROWS,
    }


class AskRequest(BaseModel):
    question: str = Field(
        ..., min_length=1, max_length=500,
        description="A natural-language question about the DSA VLOP data.",
    )


# JSON schema the LLM must fill — a constrained, flat projection of QueryRequest.
# Strict so structured outputs reliably return a valid object; compile_query still
# does the real validation against the table registry afterward.
_ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "table": {"type": "string", "enum": list(TABLES)},
        "filters": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "field": {"type": "string"},
                    "op": {"type": "string", "enum": ["EQ", "IN", "GT", "GTE", "LT", "LTE"]},
                    "values": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["field", "op", "values"],
            },
        },
        "group_by": {"type": "array", "items": {"type": "string"}},
        "aggregates": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "function": {"type": "string", "enum": ["SUM", "AVG", "MIN", "MAX", "COUNT"]},
                    "field": {"type": "string"},
                    "alias": {"type": "string"},
                },
                "required": ["function", "field", "alias"],
            },
        },
        "sort": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "field": {"type": "string"},
                    "order": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["field", "order"],
            },
        },
        "max_count": {"type": "integer"},
    },
    "required": ["table", "filters", "group_by", "aggregates", "sort", "max_count"],
}


def _ask_system_prompt() -> str:
    """Describe the queryable tables/fields so the model picks valid ones."""
    lines = [
        "You translate a user's natural-language question into a structured query over "
        "the EU DSA VLOP transparency dataset. Output ONLY the structured query object — "
        "never SQL.",
        "",
        "Choose exactly one table and use ONLY that table's listed fields. Tables:",
    ]
    for name, spec in TABLES.items():
        dims = ", ".join(spec.dimensions) or "(none)"
        meas = ", ".join(spec.measures) or "(none)"
        lines.append(f"- {name}: {spec.description}")
        lines.append(f"    dimensions (text; EQ/IN): {dims}")
        lines.append(f"    measures (numeric; SUM/AVG/MIN/MAX + GT/GTE/LT/LTE): {meas}")
    lines += [
        "",
        "Guidance:",
        "- For 'top/most/by/total' questions: SUM a measure, group_by the relevant "
        "dimension, and sort desc by the aggregate's alias.",
        '- Use COUNT with field "*" to count rows (e.g. t11, which has no measures).',
        "- Put constraints (a specific platform, category, scope…) in `filters`.",
        "- service_name and platform exist on every table; platform is the parent company.",
        "- Default max_count to 10 unless the question implies otherwise.",
        "- Aggregate aliases must be letters, digits, or underscores.",
    ]
    return "\n".join(lines)


_anthropic_client = None


# Question → AskQuery cache. Translations are stable for a given question, so a
# repeated question skips the (paid) LLM call. Insertion-ordered dict as a tiny LRU.
_ask_cache: dict[str, dict[str, Any]] = {}
_ask_cache_lock = threading.Lock()


def _ask_cache_key(question: str) -> str:
    return " ".join(question.lower().split())


def _ask_cache_get(question: str) -> dict[str, Any] | None:
    with _ask_cache_lock:
        return _ask_cache.get(_ask_cache_key(question))


def _ask_cache_put(question: str, value: dict[str, Any]) -> None:
    with _ask_cache_lock:
        key = _ask_cache_key(question)
        _ask_cache.pop(key, None)
        _ask_cache[key] = value
        while len(_ask_cache) > ASK_CACHE_SIZE:
            _ask_cache.pop(next(iter(_ask_cache)))  # evict oldest


def _translate_question(question: str) -> dict[str, Any]:
    """Call Claude to turn a question into an AskQuery dict (constrained JSON).
    `anthropic` is imported lazily (only needed when the feature is enabled);
    monkeypatched in tests so the suite never makes a network call. The client is
    cached so its HTTP connection pool is reused across requests."""
    global _anthropic_client
    import anthropic

    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    resp = _anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_ask_system_prompt(),
        messages=[{"role": "user", "content": question}],
        output_config={"format": {"type": "json_schema", "schema": _ASK_SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _askquery_to_request(aq: dict[str, Any]) -> QueryRequest:
    """Map the LLM's constrained AskQuery dict onto the real QueryRequest model.
    QueryRequest construction + compile_query perform the actual validation."""
    conditions = [
        {"operation": f["op"], "field_name": f["field"], "field_values": f["values"]}
        for f in aq.get("filters", [])
    ]
    aggregates = []
    for a in aq.get("aggregates", []):
        is_count_star = a["function"] == "COUNT" and a.get("field", "*") in ("", "*", "rows", "(rows)")
        aggregates.append({
            "function": a["function"],
            "field_name": "*" if is_count_star else a["field"],
            "alias": a["alias"],
        })
    payload = {
        "table": aq.get("table"),
        "query": {"and": conditions},
        "group_by": aq.get("group_by", []),
        "aggregates": aggregates,
        "sort": [{"field_name": s["field"], "order": s["order"]} for s in aq.get("sort", [])],
        "max_count": aq.get("max_count") or 10,
    }
    # model_validate runs the same field validation as a request body would.
    return QueryRequest.model_validate(payload)


def _run_query_bounded(body: QueryRequest) -> dict[str, Any]:
    """Compile + run a structured query synchronously with the public row cap and
    no webhook — the shared trust boundary for /api/explore and /api/ask. Raises
    QueryCompileError if any field/operation is invalid for the table."""
    capped = min(body.max_count, EXPLORE_MAX_ROWS)
    safe = body.model_copy(update={"max_count": capped, "callback_url": None})
    sql, params, columns = compile_query(safe)  # validates against the registry
    conn = _connect_ro()
    try:
        rows = [list(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    # `truncated` lets the UI flag that the public row cap was hit.
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(rows) >= capped}


@api_router.post("/explore")
def explore(body: QueryRequest, request: Request) -> dict[str, Any]:
    """Public, synchronous, bounded query for the interactive dashboard.

    Same validated structured-query model as POST /api/query (no SQL is ever
    accepted; every field/operation is checked against the table registry and all
    values are bound), but it runs inline and hard-caps the row count — no auth,
    no job, no webhook. IP-rate-limited so the open endpoint can't be hammered."""
    if _key_store.incr(f"explore:{_client_ip(request)}", EXPLORE_RATE_WINDOW) > EXPLORE_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many queries from here. Please slow down.",
            headers={"Retry-After": str(EXPLORE_RATE_WINDOW)},
        )
    try:
        return _run_query_bounded(body)
    except QueryCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@api_router.post("/ask")
def ask(body: AskRequest, request: Request) -> dict[str, Any]:
    """Public natural-language query: an LLM translates the question into the
    *structured* QueryRequest (never SQL), which is then run through the exact same
    compile_query trust boundary as /api/explore. The model only proposes — a bad
    field is a 400, and no model-authored SQL can reach the database.

    Disabled (503) unless ANTHROPIC_API_KEY is set; IP-rate-limited (LLM calls
    cost money) on the same limiter as /api/explore."""
    if not NL_QUERY_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Natural-language queries aren't enabled on this server (set ANTHROPIC_API_KEY).",
        )
    if _key_store.incr(f"ask:{_client_ip(request)}", ASK_RATE_WINDOW) > ASK_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many questions from here. Please slow down.",
            headers={"Retry-After": str(ASK_RATE_WINDOW)},
        )
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    ask_query = _ask_cache_get(question)
    cached = ask_query is not None
    # `if ask_query is None` (not `if not cached`) so the reassignment narrows the
    # type for the rest of the function — no assert needed (assert is stripped by -O).
    if ask_query is None:
        try:
            ask_query = _translate_question(question)  # LLM → constrained dict
        except Exception:
            logger.exception("nl_translate_failed")
            raise HTTPException(status_code=502, detail="The language model could not translate that question.")
        _ask_cache_put(question, ask_query)
    try:
        req = _askquery_to_request(ask_query)
        result = _run_query_bounded(req)
    except (QueryCompileError, ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Surface the model's attempt so the user sees what it produced.
        raise HTTPException(
            status_code=422,
            detail={"error": f"Couldn't run that as a query: {exc}", "generated": ask_query},
        )
    return {"question": question, "query": ask_query, "cached": cached, **result}


@api_router.post("/portal/register", status_code=201)
def portal_register(body: RegisterRequest, request: Request) -> dict[str, Any]:
    """Issue a demo API key for a researcher (no real authentication)."""
    if not ALLOW_DEMO_KEYS:
        raise HTTPException(
            status_code=404,
            detail="Demo registration is disabled. Sign in with Google at POST /auth/google.",
        )
    name = body.name.strip()
    email = body.email.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty.")
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")

    # Throttle minting by client IP and by email so the open endpoint can't be
    # used to flood the key store.
    for bucket in (f"ip:{_client_ip(request)}", f"email:{email.lower()}"):
        if _key_store.incr(bucket, REGISTER_WINDOW) > REGISTER_MAX_PER_WINDOW:
            raise HTTPException(
                status_code=429,
                detail="Too many registrations from here. Please try again later.",
            )

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ISSUED_KEY_TTL)).isoformat()
    key = "rk_" + secrets.token_hex(16)
    _key_store.put(
        key,
        {"name": name, "email": email, "created_at": now.isoformat(), "expires_at": expires_at},
        ISSUED_KEY_TTL,
    )
    return {
        "api_key": key,
        "name": name,
        "expires_at": expires_at,
        "header": "X-API-Key",
        "note": "Pass this key in the X-API-Key header on every request. Demo key — not for production.",
    }


@api_router.delete("/portal/key")
def revoke_key(principal: dict = Depends(require_api_key)) -> dict[str, Any]:
    """Revoke the calling key/session (configured demo keys can't be revoked)."""
    key = principal["key"]
    if key in API_KEYS:
        raise HTTPException(status_code=400, detail="Configured keys cannot be revoked here.")
    _key_store.delete(key)
    return {"revoked": True}


# ── Google sign-in + admin approval ───────────────────────────────────────────
#
# Frontend uses Google Identity Services (FedCM in supporting browsers) to obtain
# an ID token, POSTs it here, and we verify it server-side. New accounts land in a
# `pending` registration until an admin approves; admins (ADMIN_EMAILS) are
# implicitly approved. Approved accounts get a first-party session key.


@api_router.post("/auth/google")
def auth_google(body: GoogleAuthRequest, request: Request, response: Response) -> dict[str, Any]:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this server.")
    # Throttle sign-in attempts per IP (reuses the registration limiter window).
    if _key_store.incr(f"authip:{_client_ip(request)}", REGISTER_WINDOW) > REGISTER_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Please try again later.")

    try:
        claims = _verify_id_token(body.credential)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google credential.")

    email = str(claims.get("email", "")).strip().lower()
    if not email or not claims.get("email_verified"):
        raise HTTPException(status_code=401, detail="Google account has no verified email.")
    name = claims.get("name") or email
    now = _now()

    reg = _registrations.get(email)
    is_admin = email in ADMIN_EMAILS
    if reg is None:
        reg = {"email": email, "name": name,
               "status": "approved" if is_admin else "pending",
               "requested_at": now, "updated_at": now,
               "approved_by": "auto:admin" if is_admin else None}
        _registrations.upsert(email, reg)
        logger.info("registration_created", extra={"data": {"email": email, "status": reg["status"]}})
    elif is_admin and reg.get("status") != "approved":
        reg.update(status="approved", updated_at=now, approved_by="auto:admin")
        _registrations.upsert(email, reg)

    if reg["status"] == "revoked":
        raise HTTPException(status_code=403, detail="Your access has been revoked.")
    if reg["status"] != "approved":
        response.status_code = 202
        return {"status": "pending", "email": email,
                "message": "Your access request is awaiting admin approval."}

    key, expires_at = _mint_session(email, reg.get("name") or name)
    logger.info("session_minted", extra={"data": {"email": email}})
    return {"status": "approved", "api_key": key, "name": reg.get("name") or name,
            "email": email, "expires_at": expires_at, "header": "X-API-Key"}


@api_router.get("/admin/registrations")
def list_registrations(status: str | None = None, _: dict = Depends(require_admin)) -> dict[str, Any]:
    """List researcher registrations, optionally filtered by status."""
    regs = _registrations.list()
    if status:
        regs = [r for r in regs if r.get("status") == status]
    regs.sort(key=lambda r: r.get("requested_at") or "")
    return {"registrations": regs, "count": len(regs)}


@api_router.post("/admin/registrations/{email}/approve")
def approve_registration(email: str, admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Approve an account (pre-approval is allowed before the user has signed in)."""
    email = email.strip().lower()
    now = _now()
    reg = _registrations.get(email)
    if reg is None:
        reg = {"email": email, "name": email, "status": "approved",
               "requested_at": now, "updated_at": now, "approved_by": admin.get("email")}
    else:
        reg.update(status="approved", updated_at=now, approved_by=admin.get("email"))
    _registrations.upsert(email, reg)
    logger.info("registration_approved", extra={"data": {"email": email, "by": admin.get("email")}})
    return {"email": email, "status": "approved"}


@api_router.post("/admin/registrations/{email}/revoke")
def revoke_registration(email: str, admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Revoke an account's access (its live sessions stop working immediately)."""
    email = email.strip().lower()
    if email in ADMIN_EMAILS:
        raise HTTPException(status_code=400, detail="Cannot revoke an admin account.")
    reg = _registrations.get(email)
    if reg is None:
        raise HTTPException(status_code=404, detail="No such registration.")
    reg.update(status="revoked", updated_at=_now(), approved_by=admin.get("email"))
    _registrations.upsert(email, reg)
    logger.info("registration_revoked", extra={"data": {"email": email, "by": admin.get("email")}})
    return {"email": email, "status": "revoked"}


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict[str, str]:
    """The deployed build (commit SHA on Cloud Run, else "dev") + app version."""
    return {"version": APP_VERSION, "app_version": app.version}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus metrics. No auth — scrape over an internal network only."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/readyz")
def ready() -> dict[str, str]:
    try:
        _connect_ro().close()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok"}


def _dataset_meta() -> dict[str, str]:
    try:
        conn = _connect_ro()
        try:
            return {k: v for k, v in conn.execute("SELECT key, value FROM meta").fetchall()}
        finally:
            conn.close()
    except Exception:
        return {}


def _example_for(table: str, spec: TableSpec) -> dict[str, Any]:
    """A runnable example query for a table — aggregate its first measure, or
    (for the text-only t11) fetch the qualitative field for one service."""
    measures = list(spec.measures)
    if measures:
        return {
            "table": table,
            "group_by": ["service_name"],
            "aggregates": [{"function": "SUM", "field_name": measures[0], "alias": "total"}],
            "sort": [{"field_name": "total", "order": "desc"}],
            "max_count": 10,
        }
    return {
        "table": table,
        "query": {"and": [{"operation": "EQ", "field_name": "service_name", "field_values": ["YouTube"]}]},
        "fields": [f for f in spec.dimensions if f != "platform"],
        "max_count": 10,
    }


def _table_fields_doc(table: str, spec: TableSpec) -> dict[str, Any]:
    return {
        "table": table,
        "description": spec.description,
        "dimensions": {
            "fields": sorted(spec.dimensions),
            "operations": ["EQ", "IN"],
            "usable_in": ["query", "fields", "group_by", "sort"],
            "note": "Text fields from the joined lookup tables.",
        },
        "measures": {
            "fields": sorted(spec.measures),
            "operations": ["EQ", "IN", "GT", "GTE", "LT", "LTE"],
            "usable_in": ["query", "fields", "aggregates"],
            "note": "Numeric measure columns on the fact table.",
        },
        "aggregate_functions": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
        "example": _example_for(table, spec),
    }


@api_router.get("/fields")
def list_fields(table: str | None = None, _: dict = Depends(require_api_key)) -> dict[str, Any]:
    """Fields for a report table (`?table=…`), or an overview of all tables."""
    if table is None:
        return {
            "note": "Pass ?table=<name> for a table's fields, or GET /schema/{table}.",
            "tables": {name: spec.description for name, spec in TABLES.items()},
            "aggregate_functions": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
        }
    spec = TABLES.get(table)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table}'. See GET /tables.")
    return _table_fields_doc(table, spec)


@api_router.get("/tables")
def list_tables(_: dict = Depends(require_api_key)) -> dict[str, Any]:
    """The queryable DSA report tables and the dataset's reporting period."""
    meta = _dataset_meta()
    return {
        "dataset": "EU DSA VLOP transparency reports",
        "period": meta.get("period"),
        "generated": meta.get("generated"),
        "tables": [{"name": name, "description": spec.description} for name, spec in TABLES.items()],
    }


@api_router.get("/schema/{table}")
def table_schema(table: str, _: dict = Depends(require_api_key)) -> dict[str, Any]:
    """The queryable field registry (dimensions + measures) for a report table."""
    spec = TABLES.get(table)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Table '{table}' not found. See GET /tables.")
    return _table_fields_doc(table, spec)


@api_router.post("/query", status_code=202)
def submit_query(
    body: QueryRequest,
    response: Response,
    principal: dict = Depends(require_api_key),
) -> dict[str, Any]:
    # Throttle the expensive job-spawning path per API key.
    if _key_store.incr(f"query:{principal['key']}", QUERY_RATE_WINDOW) > QUERY_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Query rate limit exceeded ({QUERY_RATE_MAX}/{QUERY_RATE_WINDOW}s). Slow down.",
            headers={"Retry-After": str(QUERY_RATE_WINDOW)},
        )

    try:
        sql, params, _columns = compile_query(body)
    except QueryCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if body.callback_url:
        try:
            _validate_callback_url(body.callback_url)
        except CallbackUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    job = Job(
        id=uuid.uuid4().hex,
        sql=sql,
        params=params,
        owner_key=principal["key"],
        submitted_by=principal["name"],
        callback_url=body.callback_url,
    )
    _store.put(job)
    JOB_QUEUE_DEPTH.inc()  # queued; decremented when _execute_job picks it up
    _executor.submit(_execute_job, job.id)
    logger.info("job_submitted", extra={"data": {"job_id": job.id, "user": principal["name"]}})
    response.headers["Location"] = f"{API_PREFIX}/jobs/{job.id}"
    return job.to_public()


@api_router.get("/jobs")
def list_jobs(limit: int = 50, principal: dict = Depends(require_api_key)) -> dict[str, Any]:
    return {"jobs": [j.to_public() for j in _store.list_for_owner(principal["key"], limit)]}


@api_router.get("/jobs/{job_id}")
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


@api_router.get("/jobs/{job_id}/result", response_model=None)
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


@api_router.get("/jobs/{job_id}/download", response_model=None)
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
    # Verify the signature *before* touching the store, so a caller without a
    # valid signature always gets 403 — whether or not the job id exists. This
    # avoids leaking which job ids are real (404 vs 403 probing).
    if not _verify_download_signature(job_id, format, expires, sig):
        raise HTTPException(status_code=403, detail="Invalid download signature.")
    if expires < int(time.time()):
        raise HTTPException(status_code=410, detail="Download link has expired.")

    job = _store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Result not found (may have expired).")
    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job not ready (status={job.status}).",
        )
    return _render_result(job_id, format, as_attachment=True)


@api_router.delete("/jobs/{job_id}")
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


# Mount the JSON API under /api (the dashboard owns "/"). Done last so every
# @api_router route above is registered before inclusion.
app.include_router(api_router, prefix=API_PREFIX)
