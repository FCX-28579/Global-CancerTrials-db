"""
Review low-confidence AACT recall candidates with an LLM.

This script reviews candidates from aact_candidate_trials where needs_review=1.
It reads source context from the AACT zip/directory, asks an LLM whether the
trial is truly oncology-related, and writes the decision back to the source DB.

Normal mode requires OPENAI_API_KEY and uses the OpenAI Responses API.
Use --dry-run to inspect prompts without calling the model.
Use --mock-review for local tests without network/API calls.
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_aact_source_db as aact  # noqa: E402
import build_source_db as common  # noqa: E402
import preprocess_browse_conditions as mesh_preprocess  # noqa: E402

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
RESPONSES_URL = "https://api.openai.com/v1/responses"
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



def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=common.DEFAULT_DB)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aact-zip", type=Path)
    group.add_argument("--aact-dir", type=Path)
    parser.add_argument("--limit", type=int, default=50, help="Maximum candidates to review. Use 0 to review all pending candidates.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock-review", action="store_true", help="Use deterministic local heuristic and write review results.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--progress-log", type=Path, help="Append review progress to this log file.")
    parser.add_argument("--skip-mesh-preprocess", action="store_true", help="Skip pre-LLM deterministic rule preprocessing before selecting review candidates.")
    return parser.parse_args(argv)


def selected_candidates(conn: sqlite3.Connection, limit: int) -> list[str]:
    sql = """
        SELECT nct_id
        FROM aact_candidate_trials
        WHERE needs_review = 1
          AND COALESCE(llm_review_status, 'pending') IN ('pending', 'not_required')
        ORDER BY CASE recall_tier WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, nct_id
    """
    params: tuple[Any, ...] = ()
    if limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [row[0] for row in rows]


def grouped(files: aact.AACTFiles, filename: str, ids: set[str]) -> dict[str, list[dict[str, str]]]:
    return aact.group_rows(aact.read_rows_for_candidates(files, filename, ids))


def build_context(files: aact.AACTFiles, ids: list[str]) -> dict[str, dict[str, Any]]:
    id_set = set(ids)
    studies = {row["nct_id"]: row for row in aact.read_rows_for_candidates(files, "studies.txt", id_set)}
    conditions = grouped(files, "conditions.txt", id_set)
    browse = grouped(files, "browse_conditions.txt", id_set)
    summaries = grouped(files, "brief_summaries.txt", id_set)
    descriptions = grouped(files, "detailed_descriptions.txt", id_set)
    interventions = grouped(files, "interventions.txt", id_set)
    keywords = grouped(files, "keywords.txt", id_set)
    eligibilities = grouped(files, "eligibilities.txt", id_set)
    contexts: dict[str, dict[str, Any]] = {}
    for nct_id in ids:
        study = studies.get(nct_id, {})
        contexts[nct_id] = {
            "nct_id": nct_id,
            "brief_title": study.get("brief_title"),
            "official_title": study.get("official_title"),
            "conditions": aact.values(conditions.get(nct_id, []), "name"),
            "browse_conditions": [
                row.get("mesh_term") or row.get("downcase_mesh_term")
                for row in browse.get(nct_id, [])
                if row.get("mesh_term") or row.get("downcase_mesh_term")
            ],
            "keywords": aact.values(keywords.get(nct_id, []), "name"),
            "interventions": aact.values(interventions.get(nct_id, []), "name"),
            "intervention_excerpt": " | ".join(
                " / ".join(filter(None, [row.get("name"), row.get("description"), row.get("intervention_type")]))
                for row in interventions.get(nct_id, [])
            )[:1200],
            "brief_summary": aact.first_text(summaries.get(nct_id, []), "description"),
            "detailed_description_excerpt": (aact.first_text(descriptions.get(nct_id, []), "description") or "")[:1200],
            "eligibility_excerpt": (aact.first_text(eligibilities.get(nct_id, []), "criteria") or "")[:1200],
        }
    return contexts


def prompt_for(context: dict[str, Any]) -> str:
    return (
        "You are reviewing a clinical trial candidate for a local oncology clinical trial database.\n"
        "Keep the trial if it is genuinely oncology-related: cancer treatment, cancer diagnosis, "
        "cancer screening/prevention, cancer supportive care, cancer survivorship, hematologic malignancy, "
        "or a biomarker/therapy trial explicitly involving cancer patients.\n"
        "Reject it if cancer is only background risk, family history, exclusion criteria, unrelated allergy immunotherapy, "
        "a benign/non-oncology condition, or a broad MeSH/keyword artifact.\n"
        "Return only JSON with keys: keep (boolean), confidence (high|medium|low), reason (short string), evidence (short source phrase).\n\n"
        f"TRIAL_CONTEXT_JSON:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def call_openai(prompt: str, model: str, timeout: int) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "input": prompt,
        "temperature": 0,
    }
    req = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    text = body.get("output_text")
    if not text:
        chunks: list[str] = []
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        text = "\n".join(chunks)
    if not text:
        raise RuntimeError("No text output returned by model")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"Model did not return JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


def mock_review(context: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        str(context.get(key) or "")
        for key in ["brief_title", "official_title", "conditions", "interventions", "brief_summary"]
    ).lower()
    strong = ["cancer", "carcinoma", "sarcoma", "leukemia", "lymphoma", "myeloma", "melanoma", "glioma", "malignant", "neuroblastoma", "solid tumor"]
    weak_reject = ["allergy", "myasthenia", "diabetes", "preeclampsia", "parkinson", "alzheimer", "genital warts"]
    keep = any(term in text for term in strong) and not any(term in text for term in weak_reject)
    return {
        "keep": keep,
        "confidence": "medium",
        "reason": "mock heuristic review",
        "evidence": "local heuristic",
    }


def write_review(conn: sqlite3.Connection, nct_id: str, result: dict[str, Any]) -> None:
    keep = 1 if bool(result.get("keep")) else 0
    status = "kept" if keep else "rejected"
    reason = str(result.get("reason") or "")[:1000]
    evidence = str(result.get("evidence") or "")[:500]
    if evidence:
        reason = f"{reason} Evidence: {evidence}"[:1000]
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET needs_review = 0, llm_review_status = ?, llm_keep = ?, llm_reason = ?, llm_reviewed_at = ?
        WHERE nct_id = ?
        """,
        (status, keep, reason, common.utc_now(), nct_id),
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_progress_log(args.progress_log)
    if not args.dry_run and not args.mock_review and not os.environ.get("OPENAI_API_KEY"):
        close_progress_log()
        raise RuntimeError("OPENAI_API_KEY is not set; refusing to mark pending candidates as errors.")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    files = aact.AACTFiles(zip_path=args.aact_zip, dir_path=args.aact_dir)
    run_id: int | None = None
    try:
        aact.init_aact_tables(conn)
        if not args.skip_mesh_preprocess:
            summary = mesh_preprocess.apply_pre_llm_rules(conn, files)
            emit("pre_llm_rule_preprocess_summary=" + ", ".join(f"{key}:{summary.get(key, 0)}" for key in [
    "browse_malignant_or_cancer_specific",
    "browse_precancer_or_prevention",
    "browse_broad_or_ambiguous_review",
    "browse_benign_or_non_oncology_exclude",
    "browse_unclassified_review",
    "browse_strong_promoted_from_pending",
    "intervention_rule_rejected_false_pattern",
    "intervention_rule_rejected_abbreviation_context",
    "intervention_rule_kept_positive_context",
    "intervention_remaining_for_review",
    "low_text_rule_rejected_tnf",
    "low_text_rule_rejected_eligibility_exclusion",
    "low_text_rule_rejected_admin_artifact",
    "low_text_rule_rejected_abbreviation_artifact",
    "low_text_rule_rejected_non_oncology_meaning",
    "low_text_rule_rejected_benign_neoplasm",
    "low_text_rule_rejected_infection_background",
    "low_text_rule_rejected_drug_prior_oncology_use",
    "low_text_rule_rejected_epidemiology_background",
    "low_text_rule_rejected_risk_only",
    "low_text_protected_by_keep_pattern",
    "low_text_remaining_for_review",
    "eligibility_only_rule_rejected",
    "eligibility_only_protected_by_anchor",
    "eligibility_only_protected_by_browse",
]), flush=True)
        ids = selected_candidates(conn, args.limit)
        emit(f"selected_candidates={len(ids)}", flush=True)
        contexts = build_context(files, ids)
        if args.dry_run:
            for nct_id in ids[: min(5, len(ids))]:
                emit(prompt_for(contexts[nct_id])[:2500])
                emit("---")
            emit(f"dry_run_candidates={len(ids)}")
            return
        with conn:
            cur = conn.execute(
                "INSERT INTO llm_review_runs (source_name, model, started_at, status) VALUES ('aact', ?, ?, 'running')",
                (args.model if not args.mock_review else 'mock-review', common.utc_now()),
            )
            run_id = int(cur.lastrowid)
        reviewed = kept = rejected = errors = 0
        for index, nct_id in enumerate(ids, start=1):
            try:
                result = mock_review(contexts[nct_id]) if args.mock_review else call_openai(prompt_for(contexts[nct_id]), args.model, args.timeout)
                with conn:
                    write_review(conn, nct_id, result)
                reviewed += 1
                if result.get("keep"):
                    kept += 1
                else:
                    rejected += 1
                emit(f"reviewed {index}/{len(ids)} {nct_id} keep={bool(result.get('keep'))}", flush=True)
            except Exception as exc:
                errors += 1
                with conn:
                    conn.execute(
                        """
                        UPDATE aact_candidate_trials
                        SET needs_review = 0, llm_review_status = 'error', llm_reason = ?, llm_reviewed_at = ?
                        WHERE nct_id = ?
                        """,
                        (str(exc)[:1000], common.utc_now(), nct_id),
                    )
                emit(f"error {nct_id}: {exc}", flush=True)
        with conn:
            conn.execute(
                """
                UPDATE llm_review_runs
                SET finished_at = ?, reviewed_count = ?, kept_count = ?, rejected_count = ?, error_count = ?, status = 'complete'
                WHERE id = ?
                """,
                (common.utc_now(), reviewed, kept, rejected, errors, run_id),
            )
        emit(f"reviewed={reviewed} kept={kept} rejected={rejected} errors={errors}")
    finally:
        files.close()
        conn.close()
        close_progress_log()

if __name__ == "__main__":
    main(sys.argv[1:])









