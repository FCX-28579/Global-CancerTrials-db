"""Preprocess AACT recall candidates before LLM review.

This module applies deterministic, auditable rules before expensive LLM review:

1. Classify ClinicalTrials.gov/AACT browse_conditions MeSH matches into
   strong malignant, precancer/prevention, broad-review, and benign/noise.
2. Reject obvious intervention-only false positives such as TNF phrases,
   non-oncology abbreviation/code collisions, and drug/device contexts when no
   title/condition/summary or strong MeSH cancer anchor is present.
3. Reject obvious eligibility-only false positives when the cancer term appears
   only in eligibility and title/conditions/summary contain no cancer anchor,
   unless browse_conditions contains a strong malignant or precancer MeSH term.

The rules are conservative: if a non-eligibility field has a cancer anchor, the
candidate stays pending for LLM/manual review rather than being rule-rejected.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_source_db as common  # noqa: E402

MESH_TERMS_FILE = common.CONFIG_DIR / "cancer_mesh_terms.yaml"
FALSE_POSITIVE_RULES_FILE = common.CONFIG_DIR / "pre_llm_false_positive_rules.yaml"

CATEGORIES = (
    "malignant_or_cancer_specific",
    "precancer_or_prevention",
    "broad_or_ambiguous_review",
    "benign_or_non_oncology_exclude",
)
STRONG_BROWSE_CATEGORIES = {"malignant_or_cancer_specific", "precancer_or_prevention"}


def normalize_mesh(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[\u2010-\u2015]", "-", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def load_mesh_rules(path: Path = MESH_TERMS_FILE) -> dict[str, set[str]]:
    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            category: {normalize_mesh(str(term)) for term in payload.get(category, []) if str(term).strip()}
            for category in CATEGORIES
        }
    except ImportError:
        rules: dict[str, set[str]] = {category: set() for category in CATEGORIES}
        current: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                section = line.strip().rstrip(":")
                current = section if section in rules else None
                continue
            stripped = line.strip()
            if current and stripped.startswith("- "):
                rules[current].add(normalize_mesh(stripped[2:].strip().strip("'\"")))
        return rules


def pending_where(include_reviewed: bool) -> list[str]:
    if include_reviewed:
        return []
    return ["COALESCE(llm_review_status, 'pending') NOT IN ('kept', 'rejected', 'error')"]


def browse_candidate_ids(conn: sqlite3.Connection, include_reviewed: bool = False) -> set[str]:
    where = ["recall_source LIKE 'browse_conditions:%'", *pending_where(include_reviewed)]
    return {row[0] for row in conn.execute(f"SELECT nct_id FROM aact_candidate_trials WHERE {' AND '.join(where)}")}


def eligibility_candidate_ids(conn: sqlite3.Connection, include_reviewed: bool = False) -> set[str]:
    where = ["recall_source LIKE 'eligibility:%'", *pending_where(include_reviewed)]
    return {row[0] for row in conn.execute(f"SELECT nct_id FROM aact_candidate_trials WHERE {' AND '.join(where)}")}


def intervention_candidate_rows(conn: sqlite3.Connection, include_reviewed: bool = False) -> list[sqlite3.Row]:
    where = ["recall_source LIKE 'interventions:%'", *pending_where(include_reviewed)]
    return list(
        conn.execute(
            f"""
            SELECT nct_id, recall_source
            FROM aact_candidate_trials
            WHERE {' AND '.join(where)}
            """
        )
    )


def pending_review_candidate_ids(conn: sqlite3.Connection, include_reviewed: bool = False) -> set[str]:
    where = ["needs_review = 1", *pending_where(include_reviewed)]
    return {row[0] for row in conn.execute(f"SELECT nct_id FROM aact_candidate_trials WHERE {' AND '.join(where)}")}


def low_pending_candidate_rows(conn: sqlite3.Connection, include_reviewed: bool = False) -> list[sqlite3.Row]:
    where = ["recall_tier = 'low'", "needs_review = 1", *pending_where(include_reviewed)]
    return list(
        conn.execute(
            f"""
            SELECT nct_id, recall_source
            FROM aact_candidate_trials
            WHERE {' AND '.join(where)}
            """
        )
    )


def read_browse_terms(files: Any, ids: set[str]) -> dict[str, set[str]]:
    terms_by_id: dict[str, set[str]] = defaultdict(set)
    if not ids or not files.exists("browse_conditions.txt"):
        return terms_by_id
    with files.open_text("browse_conditions.txt") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        for row in reader:
            nct_id = row.get("nct_id")
            if nct_id not in ids:
                continue
            term = row.get("mesh_term") or row.get("downcase_mesh_term") or ""
            if term:
                terms_by_id[nct_id].add(term)
    return terms_by_id


def read_intervention_text(files: Any, ids: set[str]) -> dict[str, list[str]]:
    text_by_id: dict[str, list[str]] = defaultdict(list)
    if not ids or not files.exists("interventions.txt"):
        return text_by_id
    with files.open_text("interventions.txt") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        for row in reader:
            nct_id = row.get("nct_id")
            if nct_id in ids:
                text_by_id[nct_id].extend(
                    [row.get("name") or "", row.get("description") or "", row.get("intervention_type") or ""]
                )
    return text_by_id


def read_non_eligibility_anchor_text(files: Any, ids: set[str]) -> dict[str, list[str]]:
    """Read title/conditions/summary text used for conservative anchor checks."""
    text_by_id: dict[str, list[str]] = defaultdict(list)
    if not ids:
        return text_by_id
    if files.exists("studies.txt"):
        with files.open_text("studies.txt") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                nct_id = row.get("nct_id")
                if nct_id in ids:
                    text_by_id[nct_id].extend([row.get("brief_title") or "", row.get("official_title") or ""])
    if files.exists("conditions.txt"):
        with files.open_text("conditions.txt") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                nct_id = row.get("nct_id")
                if nct_id in ids:
                    text_by_id[nct_id].append(row.get("name") or "")
    if files.exists("brief_summaries.txt"):
        with files.open_text("brief_summaries.txt") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                nct_id = row.get("nct_id")
                if nct_id in ids:
                    text_by_id[nct_id].append(row.get("description") or "")
    return text_by_id


def read_low_review_text(files: Any, ids: set[str]) -> dict[str, dict[str, str]]:
    text_by_id: dict[str, dict[str, str]] = defaultdict(lambda: {
        "study": "",
        "conditions": "",
        "summary": "",
        "description": "",
        "eligibility": "",
        "keywords": "",
    })
    if not ids:
        return text_by_id
    specs = {
        "studies.txt": ("study", ["brief_title", "official_title"]),
        "conditions.txt": ("conditions", ["name"]),
        "brief_summaries.txt": ("summary", ["description"]),
        "detailed_descriptions.txt": ("description", ["description"]),
        "eligibilities.txt": ("eligibility", ["criteria"]),
        "keywords.txt": ("keywords", ["name"]),
    }
    for filename, (field, columns) in specs.items():
        if not files.exists(filename):
            continue
        with files.open_text(filename) as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                nct_id = row.get("nct_id")
                if nct_id not in ids:
                    continue
                parts = [row.get(column) or "" for column in columns]
                value = " | ".join(part for part in parts if part)
                if value:
                    current = text_by_id[nct_id][field]
                    text_by_id[nct_id][field] = f"{current} | {value}" if current else value
    return text_by_id


def load_yaml_like(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        return simple_yaml_load(path)


def simple_yaml_load(path: Path) -> dict[str, Any]:
    """Small YAML subset parser for this project's list/dict rule files."""
    payload: dict[str, Any] = {}
    current_section: str | None = None
    current_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line and not line[0].isspace() and line.endswith(":"):
            current_section = line[:-1].strip()
            current_key = None
            payload[current_section] = {}
            continue
        if current_section is None:
            continue
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("- "):
            current_key = stripped[:-1].strip()
            payload[current_section][current_key] = []
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip().strip("'\"")
            if isinstance(payload[current_section], dict) and current_key:
                payload[current_section][current_key].append(item)
            elif isinstance(payload[current_section], dict) and not payload[current_section]:
                payload[current_section] = [item]
            elif isinstance(payload[current_section], list):
                payload[current_section].append(item)
    return payload


