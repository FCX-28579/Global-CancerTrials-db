# Global Cancer Trials DB

`global-cancer-trials-db` is organized as one parent orchestration skill plus source-specific child skills.

The parent project keeps shared schema and integration boundaries. Each source folder owns its own acquisition logic, recall configuration, review workflow, output database, and quality reports.

## Project Structure

```text
global-cancer-trials-db/
  README.md
  SKILL.md
  requirements.txt
  schemas/
    registry_schema.sql
  data/
    .gitkeep
  sources/
    clinicaltrials_gov/
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
        build_aact_source_db.py
        build_source_db.py
        preprocess_browse_conditions.py
        export_conversation_review_batch.py
        import_conversation_review_results.py
        review_low_confidence_candidates.py
        monitor_aact_build.py
        test_build_ctgov_cancer_db.py
    china_chictr/
      SKILL.md
      README.md
      config/
      data/
      scripts/
```

## Parent Role

The parent skill is responsible for orchestration and final integration:

- Run source-specific child builders.
- Check that each source-local database follows `schemas/registry_schema.sql`.
- Merge source-local outputs into a future integrated multi-source database.
- Produce final cross-source quality reports.

The parent skill should not contain AACT-specific, ClinicalTrials.gov-specific, or ChiCTR-specific recall rules.

## Current Source Outputs

ClinicalTrials.gov final source-local database:

```text
sources/clinicaltrials_gov/data/ctgov_cancer_trials.db
```

ClinicalTrials.gov quality reports:

```text
sources/clinicaltrials_gov/data/quality_reports/
```

## Shared Registry Tables

The shared schema defines six normalized tables:

| Table | Purpose |
| --- | --- |
| `raw_trial_records` | Source JSON/raw payload for retained trials. |
| `trial_master` | One row per retained trial with status, phase, condition, intervention summary, and source URL. |
| `trial_registry_ids` | Registry identifiers such as NCT ID or ChiCTR number. |
| `trial_interventions` | Intervention names and types. |
| `trial_eligibility_criteria` | Inclusion/exclusion criteria blocks. |
| `trial_sites` | Trial site or facility rows when the source provides reliable site data. |

## Source Boundary

- ClinicalTrials.gov / AACT logic lives in `sources/clinicaltrials_gov/`.
- ChiCTR logic lives in `sources/china_chictr/`.
- Root-level code should be source-agnostic.
