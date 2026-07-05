"""
Build the ClinicalTrials.gov source-local cancer trials database from AACT flat files.

AACT flat files are pipe-delimited text files. This builder supports either:
- --aact-zip path/to/export_ctgov.zip
- --aact-dir path/to/extracted_aact_directory

The import is resumable:
- each AACT table scan records progress in source_build_state
- retained cancer candidates are saved in aact_candidate_trials
- registry loading records progress in source_build_state under aact_load:registry
- reruns continue unless --reset is used

No local absolute paths are hard-coded.
"""

from __future__ import annotations

import argparse
import builtins
import csv
import json
import sqlite3
import sys
import time
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_source_db as common  # noqa: E402
import preprocess_browse_conditions as mesh_preprocess  # noqa: E402

csv.field_size_limit(1024 * 1024 * 100)

SOURCE_NAME = "aact"
PROGRESS_LOG_HANDLE: Any | None = None


def configure_progress_log(path: Path | None) -> None:
    global PROGRESS_LOG_HANDLE
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_LOG_HANDLE = path.open("a", encoding="utf-8", buffering=1)
    emit(f"progress_log={path}")


def close_progress_log() -> None:
    global PROGRESS_LOG_HANDLE
    if PROGRESS_LOG_HANDLE:
        PROGRESS_LOG_HANDLE.close()
        PROGRESS_LOG_HANDLE = None


def emit(*args: Any, **kwargs: Any) -> None:
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    kwargs.pop("flush", None)
    builtins.print(*args, sep=sep, end=end, flush=True, **kwargs)
    if PROGRESS_LOG_HANDLE:
        PROGRESS_LOG_HANDLE.write(sep.join(str(arg) for arg in args) + end)
        PROGRESS_LOG_HANDLE.flush()

