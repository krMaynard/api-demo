"""Build demo.db from the aggregated EU DSA VLOP transparency dataset.

Default source: ../krMaynard.github.io/data/vlop-dsa.json
Override with --source <path> or the SEED_SOURCE_JSON env var.
Override output with --db <path> or the DB_PATH env var.

`vlop-dsa.json` is a compact interned format: shared lookup arrays (services,
service_platforms, categories, category_labels, sections, indicators, scopes,
surfaces) plus one fact array per DSA report table (t3–t11). Each fact row is a
list whose leading values are indices into the lookup arrays (= the row id in
the corresponding dimension table) and whose remaining values are the reported
measures. We expand it into a star schema: dimension tables + one fact table per
report table, queried independently via the API's `table` selector.
"""
import argparse
import json
import os
import sqlite3
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))

_DEFAULT_SOURCE = os.getenv(
    "SEED_SOURCE_JSON",
    os.path.normpath(os.path.join(HERE, "..", "krMaynard.github.io", "data", "vlop-dsa.json")),
)
_DEFAULT_DB = os.getenv("DB_PATH", os.path.join(HERE, "demo.db"))

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

-- Shared dimension tables. id = position in the source lookup array.
CREATE TABLE services   (id INTEGER PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL);
CREATE TABLE categories (id INTEGER PRIMARY KEY, code TEXT NOT NULL, label TEXT NOT NULL);
CREATE TABLE sections   (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE indicators (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE scopes     (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE surfaces   (id INTEGER PRIMARY KEY, name TEXT NOT NULL);

-- Table 3 — Member-State orders (Art. 9 & 10), by category × scope.
CREATE TABLE t3_member_state_orders (
    service_id INTEGER NOT NULL, category_id INTEGER NOT NULL, scope_id INTEGER NOT NULL,
    orders_to_act INTEGER, items INTEGER, orders_to_provide_info INTEGER
);

-- Table 4 — Notices (Art. 16), by category, with Trusted-Flagger breakdowns.
CREATE TABLE t4_notices (
    service_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
    notices INTEGER, tf_notices INTEGER, items INTEGER, tf_items INTEGER,
    median_time INTEGER, tf_median_time INTEGER,
    actions_law INTEGER, tf_actions_law INTEGER, actions_tos INTEGER, tf_actions_tos INTEGER
);

-- Table 5 — Own-initiative actions on illegal content, by category × restriction type.
CREATE TABLE t5_own_initiative_illegal (
    service_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
    measures INTEGER, automated INTEGER,
    vis_removal INTEGER, vis_disable INTEGER, vis_demoted INTEGER, vis_age_restricted INTEGER,
    vis_interaction_restricted INTEGER, vis_labelled INTEGER, vis_other INTEGER,
    monetary_suspension INTEGER, monetary_termination INTEGER, monetary_other INTEGER,
    service_suspension INTEGER, service_termination INTEGER,
    account_suspension INTEGER, account_termination INTEGER
);

-- Table 6 — Own-initiative actions on ToS violations (same shape as t5, + surface).
CREATE TABLE t6_own_initiative_tos (
    service_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
    measures INTEGER, automated INTEGER,
    vis_removal INTEGER, vis_disable INTEGER, vis_demoted INTEGER, vis_age_restricted INTEGER,
    vis_interaction_restricted INTEGER, vis_labelled INTEGER, vis_other INTEGER,
    monetary_suspension INTEGER, monetary_termination INTEGER, monetary_other INTEGER,
    service_suspension INTEGER, service_termination INTEGER,
    account_suspension INTEGER, account_termination INTEGER,
    surface_id INTEGER NOT NULL
);

-- Table 7 — Appeals & recidivism, by section × indicator × scope × surface.
CREATE TABLE t7_appeals_recidivism (
    service_id INTEGER NOT NULL, section_id INTEGER NOT NULL, indicator_id INTEGER NOT NULL,
    scope_id INTEGER NOT NULL, value INTEGER, surface_id INTEGER NOT NULL
);

-- Table 8 — Use of automated means, by section × indicator × scope × surface.
CREATE TABLE t8_automated_means (
    service_id INTEGER NOT NULL, section_id INTEGER NOT NULL, indicator_id INTEGER NOT NULL,
    scope_id INTEGER NOT NULL, value INTEGER, surface_id INTEGER NOT NULL
);

-- Table 9 — Human resources for content moderation, by section × indicator × scope.
CREATE TABLE t9_human_resources (
    service_id INTEGER NOT NULL, section_id INTEGER NOT NULL, indicator_id INTEGER NOT NULL,
    scope_id INTEGER NOT NULL, value INTEGER
);

-- Table 10 — Average Monthly Active Recipients (AMAR), by scope.
CREATE TABLE t10_amar (service_id INTEGER NOT NULL, scope_id INTEGER NOT NULL, value INTEGER);

-- Table 11 — Qualitative description (free text), by indicator.
CREATE TABLE t11_qualitative (service_id INTEGER NOT NULL, indicator_id INTEGER NOT NULL, value_text TEXT);

CREATE INDEX idx_t3_service  ON t3_member_state_orders(service_id);
CREATE INDEX idx_t4_service  ON t4_notices(service_id);
CREATE INDEX idx_t5_service  ON t5_own_initiative_illegal(service_id);
CREATE INDEX idx_t6_service  ON t6_own_initiative_tos(service_id);
CREATE INDEX idx_t7_service  ON t7_appeals_recidivism(service_id);
CREATE INDEX idx_t8_service  ON t8_automated_means(service_id);
CREATE INDEX idx_t9_service  ON t9_human_resources(service_id);
CREATE INDEX idx_t10_service ON t10_amar(service_id);
CREATE INDEX idx_t11_service ON t11_qualitative(service_id);
"""

# fact table name → (number of columns, source JSON key)
_FACT_TABLES = {
    "t3_member_state_orders": (6, "t3"),
    "t4_notices": (12, "t4"),
    "t5_own_initiative_illegal": (18, "t5"),
    "t6_own_initiative_tos": (19, "t6"),
    "t7_appeals_recidivism": (6, "t7"),
    "t8_automated_means": (6, "t8"),
    "t9_human_resources": (5, "t9"),
    "t10_amar": (3, "t10"),
    "t11_qualitative": (3, "t11"),
}


def build_db(data: dict[str, Any], db_path: str) -> dict[str, int]:
    """Build the VLOP star schema at db_path from a parsed vlop-dsa.json dict.

    Returns a {table: row_count} summary. Rows are inserted positionally, so the
    leading lookup indices in each fact row land directly in the *_id columns.
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)

        services = data["services"]
        platforms = data["service_platforms"]
        conn.executemany(
            "INSERT INTO services (id, name, platform) VALUES (?, ?, ?)",
            [(i, services[i], platforms[i]) for i in range(len(services))],
        )

        categories = data["categories"]
        labels = data.get("category_labels", {})
        conn.executemany(
            "INSERT INTO categories (id, code, label) VALUES (?, ?, ?)",
            [(i, code, labels.get(code, code)) for i, code in enumerate(categories)],
        )

        for table, key in (("sections", "sections"), ("indicators", "indicators"),
                           ("scopes", "scopes"), ("surfaces", "surfaces")):
            conn.executemany(
                f"INSERT INTO {table} (id, name) VALUES (?, ?)",
                [(i, name) for i, name in enumerate(data[key])],
            )

        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in data.get("meta", {}).items()],
        )

        summary: dict[str, int] = {}
        for table, (ncols, key) in _FACT_TABLES.items():
            rows = data.get(key, [])
            placeholders = ", ".join(["?"] * ncols)
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            summary[table] = len(rows)

        conn.commit()
        return summary
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo.db from the VLOP DSA dataset.")
    parser.add_argument("--source", default=_DEFAULT_SOURCE, help="Path to vlop-dsa.json")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Output SQLite database path")
    args = parser.parse_args()

    with open(args.source, "r", encoding="utf-8") as f:
        data = json.load(f)

    summary = build_db(data, args.db)
    period = data.get("meta", {}).get("period", "?")
    total = sum(summary.values())
    print(
        f"Seeded {args.db}: {total} fact rows across {len(_FACT_TABLES)} report tables "
        f"for {len(data['services'])} services (period {period})."
    )
    for table, n in summary.items():
        print(f"  {table}: {n}")


if __name__ == "__main__":
    main()
