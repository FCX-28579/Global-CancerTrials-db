# ClinicalTrials.gov Source Builder

This source project builds the ClinicalTrials.gov cancer trial source-local database.

Primary path: AACT static copy or local AACT files.
Fallback path: ClinicalTrials.gov API v2.

## Source Layout

```text
sources/clinicaltrials_gov/
  SKILL.md
  README.md
  config/
    cancer_recall_terms.yaml
    cancer_mesh_terms.yaml
    pre_llm_false_positive_rules.yaml
  data/
    ctgov_cancer_trials.db
    aact_export_2026-07-03.zip
    quality_reports/
    review_batches/
  scripts/
```

## Default Output

```text
data/ctgov_cancer_trials.db
```

No local absolute paths should be hard-coded. External AACT files should be passed by CLI arguments.

## Smoke Test

```bash
python sources/clinicaltrials_gov/scripts/test_build_ctgov_cancer_db.py
```

## Build From AACT

```bash
python sources/clinicaltrials_gov/scripts/build_aact_source_db.py \
  --aact-zip sources/clinicaltrials_gov/data/aact_export_2026-07-03.zip \
  --out sources/clinicaltrials_gov/data/ctgov_cancer_trials.db \
  --batch-size 50000 \
  --low-confidence-policy reviewed-only
```
