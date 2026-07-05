---
name: clinicaltrials-gov-cancer-source-builder
description: Build or refresh the ClinicalTrials.gov oncology trial source-local database from AACT or the ClinicalTrials.gov API, including recall, preprocessing, LLM/manual review import, registry loading, and quality checks.
license: MIT
metadata:
  author: CancerDAO
  version: "0.5.0"
  parent_skill: global-cancer-trials-db-orchestrator
---

# ClinicalTrials.gov Cancer Source Builder

This child skill owns the full ClinicalTrials.gov / AACT source workflow.

## Scope

Source folder:

```text
sources/clinicaltrials_gov/
```

Default source-local database:

```text
sources/clinicaltrials_gov/data/ctgov_cancer_trials.db
```

AACT archive used for the current build:

```text
sources/clinicaltrials_gov/data/aact_export_2026-07-03.zip
```

## Responsibilities

Python handles deterministic work:

- Read AACT flat files from zip or extracted directory.
- Initialize the shared registry schema.
- Apply high-recall oncology candidate scanning.
- Apply ClinicalTrials.gov-specific MeSH and false-positive preprocessing.
- Export compact current-conversation review batches.
- Import reviewed LLM/manual decisions.
- Load reviewed-only records into the six registry tables.
- Validate counts, completeness, and recall quality.

LLM/manual review is used only after deterministic preprocessing for uncertain medium/low candidates.

## Source-Specific Config

```text
config/cancer_recall_terms.yaml
config/cancer_mesh_terms.yaml
config/pre_llm_false_positive_rules.yaml
```

These files are ClinicalTrials.gov / AACT specific and should not be treated as the global source-agnostic vocabulary.

## Main Commands

Schema smoke test:

```bash
python sources/clinicaltrials_gov/scripts/test_build_ctgov_cancer_db.py
```

Build from AACT zip:

```bash
python sources/clinicaltrials_gov/scripts/build_aact_source_db.py \
  --aact-zip sources/clinicaltrials_gov/data/aact_export_2026-07-03.zip \
  --out sources/clinicaltrials_gov/data/ctgov_cancer_trials.db \
  --batch-size 50000 \
  --low-confidence-policy reviewed-only
```

Monitor build state:

```bash
python sources/clinicaltrials_gov/scripts/monitor_aact_build.py \
  --db sources/clinicaltrials_gov/data/ctgov_cancer_trials.db
```

Export current-conversation review batch:

```bash
python sources/clinicaltrials_gov/scripts/export_conversation_review_batch.py \
  --db sources/clinicaltrials_gov/data/ctgov_cancer_trials.db \
  --aact-zip sources/clinicaltrials_gov/data/aact_export_2026-07-03.zip \
  --tier all \
  --limit 100 \
  --batch-id BATCH_ID
```

Import review results:

```bash
python sources/clinicaltrials_gov/scripts/import_conversation_review_results.py \
  --db sources/clinicaltrials_gov/data/ctgov_cancer_trials.db \
  --results sources/clinicaltrials_gov/data/quality_reports/conversation_review_all_pending_20260704.results.jsonl
```

## Quality Reports

ClinicalTrials.gov quality reports live under:

```text
sources/clinicaltrials_gov/data/quality_reports/
```
