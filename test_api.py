"""Smoke tests for the structured-query API.

conftest.py sets up a temp SQLite DB and env vars before main is imported.
No Redis required — the in-memory store is used automatically.
"""
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)
ALICE = {"X-API-Key": "alice"}
BOB = {"X-API-Key": "bob"}

# A trivial valid query: count rows in t4_notices (3 in the conftest fixture).
COUNT_ALL = {"table": "t4_notices", "aggregates": [{"function": "COUNT", "alias": "n"}]}


def _wait_for_job(job_id: str, headers: dict, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal state within {timeout}s")


def _submit_and_wait(query: dict, headers: dict = ALICE) -> dict:
    r = client.post("/api/query", json=query, headers=headers)
    assert r.status_code == 202
    return _wait_for_job(r.json()["job_id"], headers)


# ── Infrastructure ────────────────────────────────────────────────────────────

class TestInfra:
    def test_api_index_no_auth(self):
        r = client.get("/api")
        assert r.status_code == 200
        assert "endpoints" in r.json()

    def test_dashboard_page_served(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_healthz(self):
        assert client.get("/healthz").status_code == 200

    def test_readyz(self):
        assert client.get("/readyz").status_code == 200


# ── Researcher portal ─────────────────────────────────────────────────────────

class TestPortal:
    def test_portal_page_served(self):
        r = client.get("/portal")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Research Data Portal" in r.text

    def test_register_issues_working_key(self):
        r = client.post("/api/portal/register", json={"name": "Ada Lovelace", "email": "ada@rs.org"})
        assert r.status_code == 201
        body = r.json()
        key = body["api_key"]
        assert key.startswith("rk_")
        assert body["name"] == "Ada Lovelace"
        assert "expires_at" in body
        # The issued key authenticates real API calls.
        assert client.get("/api/fields", headers={"X-API-Key": key}).status_code == 200
        assert client.get("/api/tables", headers={"X-API-Key": key}).status_code == 200

    def test_issued_key_can_be_revoked(self):
        key = client.post(
            "/api/portal/register", json={"name": "Grace", "email": "grace@navy.mil"}
        ).json()["api_key"]
        hdr = {"X-API-Key": key}
        assert client.get("/api/fields", headers=hdr).status_code == 200
        assert client.delete("/api/portal/key", headers=hdr).json()["revoked"] is True
        # Revoked key no longer authenticates.
        assert client.get("/api/fields", headers=hdr).status_code == 401

    def test_configured_key_cannot_be_revoked(self):
        assert client.delete("/api/portal/key", headers=ALICE).status_code == 400

    def test_register_bad_email_is_400(self):
        r = client.post("/api/portal/register", json={"name": "Ada", "email": "not-an-email"})
        assert r.status_code == 400

    def test_register_whitespace_name_is_400(self):
        r = client.post("/api/portal/register", json={"name": "   ", "email": "ada@rs.org"})
        assert r.status_code == 400

    def test_register_missing_field_is_422(self):
        assert client.post("/api/portal/register", json={"name": "Ada"}).status_code == 422

    def test_unknown_issued_key_rejected(self):
        assert client.get("/api/fields", headers={"X-API-Key": "rk_deadbeef"}).status_code == 401

    def test_register_rate_limited(self):
        # Use an isolated store + a low limit so we exercise the 429 path without
        # polluting the shared TestClient IP bucket the other tests rely on.
        import main

        original_store, original_limit = main._key_store, main.REGISTER_MAX_PER_WINDOW
        main._key_store = main.MemoryKeyStore()
        main.REGISTER_MAX_PER_WINDOW = 3
        try:
            statuses = [
                client.post("/api/portal/register", json={"name": f"R{i}", "email": f"r{i}@x.org"}).status_code
                for i in range(5)
            ]
            assert statuses == [201, 201, 201, 429, 429]
        finally:
            main._key_store, main.REGISTER_MAX_PER_WINDOW = original_store, original_limit


class TestKeyStore:
    def test_expiry_and_delete(self):
        from main import MemoryKeyStore

        s = MemoryKeyStore()
        s.put("k1", {"name": "a"}, ttl=100)
        assert s.get("k1") == {"name": "a"}
        s.put("k2", {"name": "b"}, ttl=-1)  # already expired
        assert s.get("k2") is None
        assert s.delete("k1") is True
        assert s.get("k1") is None
        assert s.delete("missing") is False

    def test_incr_counts_within_window(self):
        from main import MemoryKeyStore

        s = MemoryKeyStore()
        assert [s.incr("b", 60) for _ in range(3)] == [1, 2, 3]
        assert s.incr("other", 60) == 1  # buckets are independent

    def test_incr_prunes_stale_buckets(self):
        from main import MemoryKeyStore

        s = MemoryKeyStore()
        s._hits["old"] = [0.0]  # a bucket whose only hit is far outside any window
        s._last_sweep = 0.0
        s.incr("new", 60)  # triggers the lazy sweep
        assert "old" not in s._hits and "new" in s._hits

    def test_client_ip_respects_trust_flag(self):
        import main

        class Req:
            def __init__(self, xff, host):
                self.headers = {"x-forwarded-for": xff} if xff else {}
                self.client = type("C", (), {"host": host})()

        original = main.TRUST_PROXY_HEADERS
        try:
            main.TRUST_PROXY_HEADERS = False  # XFF ignored (spoofable) by default
            assert main._client_ip(Req("1.2.3.4", "10.0.0.1")) == "10.0.0.1"
            main.TRUST_PROXY_HEADERS = True  # trust the proxy's first hop
            assert main._client_ip(Req("1.2.3.4, 5.6.7.8", "10.0.0.1")) == "1.2.3.4"
            assert main._client_ip(Req(None, "10.0.0.1")) == "10.0.0.1"
        finally:
            main.TRUST_PROXY_HEADERS = original


# ── Auth ──────────────────────────────────────────────────────────────────────

class TestAuth:
    def test_no_key_is_401(self):
        assert client.get("/api/tables").status_code == 401

    def test_bad_key_is_401(self):
        assert client.get("/api/tables", headers={"X-API-Key": "bogus"}).status_code == 401

    def test_valid_key_ok(self):
        r = client.get("/api/tables", headers=ALICE)
        assert r.status_code == 200
        assert [t["name"] for t in r.json()["tables"]]


# ── Schema ────────────────────────────────────────────────────────────────────

class TestSchema:
    def test_tables_lists_report_tables(self):
        r = client.get("/api/tables", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        names = [t["name"] for t in body["tables"]]
        assert "t4_notices" in names and "t11_qualitative" in names
        assert body["period"] == "2025-07-01/2025-12-31"

    def test_known_table_schema(self):
        r = client.get("/api/schema/t4_notices", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        assert "service_name" in body["dimensions"]["fields"]
        assert "notices" in body["measures"]["fields"]

    def test_missing_table_is_404(self):
        assert client.get("/api/schema/nonexistent", headers=ALICE).status_code == 404


# ── Fields discovery ──────────────────────────────────────────────────────────

class TestFields:
    def test_overview_lists_tables(self):
        r = client.get("/api/fields", headers=ALICE)
        assert r.status_code == 200
        assert "t4_notices" in r.json()["tables"]

    def test_per_table_fields(self):
        r = client.get("/api/fields?table=t4_notices", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        assert "service_name" in body["dimensions"]["fields"]
        assert "notices" in body["measures"]["fields"]
        assert "SUM" in body["aggregate_functions"]

    def test_unknown_table_is_404(self):
        assert client.get("/api/fields?table=nope", headers=ALICE).status_code == 404

    def test_fields_requires_auth(self):
        assert client.get("/api/fields").status_code == 401


# ── Query lifecycle ───────────────────────────────────────────────────────────

class TestQueryLifecycle:
    def test_submit_returns_202_with_location(self):
        r = client.post("/api/query", json=COUNT_ALL, headers=ALICE)
        assert r.status_code == 202
        assert "job_id" in r.json()
        assert r.headers.get("location", "").startswith("/api/jobs/")

    def test_happy_path_json(self):
        job = _submit_and_wait(COUNT_ALL)
        assert job["status"] == "done"
        r = client.get(f"/api/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        assert body["row_count"] == 1
        assert body["columns"] == ["n"]
        assert body["rows"][0][0] == 3  # 3 rows seeded in conftest.py

    def test_happy_path_csv(self):
        job = _submit_and_wait(
            {
                "table": "t4_notices",
                "fields": ["service_name"],
                "sort": [{"field_name": "service_name", "order": "asc"}],
            }
        )
        r = client.get(f"/api/jobs/{job['job_id']}/result?format=csv", headers=ALICE)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.strip().splitlines()
        assert lines[0] == "service_name"
        assert "YouTube" in lines
        assert "Facebook" in lines

    def test_filter_group_and_aggregate(self):
        # Notices per service, YouTube only — exercises filter + group + agg + sort.
        job = _submit_and_wait(
            {
                "table": "t4_notices",
                "query": {
                    "and": [
                        {"operation": "EQ", "field_name": "service_name", "field_values": ["YouTube"]}
                    ]
                },
                "group_by": ["service_name"],
                "aggregates": [
                    {"function": "SUM", "field_name": "notices", "alias": "notices"}
                ],
                "sort": [{"field_name": "notices", "order": "desc"}],
            }
        )
        assert job["status"] == "done"
        r = client.get(f"/api/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        body = r.json()
        assert body["columns"] == ["service_name", "notices"]
        # YouTube t4 rows in conftest: notices 100 + 40 = 140.
        assert body["rows"] == [["YouTube", 140]]

    def test_compiled_sql_is_parameterised(self):
        r = client.post(
            "/api/query",
            json={
                "table": "t4_notices",
                "query": {
                    "and": [
                        {"operation": "EQ", "field_name": "service_name", "field_values": ["YouTube"]}
                    ]
                }
            },
            headers=ALICE,
        )
        job = _wait_for_job(r.json()["job_id"], ALICE)
        # Value is bound, not interpolated — 'YouTube' must not appear in the SQL.
        assert "?" in job["compiled_sql"]
        assert "YouTube" not in job["compiled_sql"]

    def test_result_before_done_is_409(self):
        r = client.post("/api/query", json=COUNT_ALL, headers=ALICE)
        job_id = r.json()["job_id"]
        # Job may already be done by the time we hit the result endpoint,
        # but if it's still in-flight we expect 409.
        r2 = client.get(f"/api/jobs/{job_id}/result", headers=ALICE)
        assert r2.status_code in (200, 409)

    def test_list_jobs(self):
        _submit_and_wait(COUNT_ALL)
        r = client.get("/api/jobs", headers=ALICE)
        assert r.status_code == 200
        assert len(r.json()["jobs"]) >= 1


# ── Secure download URLs ──────────────────────────────────────────────────────

class TestSecureDownload:
    def test_done_job_exposes_signed_download_urls(self):
        job = _submit_and_wait(COUNT_ALL)
        urls = job["download_urls"]
        assert set(urls) == {"json", "csv"}
        for u in urls.values():
            assert "sig=" in u and "expires=" in u

    def test_download_without_api_key_works(self):
        job = _submit_and_wait(COUNT_ALL)
        url = job["download_urls"]["json"]
        # No X-API-Key header — the signed URL is the capability.
        r = client.get(url)
        assert r.status_code == 200
        assert r.json()["rows"][0][0] == 3
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_download_csv_without_api_key_works(self):
        job = _submit_and_wait(COUNT_ALL)
        r = client.get(job["download_urls"]["csv"])
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    def test_tampered_signature_is_403(self):
        job = _submit_and_wait(COUNT_ALL)
        url = job["download_urls"]["json"]
        tampered = url[:-1] + ("0" if url[-1] != "0" else "1")
        assert client.get(tampered).status_code == 403

    def test_tampered_expiry_is_403(self):
        job = _submit_and_wait(COUNT_ALL)
        url = job["download_urls"]["json"]
        # Extending the expiry without re-signing breaks the signature.
        bumped = re.sub(r"expires=\d+", "expires=99999999999", url)
        assert client.get(bumped).status_code == 403

    def test_expired_link_is_410(self):
        from main import _download_signature

        job = _submit_and_wait(COUNT_ALL)
        job_id = job["job_id"]
        # Forge a correctly-signed but already-expired link.
        expires = 1
        sig = _download_signature(job_id, "json", expires)
        r = client.get(f"/api/jobs/{job_id}/download?format=json&expires={expires}&sig={sig}")
        assert r.status_code == 410

    def test_signature_bound_to_format(self):
        job = _submit_and_wait(COUNT_ALL)
        # Take the json URL but swap the declared format to csv — signature no longer matches.
        url = job["download_urls"]["json"].replace("format=json", "format=csv")
        assert client.get(url).status_code == 403

    def test_unknown_job_download_is_403(self):
        # Signature is verified before any store lookup, so an unknown job id with
        # an invalid signature returns 403 (not 404) — existence isn't leaked.
        r = client.get("/api/jobs/doesnotexist/download?format=json&expires=99999999999&sig=abc")
        assert r.status_code == 403


# ── Job isolation ─────────────────────────────────────────────────────────────

class TestJobIsolation:
    def test_bob_cannot_see_alices_job(self):
        job = _submit_and_wait(COUNT_ALL)
        assert client.get(f"/api/jobs/{job['job_id']}", headers=BOB).status_code == 404

    def test_bob_cannot_fetch_alices_result(self):
        job = _submit_and_wait(COUNT_ALL)
        r = client.get(f"/api/jobs/{job['job_id']}/result", headers=BOB)
        assert r.status_code == 404


# ── Safety: no arbitrary SQL ──────────────────────────────────────────────────

class TestSafety:
    def test_missing_table_is_400(self):
        r = client.post("/api/query", json={"aggregates": [{"function": "COUNT", "alias": "n"}]}, headers=ALICE)
        assert r.status_code == 400

    def test_unknown_table_is_400(self):
        r = client.post("/api/query", json={"table": "t99_nope", "fields": ["service_name"]}, headers=ALICE)
        assert r.status_code == 400

    def test_field_from_other_table_is_400(self):
        # `notices` belongs to t4, not t3 — it must not be accepted for t3.
        r = client.post(
            "/api/query",
            json={"table": "t3_member_state_orders", "fields": ["notices"]},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_unknown_field_is_400(self):
        r = client.post(
            "/api/query",
            json={"table": "t4_notices", "query": {"and": [{"operation": "EQ", "field_name": "secrets", "field_values": ["x"]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_comparator_on_text_field_is_400(self):
        r = client.post(
            "/api/query",
            json={"table": "t4_notices", "query": {"and": [{"operation": "GT", "field_name": "service_name", "field_values": [5]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_bad_alias_is_400(self):
        r = client.post(
            "/api/query",
            json={"table": "t4_notices", "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "x); DROP"}]},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_injection_value_is_treated_as_data(self):
        # A SQL-looking string in field_values is bound as a parameter, so the
        # query runs successfully and simply matches nothing — it is never code.
        job = _submit_and_wait(
            {
                "table": "t4_notices",
                "query": {
                    "and": [
                        {
                            "operation": "EQ",
                            "field_name": "service_name",
                            "field_values": ["YouTube'; DROP TABLE services;--"],
                        }
                    ]
                },
            }
        )
        assert job["status"] == "done"
        r = client.get(f"/api/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        assert r.json()["row_count"] == 0
        # DB still intact afterwards — a follow-up count still works.
        again = _submit_and_wait(COUNT_ALL)
        r2 = client.get(f"/api/jobs/{again['job_id']}/result?format=json", headers=ALICE)
        assert r2.json()["rows"][0][0] == 3

    def test_dimension_requires_string(self):
        # A numeric value on a TEXT dimension would silently match nothing under
        # SQLite affinity rules, so it is rejected up front.
        r = client.post(
            "/api/query",
            json={"table": "t4_notices", "query": {"and": [{"operation": "EQ", "field_name": "service_name", "field_values": [123]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_duplicate_group_by_is_400(self):
        r = client.post(
            "/api/query",
            json={
                "table": "t4_notices",
                "group_by": ["service_name", "service_name"],
                "aggregates": [{"function": "COUNT", "alias": "n"}],
            },
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_alias_clashing_with_group_by_is_400(self):
        r = client.post(
            "/api/query",
            json={
                "table": "t4_notices",
                "group_by": ["service_name"],
                "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "service_name"}],
            },
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_duplicate_field_is_400(self):
        r = client.post(
            "/api/query",
            json={"table": "t4_notices", "fields": ["service_name", "service_name"]},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_extra_sql_field_ignored(self):
        # The model has no free-form `sql` field; an extra one is ignored, and the
        # query runs the validated default SELECT for the named table.
        r = client.post("/api/query", json={"table": "t4_notices", "sql": "DROP TABLE services"}, headers=ALICE)
        assert r.status_code == 202
        job = _wait_for_job(r.json()["job_id"], ALICE)
        assert job["status"] == "done"  # ran the default SELECT, not the DROP

    def test_unknown_job_is_404(self):
        assert client.get("/api/jobs/doesnotexist", headers=ALICE).status_code == 404


# ── Cancel / delete ───────────────────────────────────────────────────────────

class TestDelete:
    def test_delete_completed_job(self):
        job = _submit_and_wait(COUNT_ALL)
        job_id = job["job_id"]
        r = client.delete(f"/api/jobs/{job_id}", headers=ALICE)
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert client.get(f"/api/jobs/{job_id}", headers=ALICE).status_code == 404


# ── Query rate limiting ─────────────────────────────────────────────────────────

class TestQueryRateLimit:
    def test_over_limit_returns_429_with_retry_after(self):
        import main

        original_store, original_max = main._key_store, main.QUERY_RATE_MAX
        main._key_store = main.MemoryKeyStore()  # isolated, so it doesn't affect other tests
        main.QUERY_RATE_MAX = 2
        try:
            statuses = [client.post("/api/query", json=COUNT_ALL, headers=ALICE).status_code for _ in range(4)]
            assert statuses == [202, 202, 429, 429]
            r = client.post("/api/query", json=COUNT_ALL, headers=ALICE)
            assert r.status_code == 429 and r.headers["Retry-After"] == str(main.QUERY_RATE_WINDOW)
        finally:
            main._key_store, main.QUERY_RATE_MAX = original_store, original_max

    def test_limit_is_per_key(self):
        import main

        original_store, original_max = main._key_store, main.QUERY_RATE_MAX
        main._key_store = main.MemoryKeyStore()
        main.QUERY_RATE_MAX = 1
        try:
            assert client.post("/api/query", json=COUNT_ALL, headers=ALICE).status_code == 202
            assert client.post("/api/query", json=COUNT_ALL, headers=ALICE).status_code == 429
            # bob has his own bucket and is unaffected
            assert client.post("/api/query", json=COUNT_ALL, headers=BOB).status_code == 202
        finally:
            main._key_store, main.QUERY_RATE_MAX = original_store, original_max


# ── Structured logging ──────────────────────────────────────────────────────────

class TestLogging:
    def test_json_formatter_emits_event_and_data(self):
        import logging

        from main import JsonLogFormatter

        rec = logging.LogRecord("api_demo", logging.INFO, __file__, 1, "job_done", None, None)
        rec.data = {"job_id": "abc", "rows": 5}
        line = json.loads(JsonLogFormatter().format(rec))
        assert line["event"] == "job_done"
        assert line["level"] == "INFO"
        assert line["job_id"] == "abc" and line["rows"] == 5
        assert "ts" in line

    def test_json_formatter_includes_exception(self):
        import logging
        import sys

        from main import JsonLogFormatter

        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.LogRecord(
                "api_demo", logging.ERROR, __file__, 1, "request_error", None, sys.exc_info()
            )
        line = json.loads(JsonLogFormatter().format(rec))
        assert "boom" in line["exc"]

    def test_request_emits_request_id_header(self):
        r = client.get("/healthz")
        assert re.fullmatch(r"[0-9a-f]{16}", r.headers["X-Request-ID"])

    def test_formatter_uses_record_created_timestamp(self):
        import logging

        from main import JsonLogFormatter

        rec = logging.LogRecord("api_demo", logging.INFO, __file__, 1, "e", None, None)
        rec.created = 1577836800.0  # 2020-01-01T00:00:00Z
        line = json.loads(JsonLogFormatter().format(rec))
        assert line["ts"].startswith("2020-01-01T00:00:00")

    def test_invalid_log_level_falls_back_to_info(self):
        import logging

        import main

        original = main.LOG_LEVEL
        main.LOG_LEVEL = "DEBUGGING"  # not a real level
        try:
            assert main._configure_logging().level == logging.INFO
        finally:
            main.LOG_LEVEL = original
            main._configure_logging()  # restore real config on the shared logger

    def test_unhandled_endpoint_exception_is_logged(self):
        # Regression guard: confirm our @app.middleware("http") still catches
        # unhandled endpoint exceptions (Starlette propagates them through
        # BaseHTTPMiddleware), so they reach the structured "request_error" log.
        import io
        import logging

        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        import main

        probe = FastAPI()
        probe.add_middleware(BaseHTTPMiddleware, dispatch=main.log_requests)

        @probe.get("/boom")
        def boom():
            raise ValueError("kaboom")

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        main.logger.addHandler(handler)
        try:
            TestClient(probe, raise_server_exceptions=False).get("/boom")
        finally:
            main.logger.removeHandler(handler)
        assert "request_error" in buf.getvalue()


# ── Prometheus metrics ──────────────────────────────────────────────────────────

class TestMetrics:
    def test_metrics_endpoint_exposes_prometheus_text(self):
        client.get("/healthz")  # generate at least one request sample
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        # Metric families are declared even before first use.
        for name in (
            "api_demo_http_requests_total",
            "api_demo_http_request_duration_seconds",
            "api_demo_jobs_in_flight",
            "api_demo_jobs_total",
            "api_demo_job_queue_depth",
        ):
            assert name in body

    def test_request_counter_uses_route_template_not_raw_path(self):
        # A job id in the URL must not leak into label cardinality.
        job = _submit_and_wait(COUNT_ALL)
        client.get(f"/api/jobs/{job['job_id']}", headers=ALICE)
        body = client.get("/metrics").text
        assert 'path="/api/jobs/{job_id}"' in body
        assert job["job_id"] not in body  # the literal id is never a label value

    def test_job_completion_increments_jobs_total(self):
        from prometheus_client import REGISTRY

        before = REGISTRY.get_sample_value("api_demo_jobs_total", {"status": "done"}) or 0.0
        _submit_and_wait(COUNT_ALL)
        after = REGISTRY.get_sample_value("api_demo_jobs_total", {"status": "done"})
        assert after is not None and after >= before + 1

    def test_queue_depth_balances_to_zero_at_rest(self):
        from prometheus_client import REGISTRY

        # inc on submit / dec on pickup should net to zero once the job is done.
        _submit_and_wait(COUNT_ALL)
        assert REGISTRY.get_sample_value("api_demo_job_queue_depth") == 0.0


# ── Webhook callbacks ───────────────────────────────────────────────────────────

class TestCallbacks:
    def test_bad_scheme_rejected_at_submit(self):
        r = client.post(
            "/api/query", json={**COUNT_ALL, "callback_url": "ftp://example.com/hook"}, headers=ALICE
        )
        assert r.status_code == 400

    def test_missing_host_rejected_at_submit(self):
        r = client.post(
            "/api/query", json={**COUNT_ALL, "callback_url": "http:///nohost"}, headers=ALICE
        )
        assert r.status_code == 400

    def test_ssrf_guard_blocks_private_targets(self):
        import main

        original = main.CALLBACK_ALLOW_PRIVATE
        main.CALLBACK_ALLOW_PRIVATE = False  # exercise the guard regardless of env
        try:
            for bad in (
                "http://127.0.0.1/x",           # loopback
                "http://169.254.169.254/x",     # cloud metadata (link-local)
                "http://10.1.2.3/x",            # private
                "http://[::1]/x",               # ipv6 loopback
                "http://[::ffff:127.0.0.1]/x",  # ipv4-mapped ipv6 loopback (bypass attempt)
            ):
                with pytest.raises(main.CallbackUrlError):
                    main._validate_callback_url(bad)
            # A public literal IP (no DNS needed) passes.
            main._validate_callback_url("http://8.8.8.8/ok")
        finally:
            main.CALLBACK_ALLOW_PRIVATE = original

    def test_end_to_end_delivery_is_signed(self):
        import hashlib
        import hmac
        import http.server
        import threading

        import main
        from prometheus_client import REGISTRY

        captured: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                captured.append({"headers": dict(self.headers), "body": self.rfile.read(n)})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        delivered_before = REGISTRY.get_sample_value(
            "api_demo_callbacks_total", {"result": "delivered"}
        ) or 0.0
        try:
            cb = f"http://127.0.0.1:{server.server_port}/hook"
            job = _submit_and_wait({**COUNT_ALL, "callback_url": cb})
            deadline = time.monotonic() + 5
            while not captured and time.monotonic() < deadline:
                time.sleep(0.05)
            assert captured, "callback was never delivered"
        finally:
            server.shutdown()

        rec = captured[0]
        payload = json.loads(rec["body"])
        assert payload["event"] == "job.done"
        assert payload["job"]["job_id"] == job["job_id"]
        assert payload["job"]["download_urls"]["json"]  # present on a done job
        expected = "sha256=" + hmac.new(
            main.DOWNLOAD_URL_SECRET.encode(), rec["body"], hashlib.sha256
        ).hexdigest()
        assert rec["headers"].get("X-Webhook-Signature") == expected

        after = REGISTRY.get_sample_value("api_demo_callbacks_total", {"result": "delivered"})
        assert after is not None and after >= delivered_before + 1

    def test_unreachable_target_records_failure(self):
        import main
        from prometheus_client import REGISTRY

        original = main.CALLBACK_MAX_ATTEMPTS
        main.CALLBACK_MAX_ATTEMPTS = 1  # fail fast, no backoff sleeps
        before = REGISTRY.get_sample_value(
            "api_demo_callbacks_total", {"result": "failed"}
        ) or 0.0
        try:
            _submit_and_wait({**COUNT_ALL, "callback_url": "http://127.0.0.1:1/closed"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                now = REGISTRY.get_sample_value("api_demo_callbacks_total", {"result": "failed"})
                if now is not None and now >= before + 1:
                    break
                time.sleep(0.05)
            assert (REGISTRY.get_sample_value(
                "api_demo_callbacks_total", {"result": "failed"}) or 0.0) >= before + 1
        finally:
            main.CALLBACK_MAX_ATTEMPTS = original


# ── Google sign-in + admin approval ──────────────────────────────────────────


def _fake_verify(credential):
    """Stand-in for Google token verification. `credential` is treated as the
    account email; prefix tricks model the failure modes."""
    if credential == "BAD":
        raise ValueError("invalid token")
    if credential.startswith("unverified:"):
        return {"email": credential.split(":", 1)[1], "email_verified": False, "name": "X"}
    return {"email": credential, "email_verified": True, "name": credential.split("@")[0]}


class TestGoogleAuth:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        # Patch token verification and give each test a fresh registration store
        # so approval state doesn't leak between tests.
        import main
        monkeypatch.setattr(main, "_verify_id_token", _fake_verify)
        monkeypatch.setattr(main, "_registrations", main.MemoryRegistrationStore())
        yield

    def _signin(self, email):
        return client.post("/api/auth/google", json={"credential": email})

    def test_new_user_is_pending(self):
        r = self._signin("newbie@example.com")
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "pending"
        assert "api_key" not in body

    def test_admin_is_auto_approved(self):
        r = self._signin("admin@example.com")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"
        assert client.get("/api/tables", headers={"X-API-Key": body["api_key"]}).status_code == 200

    def test_invalid_credential_is_401(self):
        assert self._signin("BAD").status_code == 401

    def test_unverified_email_is_401(self):
        assert self._signin("unverified:x@example.com").status_code == 401

    def test_approval_flow_issues_working_key(self):
        assert self._signin("res@example.com").status_code == 202
        admin_key = self._signin("admin@example.com").json()["api_key"]
        appr = client.post("/api/admin/registrations/res@example.com/approve",
                           headers={"X-API-Key": admin_key})
        assert appr.status_code == 200 and appr.json()["status"] == "approved"
        r = self._signin("res@example.com")
        assert r.status_code == 200
        assert client.get("/api/tables", headers={"X-API-Key": r.json()["api_key"]}).status_code == 200

    def test_non_admin_cannot_reach_admin_endpoints(self):
        admin_key = self._signin("admin@example.com").json()["api_key"]
        client.post("/api/admin/registrations/u@example.com/approve", headers={"X-API-Key": admin_key})
        user_key = self._signin("u@example.com").json()["api_key"]
        assert client.get("/api/admin/registrations", headers={"X-API-Key": user_key}).status_code == 403
        assert client.get("/api/admin/registrations").status_code == 401

    def test_revoke_invalidates_live_session(self):
        admin_key = self._signin("admin@example.com").json()["api_key"]
        client.post("/api/admin/registrations/r3@example.com/approve", headers={"X-API-Key": admin_key})
        user_key = self._signin("r3@example.com").json()["api_key"]
        assert client.get("/api/tables", headers={"X-API-Key": user_key}).status_code == 200
        client.post("/api/admin/registrations/r3@example.com/revoke", headers={"X-API-Key": admin_key})
        # The live session stops working immediately (re-checked on every request).
        assert client.get("/api/tables", headers={"X-API-Key": user_key}).status_code == 401
        # And a fresh sign-in is rejected as revoked.
        assert self._signin("r3@example.com").status_code == 403

    def test_list_registrations_filters_by_status(self):
        admin_key = self._signin("admin@example.com").json()["api_key"]
        self._signin("p1@example.com")
        self._signin("p2@example.com")
        body = client.get("/api/admin/registrations?status=pending",
                          headers={"X-API-Key": admin_key}).json()
        emails = {r["email"] for r in body["registrations"]}
        assert {"p1@example.com", "p2@example.com"} <= emails

    def test_cannot_revoke_admin(self):
        admin_key = self._signin("admin@example.com").json()["api_key"]
        r = client.post("/api/admin/registrations/admin@example.com/revoke",
                        headers={"X-API-Key": admin_key})
        assert r.status_code == 400


# ── CORS (configurable cross-origin access) ──────────────────────────────────

class TestCORS:
    def test_disabled_by_default(self):
        # No ALLOWED_ORIGINS in the test env → no CORS headers emitted.
        r = client.get("/", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    def test_origin_parsing(self, monkeypatch):
        import main
        monkeypatch.setenv("ALLOWED_ORIGINS", " https://a.example , https://b.example ,")
        assert main._cors_origins() == ["https://a.example", "https://b.example"]


# ── Version / build identifier ───────────────────────────────────────────────

class TestVersion:
    def test_version_endpoint(self):
        r = client.get("/version")
        assert r.status_code == 200
        body = r.json()
        # Defaults to "dev" in tests (APP_VERSION unset); the CD workflow injects
        # the commit SHA on Cloud Run.
        assert body["version"] == "dev"
        assert "app_version" in body

    def test_x_version_header_on_every_response(self):
        r = client.get("/healthz")
        assert r.headers.get("X-Version") == "dev"

    def test_nosniff_header_on_every_response(self):
        r = client.get("/healthz")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"


# ── Combined site: dashboard + public overview ───────────────────────────────

class TestDashboard:
    def test_overview_is_public_and_populated(self):
        r = client.get("/api/overview")  # no X-API-Key
        assert r.status_code == 200
        d = r.json()
        assert d["services"] > 0 and d["platforms"] > 0
        assert d["total_notices"] >= 0
        assert isinstance(d["top_platforms"], list) and d["top_platforms"]
        assert {"platform", "notices"} <= set(d["top_platforms"][0])
        assert isinstance(d["by_category"], list)
        assert "period" in d

    def test_dashboard_served_at_root(self):
        r = client.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "/api/overview" in r.text  # dashboard fetches the public overview

    def test_dashboard_uses_vendored_chartjs(self):
        r = client.get("/")
        # Chart.js is self-hosted, not loaded from a CDN.
        assert "/static/vendor/chart.umd.js" in r.text
        assert "cdn.jsdelivr.net" not in r.text

    def test_vendored_chartjs_served(self):
        r = client.get("/static/vendor/chart.umd.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert "immutable" in r.headers.get("cache-control", "")
        assert "Chart.js v4.4.4" in r.text  # genuine vendored bundle

    def test_unknown_vendor_asset_404(self):
        # Only allowlisted filenames are served — no path traversal / arbitrary reads.
        assert client.get("/static/vendor/secrets.js").status_code == 404
        assert client.get("/static/vendor/..%2fmain.py").status_code in (404, 400)


# ── Public interactive query (POST /api/explore) ─────────────────────────────

class TestExplore:
    def test_options_public(self):
        r = client.get("/api/explore/options")  # no key
        assert r.status_code == 200
        d = r.json()
        assert d["max_rows"] > 0 and "SUM" in d["aggregates"]
        t4 = next(t for t in d["tables"] if t["table"] == "t4_notices")
        assert "platform" in t4["dimensions"] and "notices" in t4["measures"]

    def test_explore_aggregated_query_public(self):
        q = {"table": "t4_notices", "group_by": ["platform"],
             "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "value"}],
             "sort": [{"field_name": "value", "order": "desc"}], "max_count": 5}
        r = client.post("/api/explore", json=q)  # no key
        assert r.status_code == 200
        d = r.json()
        assert d["columns"] == ["platform", "value"]
        assert 0 < len(d["rows"]) <= 5
        vals = [row[1] for row in d["rows"]]
        assert vals == sorted(vals, reverse=True)  # sorted desc

    def test_explore_caps_rows(self):
        r = client.post("/api/explore", json={"table": "t4_notices",
                                              "fields": ["service_name", "notices"],
                                              "max_count": 100000})
        assert r.status_code == 200
        assert r.json()["row_count"] <= 500

    def test_explore_rejects_invalid_table(self):
        assert client.post("/api/explore", json={"table": "nope", "group_by": ["platform"]}).status_code == 400

    def test_explore_ignores_callback_url(self):
        # callback_url is stripped — no SSRF surface, query still runs.
        r = client.post("/api/explore", json={
            "table": "t4_notices", "group_by": ["platform"],
            "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "value"}],
            "max_count": 3, "callback_url": "http://169.254.169.254/latest/meta-data"})
        assert r.status_code == 200


# ── Natural-language query (POST /api/ask) ───────────────────────────────────

class TestAsk:
    @pytest.fixture(autouse=True)
    def _mock_llm(self, monkeypatch):
        # Stand in for the Claude call: return a fixed AskQuery for "top platforms
        # by notices". No network, deterministic.
        def fake_translate(question):
            assert isinstance(question, str) and question
            return {
                "table": "t4_notices",
                "filters": [],
                "group_by": ["platform"],
                "aggregates": [{"function": "SUM", "field": "notices", "alias": "notices"}],
                "sort": [{"field": "notices", "order": "desc"}],
                "max_count": 5,
            }
        import main
        main._ask_cache.clear()  # no cross-test cache leakage
        monkeypatch.setattr(main, "_translate_question", fake_translate)
        yield

    def test_ask_runs_generated_query(self):
        r = client.post("/api/ask", json={"question": "top platforms by notices?"})
        assert r.status_code == 200
        d = r.json()
        assert d["question"] == "top platforms by notices?"
        assert d["query"]["table"] == "t4_notices"
        assert d["columns"] == ["platform", "notices"]
        assert 0 < len(d["rows"]) <= 5
        assert [row[1] for row in d["rows"]] == sorted([row[1] for row in d["rows"]], reverse=True)

    def test_ask_caps_rows(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_translate_question", lambda q: {
            "table": "t4_notices", "filters": [], "group_by": [],
            "aggregates": [], "sort": [], "max_count": 100000,
        })
        r = client.post("/api/ask", json={"question": "everything"})
        # raw (non-aggregated) query, capped at EXPLORE_MAX_ROWS
        assert r.status_code == 200 and r.json()["row_count"] <= 500

    def test_ask_invalid_generated_query_is_422_with_generated(self, monkeypatch):
        import main
        bad = {"table": "t4_notices", "filters": [], "group_by": ["not_a_field"],
               "aggregates": [], "sort": [], "max_count": 10}
        monkeypatch.setattr(main, "_translate_question", lambda q: bad)
        r = client.post("/api/ask", json={"question": "huh"})
        assert r.status_code == 422
        assert r.json()["detail"]["generated"] == bad  # surfaces the model's attempt

    def test_ask_llm_failure_is_502(self, monkeypatch):
        import main
        def boom(q):
            raise RuntimeError("model unavailable")
        monkeypatch.setattr(main, "_translate_question", boom)
        assert client.post("/api/ask", json={"question": "x"}).status_code == 502

    def test_ask_disabled_is_503(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "NL_QUERY_ENABLED", False)
        assert client.post("/api/ask", json={"question": "x"}).status_code == 503

    def test_ask_caches_translation(self, monkeypatch):
        import main
        calls = {"n": 0}
        def counting(q):
            calls["n"] += 1
            return {"table": "t4_notices", "filters": [], "group_by": ["platform"],
                    "aggregates": [{"function": "SUM", "field": "notices", "alias": "v"}],
                    "sort": [{"field": "v", "order": "desc"}], "max_count": 3}
        monkeypatch.setattr(main, "_translate_question", counting)
        r1 = client.post("/api/ask", json={"question": "Top platforms?"})
        r2 = client.post("/api/ask", json={"question": "  top   PLATFORMS? "})  # same, normalized
        assert r1.status_code == 200 and r2.status_code == 200
        assert calls["n"] == 1  # second served from cache
        assert r1.json()["cached"] is False and r2.json()["cached"] is True

    def test_ask_reports_truncated(self):
        r = client.post("/api/ask", json={"question": "top platforms by notices?"})
        assert "truncated" in r.json()  # cap indicator present


def test_askquery_to_request_maps_count_star():
    import main
    req = main._askquery_to_request({
        "table": "t11_qualitative", "filters": [], "group_by": ["indicator"],
        "aggregates": [{"function": "COUNT", "field": "(rows)", "alias": "n"}],
        "sort": [{"field": "n", "order": "desc"}], "max_count": 3,
    })
    assert req.aggregates[0].field_name == "*"  # COUNT (rows) → COUNT(*)
    assert req.table == "t11_qualitative"


# ── Content-Security-Policy on the served HTML pages ─────────────────────────

class TestCSP:
    def _inline_hash(self, html):
        import re, hashlib, base64
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
        assert blocks, "expected an inline <script> block"
        return "'sha256-" + base64.b64encode(hashlib.sha256(blocks[0].encode()).digest()).decode() + "'"

    def test_dashboard_csp(self):
        r = client.get("/")
        csp = r.headers.get("Content-Security-Policy")
        assert csp and "default-src 'self'" in csp
        # Chart.js is vendored same-origin, so no third-party script origin is needed.
        assert "cdn.jsdelivr.net" not in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        assert "'unsafe-inline'" not in csp.split("style-src")[0]  # no unsafe-inline for scripts
        # The inline-script hash is present, so a strict CSP won't break the page.
        assert self._inline_hash(r.text) in csp

    def test_portal_csp(self):
        r = client.get("/portal")
        csp = r.headers.get("Content-Security-Policy")
        assert "script-src 'self'" in csp and "https://accounts.google.com" in csp
        assert "frame-src https://accounts.google.com" in csp  # GSI sign-in iframe
        assert self._inline_hash(r.text) in csp


# ── Accessibility landmarks on the served HTML pages ─────────────────────────

class TestAccessibility:
    def test_dashboard_a11y_landmarks(self):
        html = client.get("/").text
        assert 'href="#main"' in html and 'class="skip-link"' in html  # skip link
        assert 'id="main"' in html                                     # main landmark
        assert 'aria-label="Primary"' in html                          # labelled nav
        assert 'role="alert"' in html                                  # error live region
        # Canvases carry an accessible table alternative, so hide them from AT.
        assert 'id="chart-platforms" height="150" aria-hidden="true"' in html

    def test_portal_a11y_landmarks(self):
        html = client.get("/portal").text
        assert 'href="#main"' in html and 'class="skip-link"' in html
        assert 'id="main"' in html
        assert 'role="alert"' in html
        assert 'type="email"' in html and 'autocomplete="email"' in html
