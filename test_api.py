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

# A trivial valid query: grand total row count (one row, one column "n").
COUNT_ALL = {"aggregates": [{"function": "COUNT", "alias": "n"}]}


def _wait_for_job(job_id: str, headers: dict, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal state within {timeout}s")


def _submit_and_wait(query: dict, headers: dict = ALICE) -> dict:
    r = client.post("/query", json=query, headers=headers)
    assert r.status_code == 202
    return _wait_for_job(r.json()["job_id"], headers)


# ── Infrastructure ────────────────────────────────────────────────────────────

class TestInfra:
    def test_root_no_auth(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "endpoints" in r.json()

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
        r = client.post("/portal/register", json={"name": "Ada Lovelace", "email": "ada@rs.org"})
        assert r.status_code == 201
        body = r.json()
        key = body["api_key"]
        assert key.startswith("rk_")
        assert body["name"] == "Ada Lovelace"
        assert "expires_at" in body
        # The issued key authenticates real API calls.
        assert client.get("/fields", headers={"X-API-Key": key}).status_code == 200
        assert client.get("/tables", headers={"X-API-Key": key}).status_code == 200

    def test_issued_key_can_be_revoked(self):
        key = client.post(
            "/portal/register", json={"name": "Grace", "email": "grace@navy.mil"}
        ).json()["api_key"]
        hdr = {"X-API-Key": key}
        assert client.get("/fields", headers=hdr).status_code == 200
        assert client.delete("/portal/key", headers=hdr).json()["revoked"] is True
        # Revoked key no longer authenticates.
        assert client.get("/fields", headers=hdr).status_code == 401

    def test_configured_key_cannot_be_revoked(self):
        assert client.delete("/portal/key", headers=ALICE).status_code == 400

    def test_register_bad_email_is_400(self):
        r = client.post("/portal/register", json={"name": "Ada", "email": "not-an-email"})
        assert r.status_code == 400

    def test_register_whitespace_name_is_400(self):
        r = client.post("/portal/register", json={"name": "   ", "email": "ada@rs.org"})
        assert r.status_code == 400

    def test_register_missing_field_is_422(self):
        assert client.post("/portal/register", json={"name": "Ada"}).status_code == 422

    def test_unknown_issued_key_rejected(self):
        assert client.get("/fields", headers={"X-API-Key": "rk_deadbeef"}).status_code == 401

    def test_register_rate_limited(self):
        # Use an isolated store + a low limit so we exercise the 429 path without
        # polluting the shared TestClient IP bucket the other tests rely on.
        import main

        original_store, original_limit = main._key_store, main.REGISTER_MAX_PER_WINDOW
        main._key_store = main.MemoryKeyStore()
        main.REGISTER_MAX_PER_WINDOW = 3
        try:
            statuses = [
                client.post("/portal/register", json={"name": f"R{i}", "email": f"r{i}@x.org"}).status_code
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
        assert client.get("/tables").status_code == 401

    def test_bad_key_is_401(self):
        assert client.get("/tables", headers={"X-API-Key": "bogus"}).status_code == 401

    def test_valid_key_ok(self):
        r = client.get("/tables", headers=ALICE)
        assert r.status_code == 200
        assert "removals" in r.json()["tables"]


# ── Schema ────────────────────────────────────────────────────────────────────

class TestSchema:
    def test_known_table(self):
        r = client.get("/schema/removals", headers=ALICE)
        assert r.status_code == 200
        col_names = [c["name"] for c in r.json()["columns"]]
        assert "num_requests" in col_names

    def test_missing_table_is_404(self):
        assert client.get("/schema/nonexistent", headers=ALICE).status_code == 404

    def test_invalid_table_name_is_400(self):
        # Slashes are stripped by the router; test a name that reaches the handler
        # but contains characters the isalnum guard rejects.
        assert client.get("/schema/bad;name", headers=ALICE).status_code == 400


# ── Fields discovery ──────────────────────────────────────────────────────────

class TestFields:
    def test_lists_dimensions_and_measures(self):
        r = client.get("/fields", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        assert "country_code" in body["dimensions"]["fields"]
        assert "items_requested" in body["measures"]["fields"]
        assert "SUM" in body["aggregate_functions"]

    def test_fields_requires_auth(self):
        assert client.get("/fields").status_code == 401


# ── Query lifecycle ───────────────────────────────────────────────────────────

class TestQueryLifecycle:
    def test_submit_returns_202_with_location(self):
        r = client.post("/query", json=COUNT_ALL, headers=ALICE)
        assert r.status_code == 202
        assert "job_id" in r.json()
        assert r.headers.get("location", "").startswith("/jobs/")

    def test_happy_path_json(self):
        job = _submit_and_wait(COUNT_ALL)
        assert job["status"] == "done"
        r = client.get(f"/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        assert r.status_code == 200
        body = r.json()
        assert body["row_count"] == 1
        assert body["columns"] == ["n"]
        assert body["rows"][0][0] == 3  # 3 rows seeded in conftest.py

    def test_happy_path_csv(self):
        job = _submit_and_wait(
            {
                "fields": ["country_name"],
                "sort": [{"field_name": "country_name", "order": "asc"}],
            }
        )
        r = client.get(f"/jobs/{job['job_id']}/result?format=csv", headers=ALICE)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.strip().splitlines()
        assert lines[0] == "country_name"
        assert "Germany" in lines
        assert "United States" in lines

    def test_filter_group_and_aggregate(self):
        # Items requested per country, US only — exercises filter + group + agg + sort.
        job = _submit_and_wait(
            {
                "query": {
                    "and": [
                        {"operation": "EQ", "field_name": "country_code", "field_values": ["US"]}
                    ]
                },
                "group_by": ["country_name"],
                "aggregates": [
                    {"function": "SUM", "field_name": "items_requested", "alias": "items"}
                ],
                "sort": [{"field_name": "items", "order": "desc"}],
            }
        )
        assert job["status"] == "done"
        r = client.get(f"/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        body = r.json()
        assert body["columns"] == ["country_name", "items"]
        # US rows in conftest: items 100 + 30 = 130.
        assert body["rows"] == [["United States", 130]]

    def test_compiled_sql_is_parameterised(self):
        r = client.post(
            "/query",
            json={
                "query": {
                    "and": [
                        {"operation": "EQ", "field_name": "country_code", "field_values": ["US"]}
                    ]
                }
            },
            headers=ALICE,
        )
        job = _wait_for_job(r.json()["job_id"], ALICE)
        # Value is bound, not interpolated — the literal 'US' must not appear in the SQL.
        assert "?" in job["compiled_sql"]
        assert "US" not in job["compiled_sql"]

    def test_result_before_done_is_409(self):
        r = client.post("/query", json=COUNT_ALL, headers=ALICE)
        job_id = r.json()["job_id"]
        # Job may already be done by the time we hit the result endpoint,
        # but if it's still in-flight we expect 409.
        r2 = client.get(f"/jobs/{job_id}/result", headers=ALICE)
        assert r2.status_code in (200, 409)

    def test_list_jobs(self):
        _submit_and_wait(COUNT_ALL)
        r = client.get("/jobs", headers=ALICE)
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
        r = client.get(f"/jobs/{job_id}/download?format=json&expires={expires}&sig={sig}")
        assert r.status_code == 410

    def test_signature_bound_to_format(self):
        job = _submit_and_wait(COUNT_ALL)
        # Take the json URL but swap the declared format to csv — signature no longer matches.
        url = job["download_urls"]["json"].replace("format=json", "format=csv")
        assert client.get(url).status_code == 403

    def test_unknown_job_download_is_403(self):
        # Signature is verified before any store lookup, so an unknown job id with
        # an invalid signature returns 403 (not 404) — existence isn't leaked.
        r = client.get("/jobs/doesnotexist/download?format=json&expires=99999999999&sig=abc")
        assert r.status_code == 403


# ── Job isolation ─────────────────────────────────────────────────────────────

class TestJobIsolation:
    def test_bob_cannot_see_alices_job(self):
        job = _submit_and_wait(COUNT_ALL)
        assert client.get(f"/jobs/{job['job_id']}", headers=BOB).status_code == 404

    def test_bob_cannot_fetch_alices_result(self):
        job = _submit_and_wait(COUNT_ALL)
        r = client.get(f"/jobs/{job['job_id']}/result", headers=BOB)
        assert r.status_code == 404


# ── Safety: no arbitrary SQL ──────────────────────────────────────────────────

class TestSafety:
    def test_unknown_field_is_400(self):
        r = client.post(
            "/query",
            json={"query": {"and": [{"operation": "EQ", "field_name": "secrets", "field_values": ["x"]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_comparator_on_text_field_is_400(self):
        r = client.post(
            "/query",
            json={"query": {"and": [{"operation": "GT", "field_name": "country_name", "field_values": [5]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_bad_alias_is_400(self):
        r = client.post(
            "/query",
            json={"aggregates": [{"function": "SUM", "field_name": "num_requests", "alias": "x); DROP"}]},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_injection_value_is_treated_as_data(self):
        # A SQL-looking string in field_values is bound as a parameter, so the
        # query runs successfully and simply matches nothing — it is never code.
        job = _submit_and_wait(
            {
                "query": {
                    "and": [
                        {
                            "operation": "EQ",
                            "field_name": "country_code",
                            "field_values": ["US'; DROP TABLE countries;--"],
                        }
                    ]
                }
            }
        )
        assert job["status"] == "done"
        r = client.get(f"/jobs/{job['job_id']}/result?format=json", headers=ALICE)
        assert r.json()["row_count"] == 0
        # Table still intact afterwards.
        assert client.get("/tables", headers=ALICE).json()["tables"].count("countries") == 1

    def test_dimension_requires_string(self):
        # A numeric value on a TEXT dimension would silently match nothing under
        # SQLite affinity rules, so it is rejected up front.
        r = client.post(
            "/query",
            json={"query": {"and": [{"operation": "EQ", "field_name": "country_code", "field_values": [123]}]}},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_duplicate_group_by_is_400(self):
        r = client.post(
            "/query",
            json={
                "group_by": ["country_name", "country_name"],
                "aggregates": [{"function": "COUNT", "alias": "n"}],
            },
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_alias_clashing_with_group_by_is_400(self):
        r = client.post(
            "/query",
            json={
                "group_by": ["country_name"],
                "aggregates": [{"function": "SUM", "field_name": "num_requests", "alias": "country_name"}],
            },
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_duplicate_field_is_400(self):
        r = client.post(
            "/query",
            json={"fields": ["country_name", "country_name"]},
            headers=ALICE,
        )
        assert r.status_code == 400

    def test_no_sql_field_accepted(self):
        # The old free-form `sql` field is gone; sending it yields an empty
        # (default) query rather than executing arbitrary SQL.
        r = client.post("/query", json={"sql": "DROP TABLE countries"}, headers=ALICE)
        assert r.status_code == 202
        job = _wait_for_job(r.json()["job_id"], ALICE)
        assert job["status"] == "done"  # ran the default SELECT, not the DROP

    def test_unknown_job_is_404(self):
        assert client.get("/jobs/doesnotexist", headers=ALICE).status_code == 404


# ── Cancel / delete ───────────────────────────────────────────────────────────

class TestDelete:
    def test_delete_completed_job(self):
        job = _submit_and_wait(COUNT_ALL)
        job_id = job["job_id"]
        r = client.delete(f"/jobs/{job_id}", headers=ALICE)
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert client.get(f"/jobs/{job_id}", headers=ALICE).status_code == 404


# ── Query rate limiting ─────────────────────────────────────────────────────────

class TestQueryRateLimit:
    def test_over_limit_returns_429_with_retry_after(self):
        import main

        original_store, original_max = main._key_store, main.QUERY_RATE_MAX
        main._key_store = main.MemoryKeyStore()  # isolated, so it doesn't affect other tests
        main.QUERY_RATE_MAX = 2
        try:
            statuses = [client.post("/query", json=COUNT_ALL, headers=ALICE).status_code for _ in range(4)]
            assert statuses == [202, 202, 429, 429]
            r = client.post("/query", json=COUNT_ALL, headers=ALICE)
            assert r.status_code == 429 and r.headers["Retry-After"] == str(main.QUERY_RATE_WINDOW)
        finally:
            main._key_store, main.QUERY_RATE_MAX = original_store, original_max

    def test_limit_is_per_key(self):
        import main

        original_store, original_max = main._key_store, main.QUERY_RATE_MAX
        main._key_store = main.MemoryKeyStore()
        main.QUERY_RATE_MAX = 1
        try:
            assert client.post("/query", json=COUNT_ALL, headers=ALICE).status_code == 202
            assert client.post("/query", json=COUNT_ALL, headers=ALICE).status_code == 429
            # bob has his own bucket and is unaffected
            assert client.post("/query", json=COUNT_ALL, headers=BOB).status_code == 202
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
        client.get(f"/jobs/{job['job_id']}", headers=ALICE)
        body = client.get("/metrics").text
        assert 'path="/jobs/{job_id}"' in body
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
            "/query", json={**COUNT_ALL, "callback_url": "ftp://example.com/hook"}, headers=ALICE
        )
        assert r.status_code == 400

    def test_missing_host_rejected_at_submit(self):
        r = client.post(
            "/query", json={**COUNT_ALL, "callback_url": "http:///nohost"}, headers=ALICE
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
