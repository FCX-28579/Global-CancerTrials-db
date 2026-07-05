"""Export pending AACT candidates for review by the current Codex/Claude conversation.

This mode avoids direct OpenAI API calls. The script only prepares a compact
JSON batch and a prompt file. The current assistant reviews the batch and writes
a JSONL results file, which can then be imported with
import_conversation_review_results.py.
"""

from __future__ import annotations

import argparse
import json
import re
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
import preprocess_browse_conditions as mesh_preprocess  # noqa: E402

DEFAULT_OUT_DIR = common.SOURCE_ROOT / "data" / "review_batches"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=common.DEFAULT_DB)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aact-zip", type=Path)
    group.add_argument("--aact-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--batch-id", help="Stable batch id. Defaults to utc timestamp.")
    parser.add_argument("--limit", type=int, default=50, help="Candidates to export.")
    parser.add_argument("--tier", choices=["all", "medium", "low"], default="all")
    parser.add_argument("--source-prefix", help="Optional recall_source prefix filter, e.g. eligibility or interventions.")
    parser.add_argument("--full-context", action="store_true", help="Export full context instead of compact token-saving context.")
    parser.add_argument("--summary-chars", type=int, default=160, help="Max brief_summary chars in compact mode.")
    parser.add_argument("--eligibility-chars", type=int, default=100, help="Max eligibility chars in compact mode.")
    parser.add_argument("--skip-mesh-preprocess", action="store_true", help="Skip pre-LLM deterministic rule preprocessing before selecting review candidates.")
    return parser.parse_args(argv)


def selected_candidates(conn: sqlite3.Connection, limit: int, tier: str, source_prefix: str | None) -> list[sqlite3.Row]:
    where = [
        "needs_review = 1",
        "COALESCE(llm_review_status, 'pending') IN ('pending', 'not_required')",
    ]
    params: list[Any] = []
    if tier != "all":
        where.append("recall_tier = ?")
        params.append(tier)
    if source_prefix:
        where.append("recall_source LIKE ?")
        params.append(f"{source_prefix}:%")
    sql = f"""
        SELECT nct_id, recall_tier, recall_source, cancer_recall_confidence
        FROM aact_candidate_trials
        WHERE {' AND '.join(where)}
        ORDER BY CASE recall_tier WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, nct_id
    """
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()