SCAN_SPECS = {
    "studies.txt": ("study", ["brief_title", "official_title"]),
    "conditions.txt": ("conditions", ["name"]),
    "browse_conditions.txt": ("browse_conditions", ["mesh_term", "downcase_mesh_term"]),
    "keywords.txt": ("keywords", ["name"]),
    "brief_summaries.txt": ("summary", ["description"]),
    "detailed_descriptions.txt": ("description", ["description"]),
    "interventions.txt": ("interventions", ["name", "description", "intervention_type"]),
    "eligibilities.txt": ("eligibility", ["criteria"]),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aact-zip", type=Path, help="AACT exported zip file.")
    group.add_argument("--aact-dir", type=Path, help="Directory containing extracted AACT txt files.")
    parser.add_argument("--out", type=Path, default=common.DEFAULT_DB, help="Source-local SQLite DB output path.")
    parser.add_argument("--batch-size", type=int, default=5000, help="Rows/trials per transaction checkpoint.")
    parser.add_argument("--reset", action="store_true", help="Delete output DB before building.")
    parser.add_argument("--scan-only", action="store_true", help="Only scan AACT files, classify recall tiers, and build candidate set.")
    parser.add_argument("--recall-only", dest="scan_only", action="store_true", help="Alias for --scan-only.")
    parser.add_argument("--low-confidence-policy", choices=["include", "exclude", "reviewed-only"], default="include", help="How low-confidence candidates are handled when loading registry rows.")
    parser.add_argument("--no-count-totals", action="store_true", help="Skip pre-counting AACT rows; progress will omit remaining/ETA totals.")
    parser.add_argument("--progress-log", type=Path, help="Append real-time progress lines to this log file.")
    parser.add_argument("--skip-mesh-preprocess", action="store_true", help="Skip pre-LLM deterministic rule preprocessing before review/load.")
    return parser.parse_args(argv)


class AACTFiles:
    def __init__(self, zip_path: Path | None = None, dir_path: Path | None = None) -> None:
        self.zip_path = zip_path
        self.dir_path = dir_path
        self._zip: zipfile.ZipFile | None = None
        self._members: dict[str, str] = {}
        if zip_path:
            self._zip = zipfile.ZipFile(zip_path)
            self._members = {Path(name).name: name for name in self._zip.namelist() if name.endswith(".txt")}
        elif dir_path:
            self._members = {path.name: str(path) for path in dir_path.rglob("*.txt")}

    def exists(self, filename: str) -> bool:
        return filename in self._members

    @contextmanager
    def open_text(self, filename: str) -> Iterator[Any]:
        if filename not in self._members:
            raise FileNotFoundError(filename)
        member = self._members[filename]
        if self._zip:
            with self._zip.open(member, "r") as raw:
                import io

                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                try:
                    yield text
                finally:
                    text.detach()
        else:
            with open(member, "r", encoding="utf-8", newline="") as text:
                yield text

    def close(self) -> None:
        if self._zip:
            self._zip.close()


def init_aact_tables(conn: sqlite3.Connection) -> None:
    common.init_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aact_candidate_trials (
            nct_id TEXT PRIMARY KEY,
            recall_source TEXT NOT NULL,
            cancer_recall_confidence TEXT NOT NULL,
            recall_tier TEXT NOT NULL DEFAULT 'low',
            needs_review INTEGER NOT NULL DEFAULT 1,
            llm_review_status TEXT NOT NULL DEFAULT 'not_required',
            llm_keep INTEGER,
            llm_reason TEXT,
            llm_reviewed_at TEXT,
            matched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS llm_review_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            model TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            kept_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aact_file_stats (
            filename TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL,
            counted_at TEXT NOT NULL
        );
        """
    )
    # Migrate older checkpoint databases created before recall tier / LLM review.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(aact_candidate_trials)")}
    migrations = {
        "recall_tier": "ALTER TABLE aact_candidate_trials ADD COLUMN recall_tier TEXT NOT NULL DEFAULT 'low'",
        "needs_review": "ALTER TABLE aact_candidate_trials ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 1",
        "llm_review_status": "ALTER TABLE aact_candidate_trials ADD COLUMN llm_review_status TEXT NOT NULL DEFAULT 'not_required'",
        "llm_keep": "ALTER TABLE aact_candidate_trials ADD COLUMN llm_keep INTEGER",
        "llm_reason": "ALTER TABLE aact_candidate_trials ADD COLUMN llm_reason TEXT",
        "llm_reviewed_at": "ALTER TABLE aact_candidate_trials ADD COLUMN llm_reviewed_at TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_tier = CASE
                WHEN llm_review_status IN ('rejected', 'error') THEN recall_tier
                WHEN recall_source LIKE 'study:%'
                  OR recall_source LIKE 'conditions:%'
                  OR recall_source LIKE 'browse_conditions_mesh_malignant:%'
                  OR recall_source LIKE 'browse_conditions_mesh_precancer:%'
                  OR recall_source LIKE 'intervention_rule_kept:%'
                THEN 'high'
                WHEN recall_source LIKE 'interventions:%' THEN 'medium'
                ELSE recall_tier
            END,
            needs_review = CASE
                WHEN llm_review_status IN ('kept', 'rejected', 'error') THEN 0
                WHEN recall_source LIKE 'study:%'
                  OR recall_source LIKE 'conditions:%'
                  OR recall_source LIKE 'browse_conditions_mesh_malignant:%'
                  OR recall_source LIKE 'browse_conditions_mesh_precancer:%'
                  OR recall_source LIKE 'intervention_rule_kept:%'
                THEN 0
                ELSE needs_review
            END,
            llm_review_status = CASE
                WHEN llm_review_status IN ('kept', 'rejected', 'error') THEN llm_review_status
                WHEN recall_source LIKE 'study:%'
                  OR recall_source LIKE 'conditions:%'
                  OR recall_source LIKE 'browse_conditions_mesh_malignant:%'
                  OR recall_source LIKE 'browse_conditions_mesh_precancer:%'
                  OR recall_source LIKE 'intervention_rule_kept:%'
                THEN 'not_required'
                ELSE llm_review_status
            END
        """
    )
    conn.commit()


def state_row(conn: sqlite3.Connection, key: str, label: str) -> sqlite3.Row:
    return common.get_state(conn, key, label)


def row_text(row: dict[str, str], columns: list[str]) -> str:
    return " | ".join([row.get(col, "") or "" for col in columns])


def match_text(text: str, matcher: common.TermMatcher) -> str | None:
    return matcher.match(text)
def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def progress_line(stage: str, current: int, total: int | None, retained: int, started: float) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    rate = current / elapsed
    if total and current:
        pct = current / total * 100
        eta = (total - current) / rate if rate else None
        return (
            f"[{stage}] {current:,}/{total:,} ({pct:.1f}%) "
            f"retained={retained:,} speed={rate:,.0f}/s eta={format_duration(eta)}"
        )
    return f"[{stage}] {current:,} retained={retained:,} speed={rate:,.0f}/s elapsed={format_duration(elapsed)}"




TIER_PRIORITY = {"low": 1, "medium": 2, "high": 3}


def classify_recall(source_label: str, term: str) -> tuple[str, int, str]:
    if source_label in {"study", "conditions"}:
        return "high", 0, "not_required"
    if source_label == "interventions":
        return "medium", 1, "pending"
    return "low", 1, "pending"


def upsert_candidate(conn: sqlite3.Connection, nct_id: str, recall_source: str, confidence: str, tier: str, needs_review: int, review_status: str) -> None:
    existing = conn.execute(
        "SELECT recall_tier FROM aact_candidate_trials WHERE nct_id = ?", (nct_id,)
    ).fetchone()
    if existing and TIER_PRIORITY.get(existing["recall_tier"], 0) > TIER_PRIORITY.get(tier, 0):
        return
    conn.execute(
        """
        INSERT INTO aact_candidate_trials (
            nct_id, recall_source, cancer_recall_confidence, recall_tier, needs_review,
            llm_review_status, matched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(nct_id) DO UPDATE SET
            recall_source = excluded.recall_source,
            cancer_recall_confidence = excluded.cancer_recall_confidence,
            recall_tier = excluded.recall_tier,
            needs_review = excluded.needs_review,
            llm_review_status = CASE
                WHEN excluded.needs_review = 0 THEN 'not_required'
                WHEN aact_candidate_trials.llm_review_status IN ('kept', 'rejected', 'error') THEN aact_candidate_trials.llm_review_status
                ELSE excluded.llm_review_status
            END,
            matched_at = excluded.matched_at
        """,
        (nct_id, recall_source, confidence, tier, needs_review, review_status, common.utc_now()),
    )

def count_rows(conn: sqlite3.Connection, files: AACTFiles, filename: str, skip_count: bool = False) -> int | None:
    if skip_count or not files.exists(filename):
        return None
    row = conn.execute("SELECT row_count FROM aact_file_stats WHERE filename = ?", (filename,)).fetchone()
    if row:
        return int(row[0])
    started = time.monotonic()
    count = 0
    with files.open_text(filename) as handle:
        reader = csv.reader(handle, delimiter="|")
        next(reader, None)
        for count, _ in enumerate(reader, start=1):
            if count % 500000 == 0:
                emit(progress_line(f"count {filename}", count, None, 0, started), flush=True)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO aact_file_stats (filename, row_count, counted_at) VALUES (?, ?, ?)",
            (filename, count, common.utc_now()),
        )
    return count