def flatten_rule_patterns(value: Any) -> list[str]:
    """Flatten either legacy list rules or grouped YAML rule sections."""
    if value is None:
        return []
    if isinstance(value, dict):
        flattened: list[str] = []
        for nested in value.values():
            flattened.extend(flatten_rule_patterns(nested))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(flatten_rule_patterns(item))
        return flattened
    item = str(value).strip()
    return [item] if item else []


def lower_rule_patterns(value: Any) -> list[str]:
    return [item.lower() for item in flatten_rule_patterns(value) if item.strip()]


def load_false_positive_rules(path: Path = FALSE_POSITIVE_RULES_FILE) -> dict[str, Any]:
    payload = load_yaml_like(path)
    abbreviation_rules = payload.get("abbreviation_false_positive_patterns") or {}
    return {
        "intervention_false_positive_patterns": lower_rule_patterns(payload.get("intervention_false_positive_patterns")),
        "abbreviation_false_positive_patterns": {
            str(key).lower(): lower_rule_patterns(values) for key, values in abbreviation_rules.items()
        },
        "non_oncology_context_patterns": lower_rule_patterns(payload.get("non_oncology_context_patterns")),
        "rule_keep_positive_context_patterns": lower_rule_patterns(payload.get("rule_keep_positive_context_patterns")),
        "text_false_positive_patterns": lower_rule_patterns(payload.get("text_false_positive_patterns")),
        "administrative_or_instrument_patterns": lower_rule_patterns(payload.get("administrative_or_instrument_patterns")),
        "risk_only_patterns": lower_rule_patterns(payload.get("risk_only_patterns")),
        "eligibility_exclusion_patterns": lower_rule_patterns(payload.get("eligibility_exclusion_patterns")),
        "low_text_rule_keep_patterns": lower_rule_patterns(payload.get("low_text_rule_keep_patterns")),
        "low_text_abbreviation_artifact_patterns": lower_rule_patterns(payload.get("low_text_abbreviation_artifact_patterns")),
        "low_text_non_oncology_meaning_patterns": lower_rule_patterns(payload.get("low_text_non_oncology_meaning_patterns")),
        "low_text_drug_prior_oncology_use_patterns": lower_rule_patterns(payload.get("low_text_drug_prior_oncology_use_patterns")),
        "low_text_benign_or_nonmalignant_patterns": lower_rule_patterns(payload.get("low_text_benign_or_nonmalignant_patterns")),
        "low_text_infection_background_patterns": lower_rule_patterns(payload.get("low_text_infection_background_patterns")),
        "low_text_epidemiology_background_patterns": lower_rule_patterns(payload.get("low_text_epidemiology_background_patterns")),
    }


