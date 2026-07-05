"""Import JSONL review results produced by the current Codex/Claude conversation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_aact_source_db as aact  # noqa: E402
import build_source_db as common  # noqa: E402
import review_low_confidence_candidates as review  # noqa: E402

VALID_CONFIDENCE = {"high", "medium", "low"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=common.DEFAULT_DB)
    parser.add_argument("--results", type=Path, required=True, help="JSONL file with one review decision per line.")
    parser.add_argument("--batch", type=Path, help="Optional exported batch JSON. Used to validate expected NCT IDs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-name", default="conversation")
    return parser.parse_args(argv)


def read_expected_ids(batch_path: Path | None) -> set[str] | None:
    if not batch_path:
        return None
    payload = json.loads(batch_path.read_text(encoding="utf-8-sig"))
    return {item["nct_id"] for item in payload.get("candidates", [])}


def load_results(path: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc
        nct_id = item.get("nct_id")
        if not isinstance(nct_id, str) or not nct_id.startswith("NCT"):
            raise ValueError(f"Invalid nct_id on line {line_no}: {nct_id!r}")
        if not isinstance(item.get("keep"), bool):
            raise ValueError(f"Invalid keep boolean on line {line_no}: {item.get('keep')!r}")
        confidence = item.get("confidence")
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"Invalid confidence on line {line_no}: {confidence!r}")
        item["reason"] = str(item.get("reason") or "")[:1000]
        item["evidence"] = str(item.get("evidence") or "")[:500]
        results.append(item)
    seen: set[str] = set()
    duplicates = [item["nct_id"] for item in results if item["nct_id"] in seen or seen.add(item["nct_id"])]
    if duplicates:
        raise ValueError(f"Duplicate nct_id results: {duplicates[:10]}")
    return results


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    expected = read_expected_ids(args.batch)
    results = load_results(args.results)
    result_ids = {item["nct_id"] for item in results}
    if expected is not None:
        missing = sorted(expected - result_ids)
        extra = sorted(result_ids - expected)
        if missing or extra:
            raise ValueError(f"Result IDs do not match batch. missing={missing[:10]} extra={extra[:10]}")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            row["nct_id"]
            for row in conn.execute(
                "SELECT nct_id FROM aact_candidate_trials WHERE needs_review = 1 AND nct_id IN (%s)"
                % ",".join("?" for _ in results),
                [item["nct_id"] for item in results],
            )
        } if results else set()
        missing_db = sorted(result_ids - existing)
        if missing_db:
            raise ValueError(f"Results include IDs not pending review in DB: {missing_db[:10]}")
        kept = rejected = 0
        if args.dry_run:
            kept = sum(1 for item in results if item["keep"])
            rejected = len(results) - kept
            print(f"dry_run reviewed={len(results)} kept={kept} rejected={rejected}")
            return
        with conn:
            cur = conn.execute(
                "INSERT INTO llm_review_runs (source_name, model, started_at, status) VALUES (?, 'current-conversation', ?, 'running')",
                (args.source_name, common.utc_now()),
            )
            run_id = int(cur.lastrowid)
        for item in results:
            with conn:
                review.write_review(conn, item["nct_id"], item)
            if item["keep"]:
                kept += 1
            else:
                rejected += 1
        with conn:
            conn.execute(
                """
                UPDATE llm_review_runs
                SET finished_at = ?, reviewed_count = ?, kept_count = ?, rejected_count = ?, error_count = 0, status = 'complete'
                WHERE id = ?
                """,
                (common.utc_now(), len(results), kept, rejected, run_id),
            )
        print(f"imported={len(results)} kept={kept} rejected={rejected}")
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])