def scan_file(
    conn: sqlite3.Connection,
    files: AACTFiles,
    filename: str,
    source_label: str,
    columns: list[str],
    matcher: common.TermMatcher,
    batch_size: int,
    count_totals: bool = True,
) -> None:
    if not files.exists(filename):
        emit(f"skip missing {filename}")
        return
    key = f"aact_scan:{filename}"
    state = state_row(conn, key, filename)
    if state["is_complete"]:
        emit(f"skip complete {filename}")
        return
    start_offset = int(state["processed_count"] or 0)
    total_rows = count_rows(conn, files, filename, skip_count=not count_totals)
    processed_since_commit = 0
    retained_since_commit = 0
    total_retained_this_run = 0
    current_row = 0
    started_at = common.utc_now()
    progress_started = time.monotonic()
    if start_offset:
        emit(f"resume {filename}: processed={start_offset:,} total={total_rows if total_rows is not None else 'unknown'}", flush=True)
    with files.open_text(filename) as handle:
        reader = csv.DictReader(handle, delimiter="|")
        for row in reader:
            current_row += 1
            if current_row <= start_offset:
                continue
            nct_id = row.get("nct_id")
            if not nct_id:
                continue
            processed_since_commit += 1
            text = row_text(row, columns)
            term = match_text(text, matcher)
            if term:
                retained_since_commit += 1
                total_retained_this_run += 1
                tier, needs_review, review_status = classify_recall(source_label, term)
                confidence = "high" if tier == "high" else "medium" if tier == "medium" else "low"
                upsert_candidate(
                    conn,
                    nct_id,
                    f"{source_label}:{term}",
                    confidence,
                    tier,
                    needs_review,
                    review_status,
                )
            if processed_since_commit >= batch_size:
                with conn:
                    conn.execute(
                        """
                        UPDATE source_build_state
                        SET processed_count = ?, retained_count = retained_count + ?,
                            batch_count = batch_count + 1, next_page_token = ?, updated_at = ?
                        WHERE query_key = ?
                        """,
                        (current_row, retained_since_commit, str(current_row), common.utc_now(), key),
                    )
                    conn.execute(
                        """
                        INSERT INTO source_batches (
                            query_key, query_term, request_url, fetched_at, processed_count,
                            retained_count, next_page_token, status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'success')
                        """,
                        (key, filename, filename, started_at, processed_since_commit, retained_since_commit, str(current_row)),
                    )
                emit(progress_line(f"scan {filename}", current_row, total_rows, total_retained_this_run, progress_started), flush=True)
                processed_since_commit = 0
                retained_since_commit = 0
    with conn:
        conn.execute(
            """
            UPDATE source_build_state
            SET processed_count = ?, retained_count = retained_count + ?, batch_count = batch_count + 1,
                is_complete = 1, next_page_token = NULL, last_error = NULL, updated_at = ?
            WHERE query_key = ?
            """,
            (current_row, retained_since_commit, common.utc_now(), key),
        )
        conn.execute(
            """
            INSERT INTO source_batches (
                query_key, query_term, request_url, fetched_at, processed_count,
                retained_count, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'success')
            """,
            (key, filename, filename, started_at, processed_since_commit, retained_since_commit),
        )
    emit(progress_line(f"scan {filename} complete", current_row, total_rows, total_retained_this_run, progress_started), flush=True)
    emit(f"scanned {filename}: rows={current_row:,}", flush=True)


