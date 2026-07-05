"""
Monitor AACT source-local database build progress.

Example:
    python sources/clinicaltrials_gov/scripts/monitor_aact_build.py --watch --interval 30
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = SOURCE_ROOT / "data" / "ctgov_cancer_trials.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=30.0)
    return parser.parse_args()


def fmt(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def safe_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def stage_eta(conn: sqlite3.Connection, key: str, processed: int, total: int | None) -> tuple[float | None, float | None]:
    if not total or processed <= 0:
        return None, None
    row = conn.execute(
        """
        SELECT MIN(fetched_at), MAX(fetched_at), SUM(processed_count)
        FROM source_batches
        WHERE query_key = ? AND status = 'success'
        """,
        (key,),
    ).fetchone()
    if not row or not row[0] or not row[1] or not row[2]:
        return None, None
    try:
        from datetime import datetime

        start = datetime.fromisoformat(row[0])
        end = datetime.fromisoformat(row[1])
        elapsed = max((end - start).total_seconds(), 1.0)
        rate = float(row[2]) / elapsed
        eta = (total - processed) / rate if rate > 0 and total >= processed else 0.0
        return rate, eta
    except Exception:
        return None, None


def print_once(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        print(f"database={db}")
        print("counts:")
        for table in [
            "aact_candidate_trials",
            "raw_trial_records",
            "trial_master",
            "trial_interventions",
            "trial_eligibility_criteria",
            "trial_sites",
        ]:
            count = safe_count(conn, table)
            if count is not None:
                print(f"  {table}: {count:,}")
        print("stages:")
        rows = conn.execute(
            """
            SELECT query_key, query_term, processed_count, retained_count, batch_count, is_complete, updated_at, last_error
            FROM source_build_state
            ORDER BY query_key
            """
        ).fetchall()
        stats = {
            row["filename"]: row["row_count"]
            for row in conn.execute("SELECT filename, row_count FROM aact_file_stats")
        } if safe_count(conn, "aact_file_stats") is not None else {}
        for row in rows:
            key = row["query_key"]
            term = row["query_term"]
            total = stats.get(term)
            processed = int(row["processed_count"] or 0)
            remaining = total - processed if total is not None else None
            pct = f"{processed / total * 100:.1f}%" if total else "unknown"
            rate, eta = stage_eta(conn, key, processed, total)
            status = "complete" if row["is_complete"] else "running/pending"
            print(
                f"  {key}: {status} processed={processed:,} total={total if total is not None else 'unknown'} "
                f"remaining={remaining if remaining is not None else 'unknown'} pct={pct} "
                f"retained={int(row['retained_count'] or 0):,} batches={int(row['batch_count'] or 0):,} "
                f"eta={fmt(eta)} updated={row['updated_at']}"
            )
            if row["last_error"]:
                print(f"    last_error={row['last_error']}")
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    while True:
        print_once(args.db)
        if not args.watch:
            break
        print("---")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
