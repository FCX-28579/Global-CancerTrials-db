"""
Build the ClinicalTrials.gov source-local cancer trials database.

The builder is intentionally batch-oriented and resumable:
- each API page is one batch
- source_build_state stores next_page_token and counters per query
- database writes happen in a single transaction per batch
- reruns continue from the latest saved checkpoint unless --reset is used

No local absolute paths are hard-coded. External files must be passed through
CLI arguments; project files are resolved relative to this script.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1"
CTGOV_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SOURCE_ROOT.parents[1]
CONFIG_DIR = SOURCE_ROOT / "config"
DEFAULT_DB = SOURCE_ROOT / "data" / "ctgov_cancer_trials.db"
REGISTRY_SCHEMA = PROJECT_ROOT / "schemas" / "registry_schema.sql"
TERMS_FILE = CONFIG_DIR / "cancer_recall_terms.yaml"

DEFAULT_QUERIES = [
    "cancer",
    "neoplasm",
    "tumor OR tumour",
    "carcinoma",
    "sarcoma",
    "leukemia OR lymphoma OR myeloma",
    "melanoma OR glioma OR glioblastoma",
    "metastatic OR malignancy OR malignant",
    "oncology OR antineoplastic",
    "KRAS OR EGFR OR ALK OR BRAF OR HER2 OR NTRK OR RET OR MSI OR PD-L1",
    "checkpoint inhibitor OR immunotherapy OR CAR-T OR antibody-drug conjugate",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_DB, help="Source-local SQLite DB output path.")
    parser.add_argument("--page-size", type=int, default=50, help="ClinicalTrials.gov API page size.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum studies to process across all queries; 0 means no limit.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between API requests.")
    parser.add_argument("--query", action="append", help="Override default API query.term; can be repeated.")
    parser.add_argument("--reset", action="store_true", help="Delete existing output DB before building.")
    parser.add_argument("--init-only", action="store_true", help="Initialize schema and exit without fetching data.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    return parser.parse_args(argv)


def connect_db(path: Path, reset: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset and path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(REGISTRY_SCHEMA.read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_build_state (
            query_key TEXT PRIMARY KEY,
            query_term TEXT NOT NULL,
            next_page_token TEXT,
            processed_count INTEGER NOT NULL DEFAULT 0,
            retained_count INTEGER NOT NULL DEFAULT 0,
            batch_count INTEGER NOT NULL DEFAULT 0,
            is_complete INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_key TEXT NOT NULL,
            query_term TEXT NOT NULL,
            request_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            processed_count INTEGER NOT NULL,
            retained_count INTEGER NOT NULL,
            next_page_token TEXT,
            status TEXT NOT NULL,
            error TEXT
        );
        """
    )
    conn.commit()


HARD_RECALL_SECTIONS = {
    "english_core",
    "english_abbreviations",
    "english_cancer_types",
    "english_rare_cancer_types",
    "english_histology_and_precancer_types",
    "chinese_core",
    "chinese_cancer_types",
    "chinese_rare_cancer_types",
}


def load_terms_from_sections(path: Path, include_sections: set[str]) -> list[str]:
    terms: list[str] = []
    current_section: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            current_section = line.strip().rstrip(":")
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_section in include_sections:
            term = stripped[2:].strip().strip("'\"").lower()
            if term:
                terms.append(term)
    return sorted(set(terms), key=len, reverse=True)


def load_terms(path: Path = TERMS_FILE) -> list[str]:
    """Load terms that can retain a trial candidate by themselves."""
    return load_terms_from_sections(path, HARD_RECALL_SECTIONS)


def load_contextual_terms(path: Path = TERMS_FILE) -> list[str]:
    """Load terms useful for ranking/review but unsafe as standalone recall."""
    contextual_sections: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            section = line.strip().rstrip(":")
            if section not in HARD_RECALL_SECTIONS:
                contextual_sections.add(section)
    return load_terms_from_sections(path, contextual_sections)


def api_url(query_term: str, page_size: int, page_token: str | None = None) -> str:
    params = {
        "query.term": query_term,
        "pageSize": str(page_size),
        "format": "json",
    }
    if page_token:
        params["pageToken"] = page_token
    return f"{CTGOV_STUDIES_URL}?{urllib.parse.urlencode(params)}"