def normalized_contains(text: str, pattern: str) -> bool:
    if not text or not pattern:
        return False
    if not pattern.isascii():
        return pattern.lower() in text.lower()
    normalized_text = common.normalize_match_text(text)
    normalized_pattern = common.normalize_match_text(pattern)
    if not normalized_text or not normalized_pattern:
        return False
    if " " in normalized_pattern:
        return normalized_pattern in normalized_text
    return re.search(r"(?<![a-z0-9])" + re.escape(normalized_pattern) + r"(?![a-z0-9])", normalized_text) is not None


def any_pattern(text: str, patterns: list[str]) -> str | None:
    if not text:
        return None
    normalized_text = common.normalize_match_text(text)
    lower_text = text.lower()
    for pattern in patterns:
        if not pattern:
            continue
        if not pattern.isascii():
            if pattern.lower() in lower_text:
                return pattern
            continue
        normalized_pattern = common.normalize_match_text(pattern)
        if not normalized_pattern:
            continue
        if " " in normalized_pattern:
            if normalized_pattern in normalized_text:
                return pattern
        elif re.search(r"(?<![a-z0-9])" + re.escape(normalized_pattern) + r"(?![a-z0-9])", normalized_text):
            return pattern
    return None


def classify_terms(terms: set[str], rules: dict[str, set[str]]) -> tuple[str, list[str]]:
    normalized = {normalize_mesh(term): term for term in terms}
    matches = {
        category: sorted(original for norm, original in normalized.items() if norm in rules[category])
        for category in CATEGORIES
    }
    if matches["malignant_or_cancer_specific"]:
        return "malignant_or_cancer_specific", matches["malignant_or_cancer_specific"]
    if matches["precancer_or_prevention"]:
        return "precancer_or_prevention", matches["precancer_or_prevention"]
    if matches["benign_or_non_oncology_exclude"]:
        return "benign_or_non_oncology_exclude", matches["benign_or_non_oncology_exclude"]
    if matches["broad_or_ambiguous_review"]:
        return "broad_or_ambiguous_review", matches["broad_or_ambiguous_review"]
    return "unclassified_review", sorted(terms)


