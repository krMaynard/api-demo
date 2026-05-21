#!/usr/bin/env python3
"""
Walkthrough of the api-demo SQL Query API.

Start the server first (in a separate terminal):
    uvicorn main:app --port 8000

Then run this script:
    python demo.py           # auto-advance, slight pause between steps
    python demo.py --pause   # press Enter to advance each step (live demo mode)
"""

import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
PAUSE = "--pause" in sys.argv

# ── ANSI colours ──────────────────────────────────────────────────────────────
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
RESET  = "\033[0m"

_step_count = 0


def _step(title: str) -> None:
    global _step_count
    _step_count += 1
    print(f"\n{BOLD}{CYAN}── Step {_step_count}: {title}{RESET}")
    if PAUSE:
        input(f"  {DIM}(press Enter to run){RESET} ")
    else:
        time.sleep(0.5)


def _note(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def _show_request(method: str, path: str) -> None:
    print(f"  {YELLOW}→ {method} {BASE}{path}{RESET}")


def _show_response(status: int, body: object) -> None:
    color = GREEN if status < 400 else RED
    print(f"  {color}← {status}{RESET}")
    text = json.dumps(body, indent=2)
    # Truncate very long output so the demo stays readable.
    lines = text.splitlines()
    if len(lines) > 30:
        text = "\n".join(lines[:30]) + f"\n  {DIM}… ({len(lines) - 30} more lines){RESET}"
    for line in text.splitlines():
        print(f"  {line}")
    print()


def _request(method: str, path: str, *, payload=None, key: str | None = None):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    _show_request(method, path)
    try:
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
            _show_response(r.status, body)
            return r.status, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"detail": str(e)}
        _show_response(e.code, body)
        return e.code, body


def get(path: str, **kw):
    return _request("GET", path, **kw)


def post(path: str, payload, **kw):
    return _request("POST", path, payload=payload, **kw)


def delete(path: str, **kw):
    return _request("DELETE", path, **kw)


def _poll(job_id: str, key: str) -> dict:
    """Poll /jobs/{job_id} until a terminal status is reached."""
    _note("polling until done …")
    for attempt in range(60):
        time.sleep(0.25)
        _, body = get(f"/jobs/{job_id}", key=key)
        if body.get("status") in ("done", "failed", "cancelled"):
            return body
    raise RuntimeError("Timed out waiting for job to finish.")


# ── Pre-flight check ──────────────────────────────────────────────────────────

def _check_server() -> None:
    print(f"\n{BOLD}Checking that the server is up …{RESET}")
    try:
        urllib.request.urlopen(f"{BASE}/", timeout=3)
        print(f"  {GREEN}Server is running at {BASE}{RESET}\n")
    except Exception:
        print(f"  {RED}Cannot reach {BASE}{RESET}")
        print(f"  Start it first:  uvicorn main:app --port 8000")
        sys.exit(1)


# ── Demo steps ────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "pause mode — press Enter to advance" if PAUSE else "auto mode — use --pause for live demos"
    print(f"\n{BOLD}api-demo walkthrough{RESET}  {DIM}({mode}){RESET}")
    print("=" * 60)
    _check_server()

    # 1. Root — no auth
    _step("Root endpoint (no auth required)")
    _note("Public meta-endpoint lists all routes and explains the design.")
    get("/")

    # 2. Reject unauthenticated request
    _step("Unauthenticated request → 401")
    _note("Every data endpoint requires X-API-Key. Missing key returns 401.")
    get("/tables")  # no key

    # 3. List tables
    _step("List available tables  (key=alice)")
    _note("Star schema: one removals fact table + five dimension tables.")
    get("/tables", key="alice")

    # 4. Inspect the fact table schema
    _step("Inspect the schema for the 'removals' table")
    _note("Shows column names, types, and PK/NOT NULL constraints.")
    get("/schema/removals", key="alice")

    # 5. Submit a query — the core pattern
    _step("Submit a query — POST /query returns 202 immediately")
    _note("The query runs on a background worker. We get a job_id back straight away.")
    _note("Query: top 5 countries by total items requested for removal.")
    sql_top5 = (
        "SELECT c.name, SUM(r.items_requested) AS items "
        "FROM removals r "
        "JOIN countries c ON c.id = r.country_id "
        "GROUP BY c.name ORDER BY items DESC LIMIT 5"
    )
    _, job = post("/query", {"sql": sql_top5}, key="alice")
    job_id: str = job["job_id"]
    print(f"  {DIM}job_id = {job_id}{RESET}")

    # 6. Poll for completion
    _step("Poll GET /jobs/{job_id} until status=done")
    _note("In a real client you'd sleep between polls — here we spin fast.")
    _poll(job_id, key="alice")

    # 7. Fetch result as JSON
    _step("Fetch the result as JSON")
    get(f"/jobs/{job_id}/result?format=json", key="alice")

    # 8. Job isolation
    _step("Job isolation — bob cannot see alice's job")
    _note("Foreign job IDs return 404 (not 403) so existence isn't leaked.")
    get(f"/jobs/{job_id}", key="bob")

    # 9. Read-only database rejects writes
    _step("Write attempt → job fails (SQLite is opened read-only)")
    _note("No SQL parsing needed — the DB connection itself rejects DDL/DML.")
    _, bad_job = post("/query", {"sql": "DELETE FROM countries"}, key="alice")
    bad_id: str = bad_job["job_id"]
    time.sleep(0.5)
    _, bad_final = get(f"/jobs/{bad_id}", key="alice")
    print(f"  {DIM}status={bad_final.get('status')}  error={bad_final.get('error')!r}{RESET}")

    # 10. List jobs
    _step("List all of alice's jobs")
    _note("Bob's jobs are invisible; alice sees only her own.")
    get("/jobs", key="alice")

    # 11. Cancel / clean up the failed job
    _step("Delete a finished job (also works mid-run to cancel)")
    _note("DELETE while running calls sqlite3.interrupt() to abort the query.")
    delete(f"/jobs/{bad_id}", key="alice")

    # 12. Bonus: a second query to show off the data
    _step("Bonus query: defamation requests broken down by product")
    sql_defamation = (
        "SELECT p.name AS product, SUM(r.num_requests) AS requests "
        "FROM removals r "
        "JOIN products p ON p.id = r.product_id "
        "JOIN reasons rn ON rn.id = r.reason_id "
        "WHERE rn.name = 'Defamation' "
        "GROUP BY p.name ORDER BY requests DESC"
    )
    _, j2 = post("/query", {"sql": sql_defamation}, key="alice")
    j2_id: str = j2["job_id"]
    _poll(j2_id, key="alice")
    get(f"/jobs/{j2_id}/result?format=json", key="alice")

    print(f"{BOLD}{GREEN}Demo complete!{RESET}")
    print(f"  Interactive Swagger UI: {BASE}/docs  (Authorize with key 'alice' or 'bob')")
    print()


if __name__ == "__main__":
    main()