def fetch_json(url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "global-cancer-trials-db/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def norm_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def get_path(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def join_text(values: list[str]) -> str:
    return " | ".join([value.strip() for value in values if value and value.strip()])


def study_id(study: dict[str, Any]) -> str | None:
    return get_path(study, "protocolSection", "identificationModule", "nctId")


def source_url(nct_id: str) -> str:
    return f"https://clinicaltrials.gov/study/{nct_id}"


def normalize_match_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[\u2010-\u2015\-_/.,;+():\[\]{}]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


class TermMatcher:
    """Precompiled high-recall term matcher.

    The AACT builder scans millions of rows. Normalizing text and compiling
    regexes for every term on every row is the main avoidable CPU cost, so this
    matcher normalizes each row once and compiles single-token terms once.
    """

    def __init__(self, terms: list[str]) -> None:
        self.non_ascii_terms: list[tuple[str, str]] = []
        self.ascii_phrases: list[tuple[str, str]] = []
        single_terms: list[tuple[str, str]] = []
        seen: set[str] = set()
        for term in terms:
            original = term.lower().strip()
            if not original or original in seen:
                continue
            seen.add(original)
            if not original.isascii():
                self.non_ascii_terms.append((original, original))
                continue
            normalized = normalize_match_text(original)
            if not normalized:
                continue
            if " " in normalized:
                self.ascii_phrases.append((normalized, original))
            else:
                single_terms.append((normalized, original))
        single_terms = sorted(set(single_terms), key=lambda item: len(item[0]), reverse=True)
        self.single_lookup = {normalized: original for normalized, original in single_terms}
        if single_terms:
            alternation = "|".join(re.escape(normalized) for normalized, _ in single_terms)
            self.single_pattern = re.compile(r"(?<![a-z0-9])(?:" + alternation + r")(?![a-z0-9])")
        else:
            self.single_pattern = None

    def match(self, text: str) -> str | None:
        if not text:
            return None
        lower_text = text.lower()
        for term, original in self.non_ascii_terms:
            if term in lower_text:
                return original
        normalized_text = normalize_match_text(text)
        if not normalized_text:
            return None
        for term, original in self.ascii_phrases:
            if term in normalized_text:
                return original
        if self.single_pattern:
            hit = self.single_pattern.search(normalized_text)
            if hit:
                return self.single_lookup.get(hit.group(0), hit.group(0))
        return None


def term_matches(text: str, term: str) -> bool:
    return TermMatcher([term]).match(text) is not None


def cancer_recall(study: dict[str, Any], terms: list[str]) -> tuple[bool, str | None, str]:
    ps = study.get("protocolSection", {})
    idm = ps.get("identificationModule", {})
    cond = ps.get("conditionsModule", {})
    desc = ps.get("descriptionModule", {})
    arms = ps.get("armsInterventionsModule", {})
    elig = ps.get("eligibilityModule", {})

    field_texts = {
        "conditions": join_text(norm_list(cond.get("conditions"))),
        "keywords": join_text(norm_list(cond.get("keywords"))),
        "title": join_text(norm_list(idm.get("briefTitle")) + norm_list(idm.get("officialTitle"))),
        "summary": join_text(norm_list(desc.get("briefSummary")) + norm_list(desc.get("detailedDescription"))),
        "interventions": join_text(
            [item.get("name", "") for item in arms.get("interventions", []) if isinstance(item, dict)]
        ),
        "eligibility": str(elig.get("eligibilityCriteria") or ""),
    }
    matcher = TermMatcher(terms)
    for field, text in field_texts.items():
        term = matcher.match(text)
        if term:
            confidence = "high" if field in {"conditions", "keywords", "title"} else "medium"
            return True, f"{field}:{term}", confidence
    return False, None, "none"


def normalize_status(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().lower().replace("_", " ")
    if value in {"recruiting", "available"}:
        return "recruiting"
    if value == "not yet recruiting":
        return "not_yet_recruiting"
    if value == "active, not recruiting":
        return "active_not_recruiting"
    if value in {"completed", "terminated", "withdrawn", "suspended"}:
        return value.replace(" ", "_")
    return value


def normalize_phase(phases: Any) -> tuple[str | None, str | None]:
    items = norm_list(phases)
    raw = join_text(items) or None
    if not raw:
        return None, None
    normalized = raw.lower().replace("phase ", "phase_").replace("early_phase_1", "early_phase_1")
    normalized = re.sub(r"[^a-z0-9_ /|-]+", "", normalized).strip()
    return raw, normalized


def split_criteria(criteria: str | None) -> list[tuple[str, str]]:
    if not criteria:
        return []
    lines = [line.strip(" \t-*•") for line in criteria.splitlines()]
    rows: list[tuple[str, str]] = []
    current = "unknown"
    for line in lines:
        if not line:
            continue
        lower = line.lower().rstrip(":")
        if lower in {"inclusion criteria", "inclusion"}:
            current = "inclusion"
            continue
        if lower in {"exclusion criteria", "exclusion"}:
            current = "exclusion"
            continue
        rows.append((current, line))
    if not rows and criteria.strip():
        rows.append(("unknown", criteria.strip()))
    return rows


def upsert_study(conn: sqlite3.Connection, study: dict[str, Any], terms: list[str], fetched_at: str) -> bool:
    nct_id = study_id(study)
    if not nct_id:
        return False
    retained, recall_source, recall_confidence = cancer_recall(study, terms)
    if not retained:
        return False

    ps = study.get("protocolSection", {})
    idm = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    cond = ps.get("conditionsModule", {})
    desc = ps.get("descriptionModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {})
    arms = ps.get("armsInterventionsModule", {})
    elig = ps.get("eligibilityModule", {})
    contacts = ps.get("contactsLocationsModule", {})

    trial_uid = f"ctgov:{nct_id}"
    raw_status = status.get("overallStatus")
    phase_raw, phase_norm = normalize_phase(design.get("phases"))
    interventions = [item for item in arms.get("interventions", []) if isinstance(item, dict)]
    locations = [item for item in contacts.get("locations", []) if isinstance(item, dict)]
    countries = sorted({str(loc.get("country")) for loc in locations if loc.get("country")})
    conditions = norm_list(cond.get("conditions"))

    conn.execute(
        """
        INSERT INTO raw_trial_records (
            source_name, source_trial_id, source_url, raw_json, fetched_at, parser_version, fetch_status
        )
        VALUES (?, ?, ?, ?, ?, ?, 'success')
        """,
        (
            "clinicaltrials.gov",
            nct_id,
            source_url(nct_id),
            json.dumps(study, ensure_ascii=False, sort_keys=True),
            fetched_at,
            SCHEMA_VERSION,
        ),
    )

    conn.execute(
        """
        INSERT INTO trial_master (
            trial_uid, primary_registry_id, primary_source, title, scientific_title, brief_summary,
            recruitment_status_raw, recruitment_status_normalized, phase_raw, phase_normalized,
            study_type_raw, study_type_normalized, disease_text, disease_normalized,
            cancer_type_normalized, intervention_summary, sponsor_summary, countries,
            registration_date, start_date, completion_date, last_update_date, source_url,
            last_fetched_at, cancer_recall_source, cancer_recall_confidence, data_quality_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            idm.get("briefTitle"),
            idm.get("officialTitle"),
            desc.get("briefSummary"),
            raw_status,
            normalize_status(raw_status),
            phase_raw,
            phase_norm,
            design.get("studyType"),
            str(design.get("studyType")).lower() if design.get("studyType") else None,
            join_text(conditions),
            None,
            None,
            join_text([item.get("name", "") for item in interventions]),
            get_path(sponsor, "leadSponsor", "name"),
            join_text(countries),
            status.get("studyFirstSubmitDate"),
            get_path(status, "startDateStruct", "date"),
            get_path(status, "completionDateStruct", "date"),
            status.get("lastUpdateSubmitDate"),
            source_url(nct_id),
            fetched_at,
            recall_source,
            recall_confidence,
            "unreviewed",
        ),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO trial_registry_ids (
            trial_uid, registry_source, registry_id, id_type, is_primary, source_url
        )
        VALUES (?, 'clinicaltrials.gov', ?, 'primary', 1, ?)
        """,
        (trial_uid, nct_id, source_url(nct_id)),
    )

    conn.execute("DELETE FROM trial_interventions WHERE trial_uid = ?", (trial_uid,))
    for intervention in interventions:
        conn.execute(
            """
            INSERT INTO trial_interventions (
                trial_uid, intervention_name_raw, intervention_name_normalized, intervention_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                trial_uid,
                intervention.get("name"),
                intervention.get("name"),
                intervention.get("type"),
            ),
        )

    conn.execute("DELETE FROM trial_eligibility_criteria WHERE trial_uid = ?", (trial_uid,))
    for index, (kind, text) in enumerate(split_criteria(elig.get("eligibilityCriteria")), start=1):
        conn.execute(
            """
            INSERT INTO trial_eligibility_criteria (
                trial_uid, criterion_type, criterion_text, language, criterion_order, source_section
            )
            VALUES (?, ?, ?, 'en', ?, 'eligibilityCriteria')
            """,
            (trial_uid, kind, text, index),
        )

    conn.execute("DELETE FROM trial_sites WHERE trial_uid = ?", (trial_uid,))
    for location in locations:
        conn.execute(
            """
            INSERT INTO trial_sites (
                trial_uid, site_name, country, province, city, site_status, source_name, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, 'clinicaltrials.gov', ?)
            """,
            (
                trial_uid,
                location.get("facility"),
                location.get("country"),
                location.get("state"),
                location.get("city"),
                location.get("status"),
                source_url(nct_id),
            ),
        )
    return True


def get_state(conn: sqlite3.Connection, query_key: str, query_term: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM source_build_state WHERE query_key = ?", (query_key,)).fetchone()
    if row:
        return row
    conn.execute(
        """
        INSERT INTO source_build_state (query_key, query_term, updated_at)
        VALUES (?, ?, ?)
        """,
        (query_key, query_term, utc_now()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM source_build_state WHERE query_key = ?", (query_key,)).fetchone()


def process_api(conn: sqlite3.Connection, queries: list[str], page_size: int, limit: int, sleep: float, timeout: int) -> None:
    terms = load_terms()
    total_processed = 0
    for query_index, query_term in enumerate(queries):
        query_key = f"api:{query_index}:{query_term}"
        while True:
            state = get_state(conn, query_key, query_term)
            if state["is_complete"]:
                break
            if limit and total_processed >= limit:
                return
            page_token = state["next_page_token"]
            url = api_url(query_term, page_size, page_token)
            fetched_at = utc_now()
            processed = 0
            retained = 0
            try:
                payload = fetch_json(url, timeout=timeout)
                studies = payload.get("studies", [])
                next_page_token = payload.get("nextPageToken")
                with conn:
                    for study in studies:
                        processed += 1
                        if upsert_study(conn, study, terms, fetched_at):
                            retained += 1
                    total_processed += processed
                    complete = 0 if next_page_token else 1
                    conn.execute(
                        """
                        UPDATE source_build_state
                        SET next_page_token = ?, processed_count = processed_count + ?,
                            retained_count = retained_count + ?, batch_count = batch_count + 1,
                            is_complete = ?, last_error = NULL, updated_at = ?
                        WHERE query_key = ?
                        """,
                        (next_page_token, processed, retained, complete, utc_now(), query_key),
                    )
                    conn.execute(
                        """
                        INSERT INTO source_batches (
                            query_key, query_term, request_url, fetched_at, processed_count,
                            retained_count, next_page_token, status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'success')
                        """,
                        (query_key, query_term, url, fetched_at, processed, retained, next_page_token),
                    )
                print(
                    f"{query_key}: processed={processed} retained={retained} "
                    f"next_page_token={'yes' if next_page_token else 'no'}"
                )
                if not next_page_token or (limit and total_processed >= limit):
                    break
                if sleep:
                    time.sleep(sleep)
            except Exception as exc:
                with conn:
                    conn.execute(
                        """
                        UPDATE source_build_state
                        SET last_error = ?, updated_at = ?
                        WHERE query_key = ?
                        """,
                        (str(exc), utc_now(), query_key),
                    )
                    conn.execute(
                        """
                        INSERT INTO source_batches (
                            query_key, query_term, request_url, fetched_at, processed_count,
                            retained_count, status, error
                        )
                        VALUES (?, ?, ?, ?, 0, 0, 'failed', ?)
                        """,
                        (query_key, query_term, url, fetched_at, str(exc)),
                    )
                raise


def print_summary(conn: sqlite3.Connection, db_path: Path) -> None:
    tables = [
        "raw_trial_records",
        "trial_master",
        "trial_interventions",
        "trial_eligibility_criteria",
        "trial_sites",
        "source_build_state",
        "source_batches",
    ]
    print(f"database={db_path}")
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}={count}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    conn = connect_db(args.out, reset=args.reset)
    try:
        init_schema(conn)
        if not args.init_only:
            queries = args.query or DEFAULT_QUERIES
            process_api(conn, queries, args.page_size, args.limit, args.sleep, args.timeout)
        print_summary(conn, args.out)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])




