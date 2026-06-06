"""Pytest configuration — runs before any test file is imported.

Sets DB_PATH and API_KEYS_JSON env vars so main.py picks them up at
module-level import time, which happens after this file is loaded.
"""
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
_DB = os.path.join(_tmp, "test.db")

_conn = sqlite3.connect(_DB)
_conn.executescript("""
    CREATE TABLE periods   (id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE);
    CREATE TABLE countries (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
    CREATE TABLE requestors(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE products  (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE reasons   (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE removals (
        id            INTEGER PRIMARY KEY,
        period_id     INTEGER NOT NULL REFERENCES periods(id),
        country_id    INTEGER NOT NULL REFERENCES countries(id),
        requestor_id  INTEGER NOT NULL REFERENCES requestors(id),
        product_id    INTEGER NOT NULL REFERENCES products(id),
        reason_id     INTEGER NOT NULL REFERENCES reasons(id),
        num_requests  INTEGER NOT NULL,
        items_requested INTEGER NOT NULL,
        removed_legal   INTEGER NOT NULL,
        removed_policy  INTEGER NOT NULL,
        not_found       INTEGER NOT NULL,
        not_enough_info INTEGER NOT NULL,
        no_action       INTEGER NOT NULL,
        already_removed INTEGER NOT NULL
    );
""")
_conn.executemany("INSERT INTO periods   VALUES (?,?)", [(0,"Jan-Jun 2024"),(1,"Jul-Dec 2024")])
_conn.executemany("INSERT INTO countries VALUES (?,?,?)", [(0,"US","United States"),(1,"DE","Germany")])
_conn.executemany("INSERT INTO requestors VALUES (?,?)", [(0,"Police"),(1,"Court Order")])
_conn.executemany("INSERT INTO products  VALUES (?,?)", [(0,"YouTube"),(1,"Web Search")])
_conn.executemany("INSERT INTO reasons   VALUES (?,?)", [(0,"Defamation"),(1,"Privacy")])
_conn.executemany(
    "INSERT INTO removals (period_id,country_id,requestor_id,product_id,reason_id,"
    "num_requests,items_requested,removed_legal,removed_policy,not_found,"
    "not_enough_info,no_action,already_removed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [
        (0, 0, 0, 0, 0, 10, 100, 50, 10, 5, 5, 10, 20),
        (0, 1, 0, 1, 1,  5,  50, 20,  5, 5, 5,  5, 10),
        (1, 0, 1, 0, 0,  3,  30, 15,  5, 2, 2,  3,  3),
    ],
)
_conn.commit()
_conn.close()

os.environ.setdefault("DB_PATH", _DB)
os.environ.setdefault("API_KEYS_JSON", '{"alice":{"name":"alice"},"bob":{"name":"bob"}}')
# Don't let portal registration rate-limiting interfere with the HTTP tests
# (they all share one TestClient IP). The limiter logic is unit-tested directly.
os.environ.setdefault("PORTAL_REGISTER_MAX_PER_WINDOW", "10000")
