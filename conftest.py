"""Pytest configuration — runs before any test file is imported.

Builds a small VLOP-DSA-shaped SQLite DB (via seed.build_db, the same code path
the real seed uses) and sets DB_PATH/API_KEYS_JSON env vars so main.py picks
them up at module-level import time, which happens after this file is loaded.
"""
import os
import tempfile

import seed

_tmp = tempfile.mkdtemp()
_DB = os.path.join(_tmp, "test.db")

# A tiny but representative slice of the vlop-dsa.json shape: 2 services across
# 2 platforms, a couple of categories/sections/indicators/scopes/surfaces, and a
# few fact rows per report table (chosen so aggregations have known totals).
_FIXTURE = {
    "meta": {"period": "2025-07-01/2025-12-31", "generated": "2026-05-13"},
    "services": ["YouTube", "Facebook"],
    "service_platforms": ["Google", "Meta"],
    "categories": ["TOTAL", "STATEMENT_CATEGORY_ILLEGAL_OR_HARMFUL_SPEECH"],
    "category_labels": {"TOTAL": "All the entries",
                        "STATEMENT_CATEGORY_ILLEGAL_OR_HARMFUL_SPEECH": "Illegal or harmful speech"},
    "sections": ["Internal complaints mechanism"],
    "indicators": ["Number of complaints submitted to the internal-complaints mechanism", "Summary"],
    "scopes": ["Total number", "Decisions upheld"],
    "surfaces": ["All", "Ads"],
    # t3: [svc, cat, scope, orders_to_act, items, orders_to_provide_info]
    "t3": [[0, 0, 0, 11, 22, 3], [1, 0, 0, 5, 6, 1]],
    # t4: [svc, cat, notices, tf_notices, items, tf_items, median, tf_median, act_law, tf_act_law, act_tos, tf_act_tos]
    "t4": [[0, 0, 100, 10, 200, 20, 5, None, 30, 3, 70, 7],
           [0, 1, 40, 4, 80, 8, 6, None, 10, 1, 30, 3],
           [1, 0, 50, 5, 90, 9, 4, None, 20, 2, 30, 3]],
    # t5: 18 cols [svc, cat, measures, automated, 7 vis_*, 3 monetary_*, service x2, account x2]
    "t5": [[0, 0, 9, 4, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
    # t6: t5 + surface_id
    "t6": [[0, 0, 9, 4, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
    # t7/t8: [svc, section, indicator, scope, value, surface]
    "t7": [[0, 0, 0, 0, 1000, 0], [1, 0, 0, 0, 500, 0]],
    "t8": [[0, 0, 0, 0, 7, 0]],
    # t9: [svc, section, indicator, scope, value]
    "t9": [[0, 0, 0, 0, 12]],
    # t10: [svc, scope, value]
    "t10": [[0, 0, 64767887], [1, 0, 50000000]],
    # t11: [svc, indicator, value_text]
    "t11": [[0, 1, "YouTube qualitative summary text."]],
}

seed.build_db(_FIXTURE, _DB)

os.environ.setdefault("DB_PATH", _DB)
os.environ.setdefault("API_KEYS_JSON", '{"alice":{"name":"alice"},"bob":{"name":"bob"}}')
# Google sign-in config for the auth tests (token verification is monkeypatched).
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("ADMIN_EMAILS", "admin@example.com")
# Don't let the rate limiters interfere with the HTTP tests (they share one
# TestClient IP / API key). The 429 paths are exercised with isolated stores.
os.environ.setdefault("PORTAL_REGISTER_MAX_PER_WINDOW", "10000")
os.environ.setdefault("QUERY_RATE_MAX_PER_WINDOW", "100000")
os.environ.setdefault("LOG_FORMAT", "text")  # readable pytest output
# Allow webhook callbacks to loopback so the end-to-end test can hit a local
# capture server. The SSRF guard itself is unit-tested with the flag off.
os.environ.setdefault("CALLBACK_ALLOW_PRIVATE", "1")
