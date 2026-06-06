"""Smoke tests for the structured-query API.

conftest.py sets up a temp SQLite DB and env vars before main is imported.
No Redis required — the in-memory store is used automatically.
"""
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
        from main import _make_download_url, _store

        job = _submit_and_wait(COUNT_ALL)
        job_id = job["job_id"]
        owner_key = _store.get(job_id).owner_key
        # Forge a correctly-signed but already-expired link.
        expires = 1
        from main import _download_signature

        sig = _download_signature(job_id, owner_key, "json", expires)
        r = client.get(f"/jobs/{job_id}/download?format=json&expires={expires}&sig={sig}")
        assert r.status_code == 410

    def test_signature_bound_to_format(self):
        job = _submit_and_wait(COUNT_ALL)
        # Take the json URL but swap the declared format to csv — signature no longer matches.
        url = job["download_urls"]["json"].replace("format=json", "format=csv")
        assert client.get(url).status_code == 403

    def test_unknown_job_download_is_404(self):
        r = client.get("/jobs/doesnotexist/download?format=json&expires=99999999999&sig=abc")
        assert r.status_code == 404


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