def normalize_ws(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace("~", " ").strip()


def matched_term_from_source(recall_source: str | None) -> str:
    if not recall_source or ":" not in recall_source:
        return ""
    return recall_source.split(":", 1)[1].split(";", 1)[0].strip()


def snippet_around(text: Any, terms: list[str], max_chars: int) -> str:
    clean = normalize_ws(text)
    if not clean or max_chars <= 0:
        return ""
    lower = clean.lower()
    hit_start: int | None = None
    hit_end: int | None = None
    for term in terms:
        term = term.lower().strip()
        if not term:
            continue
        match = re.search(re.escape(term), lower)
        if match:
            hit_start, hit_end = match.start(), match.end()
            break
    if hit_start is None:
        return clean[:max_chars].rstrip() + ("..." if len(clean) > max_chars else "")
    context = max(0, (max_chars - (hit_end - hit_start)) // 2)
    start = max(0, hit_start - context)
    end = min(len(clean), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return prefix + clean[start:end].strip() + suffix


def recalled_field_text(context: dict[str, Any], recall_source: str | None) -> Any:
    source = recall_source or ""
    if source.startswith("interventions:"):
        return context.get("intervention_excerpt") or " | ".join(context.get("interventions") or [])
    if source.startswith("description:"):
        return context.get("detailed_description_excerpt") or context.get("brief_summary")
    if source.startswith("summary:"):
        return context.get("brief_summary")
    if source.startswith("eligibility:"):
        return context.get("eligibility_excerpt")
    if source.startswith("conditions:"):
        return " | ".join(context.get("conditions") or [])
    if source.startswith("study:"):
        return " | ".join([str(context.get("brief_title") or ""), str(context.get("official_title") or "")])
    return ""


def compact_context(context: dict[str, Any], recall_source: str | None, summary_chars: int, eligibility_chars: int) -> dict[str, Any]:
    def trim(value: Any, max_chars: int) -> str:
        text = normalize_ws(value)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    matched_term = matched_term_from_source(recall_source)
    snippet_terms = [matched_term]
    snippet_terms.extend(str(item) for item in (context.get("conditions") or []))
    snippet_terms.extend(str(item) for item in (context.get("browse_conditions") or []))
    summary_snippet = snippet_around(context.get("brief_summary"), snippet_terms, summary_chars)
    eligibility_snippet = snippet_around(context.get("eligibility_excerpt"), snippet_terms, eligibility_chars)
    recalled_snippet = snippet_around(recalled_field_text(context, recall_source), [matched_term], 140)
    return {
        "title": context.get("brief_title"),
        "conditions": context.get("conditions") or [],
        "browse_conditions": (context.get("browse_conditions") or [])[:8],
        "interventions": context.get("interventions") or [],
        "matched_term": matched_term,
        "recalled_field_hit_snippet": recalled_snippet,
        "summary_hit_snippet": summary_snippet or trim(context.get("brief_summary"), summary_chars),
        "eligibility_hit_snippet": eligibility_snippet or trim(context.get("eligibility_excerpt"), eligibility_chars),
    }

def instructions_text(batch_path: Path, results_path: Path) -> str:
    return f"""# Conversation LLM Review Instructions

You are reviewing candidate ClinicalTrials.gov records for a local oncology trial database.

Input batch: `{batch_path}`
Output JSONL: `{results_path}`

For each candidate, decide whether to keep it in the oncology database.

Keep if the trial is genuinely oncology-related, including cancer treatment, cancer diagnosis, screening/prevention, supportive care, survivorship, hematologic malignancy, or a biomarker/therapy trial explicitly involving cancer patients.

Reject if cancer is only background risk, family history, exclusion criteria, unrelated allergy/immunology, a benign/non-oncology condition, or a broad MeSH/keyword artifact.

Write one JSON object per line with this schema:

```json
{{"nct_id":"NCT...","keep":true,"confidence":"high","reason":"short reason","evidence":"short phrase from title/condition/summary/etc"}}
```

Rules:
- Use only `high`, `medium`, or `low` for confidence.
- `keep` must be boolean.
- Keep `reason` under 1000 characters.
- Keep `evidence` under 500 characters.
- Every candidate in the batch must have exactly one result line.
"""


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.limit < 1:
        raise ValueError("--limit must be >= 1 for conversation batches")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    batch_id = args.batch_id or common.utc_now().replace(":", "").replace("+", "Z").replace("-", "").replace(".", "")
    batch_path = args.out_dir / f"{batch_id}.json"
    prompt_path = args.out_dir / f"{batch_id}.instructions.md"
    results_path = args.out_dir / f"{batch_id}.results.jsonl"

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    files = aact.AACTFiles(zip_path=args.aact_zip, dir_path=args.aact_dir)
    try:
        if not args.skip_mesh_preprocess:
            summary = mesh_preprocess.apply_pre_llm_rules(conn, files)
            print("pre_llm_rule_preprocess_summary=" + ", ".join(f"{key}:{summary.get(key, 0)}" for key in [
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
]))
        rows = selected_candidates(conn, args.limit, args.tier, args.source_prefix)
        ids = [row["nct_id"] for row in rows]
        contexts = review.build_context(files, ids)
        candidates = []
        for row in rows:
            context = contexts.get(row["nct_id"], {"nct_id": row["nct_id"]})
            export_context = context if args.full_context else compact_context(context, row["recall_source"], args.summary_chars, args.eligibility_chars)
            candidates.append(
                {
                    "nct_id": row["nct_id"],
                    "recall_tier": row["recall_tier"],
                    "recall_source": row["recall_source"],
                    "cancer_recall_confidence": row["cancer_recall_confidence"],
                    "context": export_context,
                }
            )
        payload = {
            "batch_id": batch_id,
            "created_at": common.utc_now(),
            "db": str(args.db),
            "candidate_count": len(candidates),
            "context_mode": "full" if args.full_context else "compact_hit_snippets",
            "result_schema": {
                "nct_id": "string",
                "keep": "boolean",
                "confidence": "high|medium|low",
                "reason": "short string",
                "evidence": "short source phrase",
            },
            "candidates": candidates,
        }
        batch_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        prompt_path.write_text(instructions_text(batch_path, results_path), encoding="utf-8")
        print(f"batch={batch_path}")
        print(f"instructions={prompt_path}")
        print(f"results_template={results_path}")
        print(f"candidate_count={len(candidates)}")
    finally:
        files.close()
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])