def update_browse_candidate(conn: sqlite3.Connection, nct_id: str, category: str, matched_terms: list[str], dry_run: bool = False) -> None:
    matched = ", ".join(matched_terms[:8])
    now = common.utc_now()
    if category == "malignant_or_cancer_specific":
        values = (
            f"browse_conditions_mesh_malignant:{matched_terms[0]}",
            "high",
            "high",
            0,
            "not_required",
            None,
            f"Rule keep: malignant/cancer-specific MeSH browse condition: {matched}",
            None,
            now,
            nct_id,
        )
    elif category == "precancer_or_prevention":
        values = (
            f"browse_conditions_mesh_precancer:{matched_terms[0]}",
            "high",
            "high",
            0,
            "not_required",
            None,
            f"Rule keep: precancer/prevention MeSH browse condition: {matched}",
            None,
            now,
            nct_id,
        )
    elif category == "benign_or_non_oncology_exclude":
        values = (
            f"browse_conditions_mesh_excluded:{matched_terms[0]}",
            "low",
            "low",
            0,
            "rejected",
            0,
            f"Rule reject: benign or non-oncology MeSH browse condition without stronger cancer MeSH: {matched}",
            now,
            now,
            nct_id,
        )
    else:
        label = "broad_review" if category == "broad_or_ambiguous_review" else "unclassified_review"
        first = matched_terms[0] if matched_terms else "unclassified"
        values = (
            f"browse_conditions_mesh_{label}:{first}",
            "low",
            "low",
            1,
            "pending",
            None,
            f"Rule review: broad or unclassified MeSH browse condition: {matched}",
            None,
            now,
            nct_id,
        )
    if dry_run:
        return
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_source = ?,
            cancer_recall_confidence = ?,
            recall_tier = ?,
            needs_review = ?,
            llm_review_status = ?,
            llm_keep = ?,
            llm_reason = ?,
            llm_reviewed_at = ?,
            matched_at = ?
        WHERE nct_id = ?
        """,
        values,
    )


def reject_eligibility_false_positive(conn: sqlite3.Connection, nct_id: str, source: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    now = common.utc_now()
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_source = ?,
            cancer_recall_confidence = 'low',
            recall_tier = 'low',
            needs_review = 0,
            llm_review_status = 'rejected',
            llm_keep = 0,
            llm_reason = ?,
            llm_reviewed_at = ?,
            matched_at = ?
        WHERE nct_id = ?
        """,
        (
            source,
            "Rule reject: eligibility-only cancer term with no cancer anchor in title/conditions/summary and no strong malignant/precancer browse_condition MeSH.",
            now,
            now,
            nct_id,
        ),
    )


def reject_low_text_false_positive(conn: sqlite3.Connection, nct_id: str, source: str, reason: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    now = common.utc_now()
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_source = ?,
            cancer_recall_confidence = 'low',
            recall_tier = 'low',
            needs_review = 0,
            llm_review_status = 'rejected',
            llm_keep = 0,
            llm_reason = ?,
            llm_reviewed_at = ?,
            matched_at = ?
        WHERE nct_id = ?
        """,
        (source, reason, now, now, nct_id),
    )


def reject_intervention_false_positive(conn: sqlite3.Connection, nct_id: str, source: str, reason: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    now = common.utc_now()
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_source = ?,
            cancer_recall_confidence = 'low',
            recall_tier = 'low',
            needs_review = 0,
            llm_review_status = 'rejected',
            llm_keep = 0,
            llm_reason = ?,
            llm_reviewed_at = ?,
            matched_at = ?
        WHERE nct_id = ?
        """,
        (source, reason, now, now, nct_id),
    )


