from __future__ import annotations

import sqlite3
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT.parents[1]
SCHEMA = PROJECT_ROOT / "schemas" / "registry_schema.sql"

EXPECTED_OBJECTS = {
    "raw_trial_records",
    "trial_master",
    "trial_registry_ids",
    "trial_interventions",
    "trial_eligibility_criteria",
    "trial_sites",
    "skill_trial_candidates_view",
    "trial_matching_features_view",
}


def test_schema_creates_expected_objects() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        names = {row[0] for row in rows}
        missing = EXPECTED_OBJECTS - names
        assert not missing, f"Missing schema objects: {sorted(missing)}"
    finally:
        conn.close()


if __name__ == "__main__":
    test_schema_creates_expected_objects()
    print("schema smoke test passed")

