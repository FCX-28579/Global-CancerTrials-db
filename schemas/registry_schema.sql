-- Registry layer schema for local global cancer clinical trials database.
-- Initial target: SQLite. Keep raw records and normalized matching tables separate.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_trial_records (
    raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_trial_id TEXT NOT NULL,
    source_url TEXT,
    raw_json TEXT,
    raw_html_path TEXT,
    fetched_at TEXT NOT NULL,
    parser_version TEXT,
    fetch_status TEXT DEFAULT 'success',
    UNIQUE(source_name, source_trial_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS trial_master (
    trial_uid TEXT PRIMARY KEY,
    primary_registry_id TEXT NOT NULL,
    primary_source TEXT NOT NULL,
    title TEXT,
    scientific_title TEXT,
    brief_summary TEXT,
    recruitment_status_raw TEXT,
    recruitment_status_normalized TEXT,
    phase_raw TEXT,
    phase_normalized TEXT,
    study_type_raw TEXT,
    study_type_normalized TEXT,
    disease_text TEXT,
    disease_normalized TEXT,
    cancer_type_normalized TEXT,
    intervention_summary TEXT,
    sponsor_summary TEXT,
    countries TEXT,
    registration_date TEXT,
    start_date TEXT,
    completion_date TEXT,
    last_update_date TEXT,
    source_url TEXT,
    last_fetched_at TEXT,
    cancer_recall_source TEXT,
    cancer_recall_confidence TEXT,
    data_quality_status TEXT DEFAULT 'unreviewed'
);

CREATE TABLE IF NOT EXISTS trial_registry_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_uid TEXT NOT NULL,
    registry_source TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    id_type TEXT,
    is_primary INTEGER DEFAULT 0,
    source_url TEXT,
    FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE,
    UNIQUE(registry_source, registry_id)
);

CREATE TABLE IF NOT EXISTS trial_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_uid TEXT NOT NULL,
    arm_name TEXT,
    arm_type TEXT,
    sample_size INTEGER,
    intervention_name_raw TEXT,
    intervention_name_normalized TEXT,
    intervention_type TEXT,
    drug_name_normalized TEXT,
    drug_aliases TEXT,
    target TEXT,
    mechanism TEXT,
    therapy_class TEXT,
    is_combination INTEGER,
    FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trial_eligibility_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_uid TEXT NOT NULL,
    criterion_type TEXT NOT NULL CHECK (criterion_type IN ('inclusion', 'exclusion', 'unknown')),
    criterion_text TEXT NOT NULL,
    language TEXT,
    criterion_order INTEGER,
    parsed_category TEXT,
    is_critical INTEGER DEFAULT 0,
    normalized_entities TEXT,
    source_section TEXT,
    FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trial_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_uid TEXT NOT NULL,
    site_name TEXT,
    country TEXT,
    province TEXT,
    city TEXT,
    site_status TEXT,
    investigator TEXT,
    contact_name TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    source_name TEXT,
    source_url TEXT,
    last_verified_at TEXT,
    FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trial_master_primary_id
    ON trial_master(primary_registry_id);

CREATE INDEX IF NOT EXISTS idx_trial_master_status
    ON trial_master(recruitment_status_normalized);

CREATE INDEX IF NOT EXISTS idx_trial_master_cancer
    ON trial_master(cancer_type_normalized);

CREATE INDEX IF NOT EXISTS idx_trial_registry_ids_trial
    ON trial_registry_ids(trial_uid);

CREATE INDEX IF NOT EXISTS idx_trial_interventions_trial
    ON trial_interventions(trial_uid);

CREATE INDEX IF NOT EXISTS idx_trial_criteria_trial
    ON trial_eligibility_criteria(trial_uid);

CREATE INDEX IF NOT EXISTS idx_trial_sites_trial
    ON trial_sites(trial_uid);


-- Views designed for clinical-trial-matching-skill consumption.

DROP VIEW IF EXISTS skill_trial_candidates_view;

CREATE VIEW skill_trial_candidates_view AS
SELECT
    tm.trial_uid,
    tm.primary_registry_id AS display_id,
    tm.primary_source AS source,
    tm.title,
    tm.scientific_title,
    tm.brief_summary,
    tm.recruitment_status_normalized,
    tm.phase_normalized,
    tm.study_type_normalized,
    tm.disease_text,
    tm.disease_normalized,
    tm.cancer_type_normalized,
    tm.intervention_summary,
    tm.sponsor_summary,
    tm.countries,
    tm.last_update_date,
    tm.source_url,
    tm.cancer_recall_confidence,
    (
        SELECT COUNT(*)
        FROM trial_sites ts
        WHERE ts.trial_uid = tm.trial_uid
          AND lower(COALESCE(ts.country, '')) IN ('china', '中国', 'cn')
    ) AS china_site_count
FROM trial_master tm;

DROP VIEW IF EXISTS trial_matching_features_view;

CREATE VIEW trial_matching_features_view AS
SELECT
    tm.trial_uid,
    tm.primary_registry_id,
    lower(COALESCE(tm.title, '') || ' ' || COALESCE(tm.scientific_title, '') || ' ' || COALESCE(tm.brief_summary, '')) AS title_summary_text,
    lower(COALESCE(tm.disease_text, '') || ' ' || COALESCE(tm.disease_normalized, '') || ' ' || COALESCE(tm.cancer_type_normalized, '')) AS disease_terms,
    lower(COALESCE(tm.intervention_summary, '') || ' ' ||
        COALESCE((SELECT group_concat(intervention_name_raw, ' | ') FROM trial_interventions ti WHERE ti.trial_uid = tm.trial_uid), '')
    ) AS intervention_terms,
    lower(COALESCE((SELECT group_concat(criterion_text, ' | ') FROM trial_eligibility_criteria te WHERE te.trial_uid = tm.trial_uid), '')) AS eligibility_terms,
    tm.recruitment_status_normalized,
    tm.phase_normalized,
    tm.cancer_recall_confidence
FROM trial_master tm;