def keep_positive_context_candidate(conn: sqlite3.Connection, nct_id: str, source: str, reason: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    now = common.utc_now()
    conn.execute(
        """
        UPDATE aact_candidate_trials
        SET recall_source = ?,
            cancer_recall_confidence = 'high',
            recall_tier = 'high',
            needs_review = 0,
            llm_review_status = 'not_required',
            llm_keep = NULL,
            llm_reason = ?,
            llm_reviewed_at = NULL,
            matched_at = ?
        WHERE nct_id = ?
        """,
        (source, reason, now, nct_id),
    )


def apply_browse_condition_rules(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = MESH_TERMS_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    rules = load_mesh_rules(rules_path)
    ids = browse_candidate_ids(conn, include_reviewed=include_reviewed)
    terms_by_id = read_browse_terms(files, ids)
    summary: Counter[str] = Counter()
    with conn if not dry_run else nullcontext(conn):
        for nct_id in sorted(ids):
            terms = terms_by_id.get(nct_id, set())
            category, matched_terms = classify_terms(terms, rules)
            summary[f"browse_{category}"] += 1
            update_browse_candidate(conn, nct_id, category, matched_terms, dry_run=dry_run)
    return summary


def apply_intervention_false_positive_rules(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = FALSE_POSITIVE_RULES_FILE,
    mesh_rules_path: Path = MESH_TERMS_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    rows = intervention_candidate_rows(conn, include_reviewed=include_reviewed)
    ids = {row["nct_id"] for row in rows}
    recall_source_by_id = {row["nct_id"]: row["recall_source"] for row in rows}
    rules = load_false_positive_rules(rules_path)
    mesh_rules = load_mesh_rules(mesh_rules_path)
    browse_terms = read_browse_terms(files, ids)
    intervention_text = read_intervention_text(files, ids)
    review_text = read_low_review_text(files, ids)
    matcher = common.TermMatcher(common.load_terms())
    summary: Counter[str] = Counter()
    with conn if not dry_run else nullcontext(conn):
        for nct_id in sorted(ids):
            summary["intervention_total_checked"] += 1
            browse_category, _browse_matches = classify_terms(browse_terms.get(nct_id, set()), mesh_rules)
            if browse_category in STRONG_BROWSE_CATEGORIES:
                summary["intervention_protected_by_browse"] += 1
                continue

            fields = review_text.get(nct_id, {})
            title_condition_text = " | ".join([fields.get("study", ""), fields.get("conditions", "")])
            weak_context_text = " | ".join([fields.get("summary", ""), fields.get("description", ""), fields.get("eligibility", ""), fields.get("keywords", "")])
            non_intervention_text = f"{title_condition_text} | {weak_context_text}"
            intervention_blob = " | ".join(intervention_text.get(nct_id, []))
            all_text = f"{intervention_blob} | {non_intervention_text}"
            source = recall_source_by_id.get(nct_id, "")
            matched_term = source.split(":", 1)[1].lower() if ":" in source else ""

            positive = any_pattern(all_text, rules["rule_keep_positive_context_patterns"])
            if positive:
                summary["intervention_rule_kept_positive_context"] += 1
                keep_positive_context_candidate(
                    conn,
                    nct_id,
                    f"intervention_rule_kept:{positive}",
                    f"Rule keep: strong oncology support/prevention/survivorship context: {positive}",
                    dry_run=dry_run,
                )
                continue

            title_condition_anchor = matcher.match(title_condition_text)
            if title_condition_anchor:
                summary["intervention_protected_by_title_or_condition_anchor"] += 1
                continue

            false_pattern = any_pattern(intervention_blob, rules["intervention_false_positive_patterns"])
            if not false_pattern:
                false_pattern = any_pattern(all_text, rules["low_text_drug_prior_oncology_use_patterns"])
            abbreviation_pattern = None
            if matched_term in rules["abbreviation_false_positive_patterns"]:
                abbreviation_pattern = any_pattern(all_text, rules["abbreviation_false_positive_patterns"][matched_term])
            context_pattern = any_pattern(all_text, rules["non_oncology_context_patterns"])
            admin_pattern = any_pattern(all_text, rules["administrative_or_instrument_patterns"])
            risk_pattern = any_pattern(weak_context_text, rules["risk_only_patterns"])
            benign_pattern = any_pattern(all_text, rules["low_text_benign_or_nonmalignant_patterns"])
            non_oncology_meaning = any_pattern(all_text, rules["low_text_non_oncology_meaning_patterns"])

            if abbreviation_pattern:
                summary["intervention_rule_rejected_abbreviation_context"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:{matched_term}",
                    f"Rule reject: ambiguous abbreviation '{matched_term}' matched non-oncology artifact '{abbreviation_pattern}'.",
                    dry_run=dry_run,
                )
            elif false_pattern:
                summary["intervention_rule_rejected_false_pattern"] += 1
                reason = f"Rule reject: intervention recall matched drug/device/label/admin false-positive pattern '{false_pattern}'"
                if context_pattern:
                    reason += f" in non-oncology context '{context_pattern}'"
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:{matched_term or false_pattern}",
                    reason + ".",
                    dry_run=dry_run,
                )
            elif admin_pattern:
                summary["intervention_rule_rejected_admin_artifact"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:admin_{matched_term}",
                    f"Rule reject: intervention recall is administrative/instrument/education artifact '{admin_pattern}' without title/condition cancer anchor.",
                    dry_run=dry_run,
                )
            elif risk_pattern:
                summary["intervention_rule_rejected_risk_only"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:risk_{matched_term}",
                    f"Rule reject: intervention recall only has background cancer risk/prevention wording '{risk_pattern}' without title/condition cancer anchor.",
                    dry_run=dry_run,
                )
            elif benign_pattern:
                summary["intervention_rule_rejected_benign_neoplasm"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:benign_{matched_term}",
                    f"Rule reject: intervention recall matched benign/non-malignant neoplasm context '{benign_pattern}' without cancer anchor.",
                    dry_run=dry_run,
                )
            elif non_oncology_meaning:
                summary["intervention_rule_rejected_non_oncology_meaning"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:nononcology_{matched_term}",
                    f"Rule reject: intervention recall term has non-oncology meaning '{non_oncology_meaning}'.",
                    dry_run=dry_run,
                )
            elif context_pattern and matched_term in {"crc", "hcc", "aml", "cml"}:
                summary["intervention_rule_rejected_abbreviation_context"] += 1
                reject_intervention_false_positive(
                    conn,
                    nct_id,
                    f"interventions_rule_excluded:{matched_term}",
                    f"Rule reject: ambiguous abbreviation '{matched_term}' appears only in intervention field with non-oncology context '{context_pattern}'.",
                    dry_run=dry_run,
                )
            elif matcher.match(weak_context_text):
                summary["intervention_protected_by_weak_context_anchor"] += 1
            else:
                summary["intervention_remaining_for_review"] += 1
    return summary


def apply_low_text_false_positive_rules(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = FALSE_POSITIVE_RULES_FILE,
    mesh_rules_path: Path = MESH_TERMS_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    rows = low_pending_candidate_rows(conn, include_reviewed=include_reviewed)
    ids = {row["nct_id"] for row in rows}
    recall_source_by_id = {row["nct_id"]: row["recall_source"] for row in rows}
    rules = load_false_positive_rules(rules_path)
    mesh_rules = load_mesh_rules(mesh_rules_path)
    browse_terms = read_browse_terms(files, ids)
    text_by_id = read_low_review_text(files, ids)
    matcher = common.TermMatcher(common.load_terms())
    summary: Counter[str] = Counter()
    with conn if not dry_run else nullcontext(conn):
        for nct_id in sorted(ids):
            summary["low_text_total_checked"] += 1
            browse_category, _browse_matches = classify_terms(browse_terms.get(nct_id, set()), mesh_rules)
            if browse_category in STRONG_BROWSE_CATEGORIES:
                summary["low_text_protected_by_browse"] += 1
                continue
            fields = text_by_id.get(nct_id, {})
            all_text = " | ".join(fields.values())
            keep_pattern = any_pattern(all_text, rules["low_text_rule_keep_patterns"])
            if keep_pattern:
                summary["low_text_rule_kept_positive_context"] += 1
                keep_positive_context_candidate(
                    conn,
                    nct_id,
                    f"low_text_rule_kept:{keep_pattern}",
                    f"Rule keep: low-confidence text contains strong oncology support/prevention/survivorship context: {keep_pattern}",
                    dry_run=dry_run,
                )
                continue

            source = recall_source_by_id.get(nct_id, "")
            source_field = source.split(":", 1)[0] if ":" in source else ""
            matched_term = source.split(":", 1)[1].lower() if ":" in source else source.lower()
            recalled_text = fields.get(source_field, "") or all_text

            false_pattern = any_pattern(recalled_text, rules["text_false_positive_patterns"])
            if false_pattern:
                summary["low_text_rule_rejected_tnf"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:{matched_term or false_pattern}",
                    f"Rule reject: low-confidence recall matched non-oncology TNF/tumor-necrosis-factor pattern '{false_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            if source_field == "eligibility":
                exclusion_pattern = any_pattern(fields.get("eligibility", ""), rules["eligibility_exclusion_patterns"])
                if exclusion_pattern:
                    summary["low_text_rule_rejected_eligibility_exclusion"] += 1
                    reject_low_text_false_positive(
                        conn,
                        nct_id,
                        f"low_text_rule_excluded:eligibility_{matched_term}",
                        f"Rule reject: eligibility-only cancer term appears as an exclusion or exception pattern '{exclusion_pattern}'.",
                        dry_run=dry_run,
                    )
                    continue

            abbreviation_pattern = any_pattern(all_text, rules["low_text_abbreviation_artifact_patterns"])
            if abbreviation_pattern:
                summary["low_text_rule_rejected_abbreviation_artifact"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:abbr_{matched_term}",
                    f"Aggressive rule reject: low-confidence recall matched abbreviation artifact '{abbreviation_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            non_oncology_pattern = any_pattern(all_text, rules["low_text_non_oncology_meaning_patterns"])
            if non_oncology_pattern:
                summary["low_text_rule_rejected_non_oncology_meaning"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:nononcology_{matched_term}",
                    f"Aggressive rule reject: cancer recall term has non-oncology meaning '{non_oncology_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            benign_pattern = any_pattern(all_text, rules["low_text_benign_or_nonmalignant_patterns"])
            if benign_pattern:
                summary["low_text_rule_rejected_benign_neoplasm"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:benign_{matched_term}",
                    f"Aggressive rule reject: benign or non-malignant neoplasm-like context '{benign_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            infection_pattern = any_pattern(all_text, rules["low_text_infection_background_patterns"])
            if infection_pattern:
                summary["low_text_rule_rejected_infection_background"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:infection_{matched_term}",
                    f"Aggressive rule reject: cancer term is background in infection/hepatitis/HIV context '{infection_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            drug_pattern = any_pattern(recalled_text, rules["low_text_drug_prior_oncology_use_patterns"])
            if drug_pattern:
                summary["low_text_rule_rejected_drug_prior_oncology_use"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:drug_history_{matched_term}",
                    f"Aggressive rule reject: cancer term only describes drug prior/other oncology indication '{drug_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            admin_pattern = any_pattern(recalled_text, rules["administrative_or_instrument_patterns"])
            if admin_pattern and source_field in {"description", "summary", "keywords"}:
                summary["low_text_rule_rejected_admin_artifact"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:admin_{matched_term}",
                    f"Rule reject: cancer term appears in administrative/instrument text rather than oncology target context: '{admin_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            epidemiology_pattern = any_pattern(all_text, rules["low_text_epidemiology_background_patterns"])
            if epidemiology_pattern:
                summary["low_text_rule_rejected_epidemiology_background"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:epidemiology_{matched_term}",
                    f"Aggressive rule reject: chronic disease epidemiology/background context '{epidemiology_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            risk_pattern = any_pattern(recalled_text, rules["risk_only_patterns"])
            if risk_pattern and source_field in {"description", "summary"}:
                summary["low_text_rule_rejected_risk_only"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:risk_{matched_term}",
                    f"Aggressive rule reject: cancer term appears as background risk/downstream epidemiology '{risk_pattern}'.",
                    dry_run=dry_run,
                )
                continue

            title_condition_text = " | ".join([fields.get("study", ""), fields.get("conditions", "")])
            if source_field in {"description", "summary", "keywords"} and not matcher.match(title_condition_text):
                summary["low_text_rule_rejected_weak_text_no_title_condition_anchor"] += 1
                reject_low_text_false_positive(
                    conn,
                    nct_id,
                    f"low_text_rule_excluded:weak_text_{matched_term}",
                    "Aggressive rule reject: cancer term appears only in weak text fields without title/condition cancer anchor, strong MeSH, or rule-keep context.",
                    dry_run=dry_run,
                )
                continue

            summary["low_text_remaining_for_review"] += 1
    return summary


def apply_strong_browse_promotions(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = MESH_TERMS_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    rules = load_mesh_rules(rules_path)
    ids = pending_review_candidate_ids(conn, include_reviewed=include_reviewed)
    terms_by_id = read_browse_terms(files, ids)
    summary: Counter[str] = Counter()
    with conn if not dry_run else nullcontext(conn):
        for nct_id in sorted(ids):
            category, matched_terms = classify_terms(terms_by_id.get(nct_id, set()), rules)
            if category in STRONG_BROWSE_CATEGORIES:
                summary["browse_strong_promoted_from_pending"] += 1
                update_browse_candidate(conn, nct_id, category, matched_terms, dry_run=dry_run)
    return summary

def apply_eligibility_false_positive_rules(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = MESH_TERMS_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    rules = load_mesh_rules(rules_path)
    ids = eligibility_candidate_ids(conn, include_reviewed=include_reviewed)
    browse_terms = read_browse_terms(files, ids)
    review_text = read_low_review_text(files, ids)
    matcher = common.TermMatcher(common.load_terms())
    summary: Counter[str] = Counter()
    with conn if not dry_run else nullcontext(conn):
        for nct_id in sorted(ids):
            browse_category, browse_matches = classify_terms(browse_terms.get(nct_id, set()), rules)
            if browse_category in STRONG_BROWSE_CATEGORIES:
                summary["eligibility_only_protected_by_browse"] += 1
                continue
            fields = review_text.get(nct_id, {})
            title_condition_text = " | ".join([fields.get("study", ""), fields.get("conditions", "")])
            anchor = matcher.match(title_condition_text)
            if anchor:
                summary["eligibility_only_protected_by_title_or_condition_anchor"] += 1
                continue
            summary["eligibility_only_rule_rejected"] += 1
            source = "eligibility_rule_excluded:no_title_condition_or_strong_mesh_anchor"
            if browse_matches:
                source = f"{source};browse={browse_category}:{browse_matches[0]}"
            reject_eligibility_false_positive(conn, nct_id, source, dry_run=dry_run)
    summary["eligibility_only_total_checked"] = len(ids)
    return summary


def apply_pre_llm_rules(
    conn: sqlite3.Connection,
    files: Any,
    rules_path: Path = MESH_TERMS_FILE,
    false_positive_rules_path: Path = FALSE_POSITIVE_RULES_FILE,
    include_reviewed: bool = False,
    dry_run: bool = False,
) -> Counter[str]:
    summary = Counter()
    summary.update(
        apply_browse_condition_rules(
            conn,
            files,
            rules_path=rules_path,
            include_reviewed=include_reviewed,
            dry_run=dry_run,
        )
    )
    summary.update(
        apply_strong_browse_promotions(
            conn,
            files,
            rules_path=rules_path,
            include_reviewed=include_reviewed,
            dry_run=dry_run,
        )
    )
    summary.update(
        apply_intervention_false_positive_rules(
            conn,
            files,
            rules_path=false_positive_rules_path,
            mesh_rules_path=rules_path,
            include_reviewed=include_reviewed,
            dry_run=dry_run,
        )
    )
    summary.update(
        apply_low_text_false_positive_rules(
            conn,
            files,
            rules_path=false_positive_rules_path,
            mesh_rules_path=rules_path,
            include_reviewed=include_reviewed,
            dry_run=dry_run,
        )
    )
    summary.update(
        apply_eligibility_false_positive_rules(
            conn,
            files,
            rules_path=rules_path,
            include_reviewed=include_reviewed,
            dry_run=dry_run,
        )
    )
    return summary


class nullcontext:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, *exc: Any) -> None:
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=common.DEFAULT_DB)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aact-zip", type=Path)
    group.add_argument("--aact-dir", type=Path)
    parser.add_argument("--mesh-rules", type=Path, default=MESH_TERMS_FILE)
    parser.add_argument("--false-positive-rules", type=Path, default=FALSE_POSITIVE_RULES_FILE)
    parser.add_argument("--include-reviewed", action="store_true", help="Also reprocess candidates already kept/rejected/errored.")
    parser.add_argument("--dry-run", action="store_true", help="Print rule counts without writing changes.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import build_aact_source_db as aact

    args = parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    files = aact.AACTFiles(zip_path=args.aact_zip, dir_path=args.aact_dir)
    try:
        aact.init_aact_tables(conn)
        summary = apply_pre_llm_rules(
            conn,
            files,
            rules_path=args.mesh_rules,
            false_positive_rules_path=args.false_positive_rules,
            include_reviewed=args.include_reviewed,
            dry_run=args.dry_run,
        )
        print("pre_llm_rule_preprocess_summary:")
        for key in [
            "browse_malignant_or_cancer_specific",
            "browse_precancer_or_prevention",
            "browse_broad_or_ambiguous_review",
            "browse_benign_or_non_oncology_exclude",
            "browse_unclassified_review",
            "browse_strong_promoted_from_pending",
            "intervention_total_checked",
            "intervention_rule_rejected_false_pattern",
            "intervention_rule_rejected_abbreviation_context",
            "intervention_rule_kept_positive_context",
            "intervention_protected_by_title_or_condition_anchor",
            "intervention_protected_by_weak_context_anchor",
            "intervention_protected_by_browse",
            "intervention_rule_rejected_admin_artifact",
            "intervention_rule_rejected_risk_only",
            "intervention_rule_rejected_benign_neoplasm",
            "intervention_rule_rejected_non_oncology_meaning",
            "intervention_remaining_for_review",
            "low_text_total_checked",
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
            "low_text_rule_rejected_weak_text_no_title_condition_anchor",
            "low_text_rule_kept_positive_context",
            "low_text_protected_by_browse",
            "low_text_remaining_for_review",
            "eligibility_only_total_checked",
            "eligibility_only_rule_rejected",
            "eligibility_only_protected_by_title_or_condition_anchor",
            "eligibility_only_protected_by_browse",
        ]:
            print(f"  {key}={summary.get(key, 0):,}")
    finally:
        files.close()
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])






