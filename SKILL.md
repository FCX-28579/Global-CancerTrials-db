---
name: global-cancer-trials-db-orchestrator
description: Coordinate source-specific cancer trial database builders and merge reviewed source-local outputs into a unified registry database for clinical trial matching.
license: MIT
metadata:
  author: CancerDAO
  version: "0.5.0"
  parent_skill: clinical-trial-matching
---

# Global Cancer Trials DB Orchestrator

This parent skill coordinates multiple source-specific builders. It does not own registry-specific crawling, recall rules, or source-specific LLM review prompts.

## Source Skills

- `sources/clinicaltrials_gov/SKILL.md`: ClinicalTrials.gov / AACT source builder.
- `sources/china_chictr/SKILL.md`: ChiCTR source builder design and implementation area.

## Parent Responsibilities

- Keep the shared registry schema in `schemas/registry_schema.sql`.
- Define source boundaries and expected normalized output contracts.
- Run source builders through their child skills.
- Merge reviewed source-local databases into a final multi-source database.
- Detect duplicate registry records across sources when that integration layer is implemented.
- Produce source-comparison and final integrated quality reports.

## Child Source Responsibilities

Each child source owns:

- Source acquisition or source import.
- Source-specific recall terms and preprocessing rules.
- Source-specific LLM/manual review batching.
- Source-local SQLite output.
- Source-local quality reports.

## Shared Output Contract

Every source builder should produce the six registry tables defined by `schemas/registry_schema.sql`:

- `raw_trial_records`
- `trial_master`
- `trial_registry_ids`
- `trial_interventions`
- `trial_eligibility_criteria`
- `trial_sites`

The parent skill treats source-local databases as inputs. It should not reinterpret source-specific raw pages unless a child source explicitly exposes that data.

## Boundary Rule

ClinicalTrials.gov-specific files belong under `sources/clinicaltrials_gov/`.
ChiCTR-specific files belong under `sources/china_chictr/`.
The root project keeps only shared schema, parent documentation, dependencies, and future integration code.