def scan_candidates(conn: sqlite3.Connection, files: AACTFiles, batch_size: int, count_totals: bool = True) -> None:
    terms = common.load_terms()
    matcher = common.TermMatcher(terms)
    emit(f"loaded_recall_terms={len(terms)}", flush=True)
    for filename, (source_label, columns) in SCAN_SPECS.items():
        scan_file(conn, files, filename, source_label, columns, matcher, batch_size, count_totals)
    count = conn.execute("SELECT COUNT(*) FROM aact_candidate_trials").fetchone()[0]
    emit(f"aact_candidate_trials={count}", flush=True)


def print_candidate_tier_summary(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT recall_tier, needs_review, llm_review_status, COUNT(*) AS count
        FROM aact_candidate_trials
        GROUP BY recall_tier, needs_review, llm_review_status
        ORDER BY
            CASE recall_tier WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            needs_review,
            llm_review_status
        """
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM aact_candidate_trials").fetchone()[0]
    pending_review = conn.execute(
        """
        SELECT COUNT(*)
        FROM aact_candidate_trials
        WHERE needs_review = 1
          AND COALESCE(llm_review_status, 'pending') IN ('pending', 'not_required')
        """
    ).fetchone()[0]
    emit("candidate recall tier summary:", flush=True)
    emit(f"  total={total:,}", flush=True)
    for row in rows:
        emit(
            f"  tier={row['recall_tier']} needs_review={row['needs_review']} "
            f"review_status={row['llm_review_status']} count={row['count']:,}",
            flush=True,
        )
    emit(f"  pending_llm_review={pending_review:,}", flush=True)


def candidate_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT nct_id FROM aact_candidate_trials")}


def read_rows_for_candidates(files: AACTFiles, filename: str, candidates: set[str]) -> list[dict[str, str]]:
    if not files.exists(filename):
        return []
    rows: list[dict[str, str]] = []
    with files.open_text(filename) as handle:
        reader = csv.DictReader(handle, delimiter="|")
        for row in reader:
            if row.get("nct_id") in candidates:
                rows.append(dict(row))
    return rows


def group_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        nct_id = row.get("nct_id")
        if nct_id:
            grouped[nct_id].append(row)
    return grouped


def first_text(rows: list[dict[str, str]], field: str) -> str | None:
    for row in rows:
        value = row.get(field)
        if value:
            return value
    return None


def values(rows: list[dict[str, str]], field: str) -> list[str]:
    return [row.get(field, "") for row in rows if row.get(field)]


def load_registry(conn: sqlite3.Connection, files: AACTFiles, batch_size: int, low_confidence_policy: str = "include") -> None:
    candidates = candidate_ids(conn)
    if low_confidence_policy == "exclude":
        candidates = {row[0] for row in conn.execute("SELECT nct_id FROM aact_candidate_trials WHERE recall_tier = 'high'")}
    elif low_confidence_policy == "reviewed-only":
        candidates = {row[0] for row in conn.execute("SELECT nct_id FROM aact_candidate_trials WHERE recall_tier = 'high' OR llm_keep = 1")}
    if not candidates:
        emit("no candidates to load")
        return
    emit(f"loading candidate details for {len(candidates):,} trials", flush=True)
    studies = {row["nct_id"]: row for row in read_rows_for_candidates(files, "studies.txt", candidates)}
    conditions = group_rows(read_rows_for_candidates(files, "conditions.txt", candidates))
    summaries = group_rows(read_rows_for_candidates(files, "brief_summaries.txt", candidates))
    descriptions = group_rows(read_rows_for_candidates(files, "detailed_descriptions.txt", candidates))
    interventions = group_rows(read_rows_for_candidates(files, "interventions.txt", candidates))
    eligibilities = group_rows(read_rows_for_candidates(files, "eligibilities.txt", candidates))
    facilities = group_rows(read_rows_for_candidates(files, "facilities.txt", candidates))
    sponsors = group_rows(read_rows_for_candidates(files, "sponsors.txt", candidates))
    countries = group_rows(read_rows_for_candidates(files, "countries.txt", candidates))
    candidate_meta = {
        row["nct_id"]: row
        for row in conn.execute("SELECT nct_id, recall_source, cancer_recall_confidence, recall_tier, needs_review, llm_keep FROM aact_candidate_trials")
    }

    key = "aact_load:registry"
    state = state_row(conn, key, "registry")
    if state["is_complete"]:
        emit("skip complete registry load")
        return
    start_offset = int(state["processed_count"] or 0)
    nct_ids = sorted([nct_id for nct_id in candidates if nct_id in studies])
    fetched_at = common.utc_now()
    processed_since_commit = 0
    loaded_since_commit = 0

    load_started = time.monotonic()
    for index, nct_id in enumerate(nct_ids, start=1):
        if index <= start_offset:
            continue
        study = studies[nct_id]
        meta = candidate_meta[nct_id]
        trial_uid = f"ctgov:{nct_id}"
        cond_values = values(conditions.get(nct_id, []), "name")
        intervention_rows = interventions.get(nct_id, [])
        site_rows = facilities.get(nct_id, [])
        country_values = values(countries.get(nct_id, []), "name") or sorted({row.get("country", "") for row in site_rows if row.get("country")})
        sponsor_name = first_text([row for row in sponsors.get(nct_id, []) if row.get("lead_or_collaborator") == "lead"], "name")
        sponsor_name = sponsor_name or first_text(sponsors.get(nct_id, []), "name")
        criteria_text = first_text(eligibilities.get(nct_id, []), "criteria")
        raw_payload: dict[str, Any] = {
            "study": study,
            "conditions": conditions.get(nct_id, []),
            "brief_summaries": summaries.get(nct_id, []),
            "detailed_descriptions": descriptions.get(nct_id, []),
            "interventions": intervention_rows,
            "eligibilities": eligibilities.get(nct_id, []),
            "facilities": site_rows,
            "sponsors": sponsors.get(nct_id, []),
            "countries": countries.get(nct_id, []),
        }
        conn.execute(
            """
            INSERT INTO raw_trial_records (
                source_name, source_trial_id, source_url, raw_json, fetched_at, parser_version, fetch_status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'success')
            """,
            (SOURCE_NAME, nct_id, common.source_url(nct_id), json.dumps(raw_payload, ensure_ascii=False), fetched_at, common.SCHEMA_VERSION),
        )
        conn.execute(
            """
            INSERT INTO trial_master (
                trial_uid, primary_registry_id, primary_source, title, scientific_title, brief_summary,
                recruitment_status_raw, recruitment_status_normalized, phase_raw, phase_normalized,
                study_type_raw, study_type_normalized, disease_text, intervention_summary, sponsor_summary,
                countries, registration_date, start_date, completion_date, last_update_date, source_url,
                last_fetched_at, cancer_recall_source, cancer_recall_confidence, data_quality_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unreviewed')
            ON CONFLICT(trial_uid) DO UPDATE SET
                title = excluded.title,
                scientific_title = excluded.scientific_title,
                brief_summary = excluded.brief_summary,
                recruitment_status_raw = excluded.recruitment_status_raw,
                recruitment_status_normalized = excluded.recruitment_status_normalized,
                phase_raw = excluded.phase_raw,
                phase_normalized = excluded.phase_normalized,
                study_type_raw = excluded.study_type_raw,
                study_type_normalized = excluded.study_type_normalized,
                disease_text = excluded.disease_text,
                intervention_summary = excluded.intervention_summary,
                sponsor_summary = excluded.sponsor_summary,
                countries = excluded.countries,
                registration_date = excluded.registration_date,
                start_date = excluded.start_date,
                completion_date = excluded.completion_date,
                last_update_date = excluded.last_update_date,
                last_fetched_at = excluded.last_fetched_at,
                cancer_recall_source = excluded.cancer_recall_source,
                cancer_recall_confidence = excluded.cancer_recall_confidence
            """,
            (
                trial_uid,
                nct_id,
                "clinicaltrials.gov",
                study.get("brief_title"),
                study.get("official_title"),
                first_text(summaries.get(nct_id, []), "description"),
                study.get("overall_status"),
                common.normalize_status(study.get("overall_status")),
                study.get("phase"),
                study.get("phase", "").lower().replace(" ", "_") if study.get("phase") else None,
                study.get("study_type"),
                study.get("study_type", "").lower() if study.get("study_type") else None,
                common.join_text(cond_values),
                common.join_text(values(intervention_rows, "name")),
                sponsor_name,
                common.join_text(country_values),
                study.get("study_first_submitted_date"),
                study.get("start_date"),
                study.get("completion_date"),
                study.get("last_update_submitted_date"),
                common.source_url(nct_id),
                fetched_at,
                meta["recall_source"],
                meta["cancer_recall_confidence"],
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO trial_registry_ids (trial_uid, registry_source, registry_id, id_type, is_primary, source_url)
            VALUES (?, 'clinicaltrials.gov', ?, 'primary', 1, ?)
            """,
            (trial_uid, nct_id, common.source_url(nct_id)),
        )
        conn.execute("DELETE FROM trial_interventions WHERE trial_uid = ?", (trial_uid,))
        for row in intervention_rows:
            conn.execute(
                """
                INSERT INTO trial_interventions (trial_uid, intervention_name_raw, intervention_name_normalized, intervention_type)
                VALUES (?, ?, ?, ?)
                """,
                (trial_uid, row.get("name"), row.get("name"), row.get("intervention_type")),
            )
        conn.execute("DELETE FROM trial_eligibility_criteria WHERE trial_uid = ?", (trial_uid,))
        for order, (kind, text) in enumerate(common.split_criteria(criteria_text), start=1):
            conn.execute(
                """
                INSERT INTO trial_eligibility_criteria (trial_uid, criterion_type, criterion_text, language, criterion_order, source_section)
                VALUES (?, ?, ?, 'en', ?, 'criteria')
                """,
                (trial_uid, kind, text, order),
            )
        conn.execute("DELETE FROM trial_sites WHERE trial_uid = ?", (trial_uid,))
        for row in site_rows:
            conn.execute(
                """
                INSERT INTO trial_sites (trial_uid, site_name, country, province, city, site_status, source_name, source_url)
                VALUES (?, ?, ?, ?, ?, ?, 'clinicaltrials.gov', ?)
                """,
                (trial_uid, row.get("name"), row.get("country"), row.get("state"), row.get("city"), row.get("status"), common.source_url(nct_id)),
            )
        processed_since_commit += 1
        loaded_since_commit += 1
        if processed_since_commit >= batch_size:
            with conn:
                conn.execute(
                    """
                    UPDATE source_build_state
                    SET processed_count = ?, retained_count = retained_count + ?, batch_count = batch_count + 1,
                        next_page_token = ?, updated_at = ?
                    WHERE query_key = ?
                    """,
                    (index, loaded_since_commit, str(index), common.utc_now(), key),
                )
            emit(progress_line("load registry", index, len(nct_ids), loaded_since_commit, load_started), flush=True)
            processed_since_commit = 0
            loaded_since_commit = 0
    with conn:
        conn.execute(
            """
            UPDATE source_build_state
            SET processed_count = ?, retained_count = retained_count + ?, batch_count = batch_count + 1,
                is_complete = 1, next_page_token = NULL, last_error = NULL, updated_at = ?
            WHERE query_key = ?
            """,
            (len(nct_ids), loaded_since_commit, common.utc_now(), key),
        )
    emit(progress_line("load registry complete", len(nct_ids), len(nct_ids), loaded_since_commit, load_started), flush=True)
    emit(f"loaded registry trials={len(nct_ids):,}", flush=True)


def print_summary(conn: sqlite3.Connection, db_path: Path) -> None:
    emit(f"database={db_path}")
    for table in [
        "aact_candidate_trials",
        "raw_trial_records",
        "trial_master",
        "trial_registry_ids",
        "trial_interventions",
        "trial_eligibility_criteria",
        "trial_sites",
        "source_build_state",
        "source_batches",
    ]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        emit(f"{table}={count}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_progress_log(args.progress_log)
    conn = common.connect_db(args.out, reset=args.reset)
    files = AACTFiles(zip_path=args.aact_zip, dir_path=args.aact_dir)
    try:
        init_aact_tables(conn)
        emit("stage=recall_and_tiering", flush=True)
        scan_candidates(conn, files, args.batch_size, not args.no_count_totals)
        if not args.skip_mesh_preprocess:
            emit("stage=pre_llm_rule_preprocess", flush=True)
            summary = mesh_preprocess.apply_pre_llm_rules(conn, files)
            for category in [
                "browse_malignant_or_cancer_specific",
                "browse_precancer_or_prevention",
                "browse_broad_or_ambiguous_review",
                "browse_benign_or_non_oncology_exclude",
                "browse_unclassified_review",
                "browse_strong_promoted_from_pending",
                "eligibility_only_rule_rejected",
                "eligibility_only_protected_by_anchor",
                "eligibility_only_protected_by_browse",
            ]:                emit(f"  {category}={summary.get(category, 0):,}", flush=True)
        print_candidate_tier_summary(conn)
        if not args.scan_only:
            emit(f"stage=load_registry low_confidence_policy={args.low_confidence_policy}", flush=True)
            load_registry(conn, files, args.batch_size, args.low_confidence_policy)
        else:
            emit("stage=load_registry skipped (--scan-only/--recall-only)", flush=True)
        print_summary(conn, args.out)
    finally:
        files.close()
        conn.close()
        close_progress_log()


if __name__ == "__main__":
    main(sys.argv[1:])














