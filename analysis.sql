-- SQL sections used by the analysis notebook.
-- Each section is selected by filename; do not execute the whole file as one batch.

-- BEGIN FILE: 03_annotation_audit.sql
-- Private QA workspace. Raw rows and source IDs are retained locally.
-- name: keys
SELECT COUNT(*) AS records,
 COUNT(DISTINCT id) AS row_ids,
 COUNT(DISTINCT item_id) AS items,
 COUNT(DISTINCT rater_id) AS raters,
 (SELECT COUNT(*) FROM (SELECT item_id,rater_id FROM raw_annotations GROUP BY item_id,rater_id)) AS pairs
FROM raw_annotations;

-- name: flag_counts
SELECT SUM(reason_status='invalid_json') AS invalid_reason_json_rows,
 SUM(reason_status='invalid_shape') AS invalid_reason_shape_rows,
 COUNT(DISTINCT CASE WHEN reason_status NOT IN ('valid_empty','valid_nonempty') THEN item_id END) AS invalid_reason_items,
 SUM(q1_ok=0) AS q1_issue_rows,
 SUM(published_excluded) AS published_excluded_rows,
 SUM(invalid_labels) AS invalid_safety_cells,
 SUM(aggregate_mismatches) AS aggregate_mismatch_cells,
 SUM(invalid_time) AS invalid_time_rows
FROM audit_flags;

-- name: item_metadata
SELECT SUM(text_variants>1) AS text_conflict_items,
 SUM(reference_variants>1) AS reference_conflict_items,
 SUM(reason_variants>1) AS reason_conflict_items,
 SUM(severity_variants>1) AS severity_conflict_items,
 SUM(topic_variants>1) AS topic_conflict_items
FROM (
 SELECT item_id,COUNT(DISTINCT context||char(0)||response) AS text_variants,
 COUNT(DISTINCT safety_gold) AS reference_variants,
 COUNT(DISTINCT safety_gold_reason) AS reason_variants,
 COUNT(DISTINCT degree_of_harm) AS severity_variants,
 COUNT(DISTINCT harm_type) AS topic_variants
 FROM raw_annotations GROUP BY item_id
);

-- name: rater_metadata
SELECT SUM(gender_variants>1) AS gender_conflict_raters,
 SUM(race_variants>1) AS race_conflict_raters,
 SUM(raw_race_variants>1) AS raw_race_conflict_raters,
 SUM(age_variants>1) AS age_conflict_raters,
 SUM(education_variants>1) AS education_conflict_raters
FROM (
 SELECT rater_id,COUNT(DISTINCT rater_gender) AS gender_variants,
 COUNT(DISTINCT rater_race) AS race_variants,COUNT(DISTINCT rater_raw_race) AS raw_race_variants,
 COUNT(DISTINCT rater_age) AS age_variants,COUNT(DISTINCT rater_education) AS education_variants
 FROM raw_annotations GROUP BY rater_id
);

-- name: reason_status
SELECT reason_status,COUNT(*) AS records,COUNT(DISTINCT item_id) AS items
FROM audit_flags GROUP BY reason_status ORDER BY reason_status;

-- name: constant_overall_patterns
SELECT constant_overall,MAX(published_excluded) AS published_excluded,COUNT(*) AS raters
FROM (
 SELECT a.rater_id,CASE WHEN COUNT(DISTINCT a.Q_overall)=1 THEN 1 ELSE 0 END AS constant_overall,
 MAX(f.published_excluded) AS published_excluded
 FROM raw_annotations a JOIN audit_flags f USING(source_row) GROUP BY a.rater_id
) GROUP BY constant_overall,published_excluded ORDER BY constant_overall,published_excluded;

-- name: duplicate_content
SELECT COUNT(*) AS shared_text_clusters,COALESCE(SUM(items),0) AS distinct_item_ids_in_clusters
FROM (SELECT context,response,COUNT(DISTINCT item_id) AS items FROM raw_annotations
      GROUP BY context,response HAVING COUNT(DISTINCT item_id)>1);

-- name: join_cardinality
SELECT COUNT(*) AS joined_records,COUNT(DISTINCT a.source_row) AS unique_source_rows
FROM raw_annotations a JOIN audit_flags f USING(source_row);

-- BEGIN FILE: 04_item_metrics.sql
-- Per-item distributions. Unsure does not become a binary safe/unsafe decision.
WITH counts AS (
 SELECT item_id,COUNT(*) AS n,SUM(judgment='Yes') AS yes,
 SUM(judgment='No') AS no,SUM(judgment='Unsure') AS unsure FROM ratings GROUP BY item_id
), totals AS (SELECT *,yes+no AS d FROM counts)
SELECT *,1.0*unsure/n AS I01,
 CASE WHEN d>=2 THEN 2.0*yes*no/(d*(d-1)) END AS I02,
 (d=0) AS all_unsure,(d<2) AS insufficient_decisive,
 (d>0 AND yes=no) AS decisive_tie FROM totals ORDER BY item_id;

-- BEGIN FILE: 04_reference_scorecard.sql
-- Synthetic/reference implementation. Input: one unique rater-item judgment.
-- Reference is a benchmark; Unsure is a separate third column.
WITH cells AS (
 SELECT COUNT(*) AS n,COUNT(DISTINCT item_id) AS items,COUNT(DISTINCT rater_id) AS raters,
 COALESCE(SUM(reference='Yes' AND judgment='Yes'),0) AS tp,
 COALESCE(SUM(reference='Yes' AND judgment='No'),0) AS fn,
 COALESCE(SUM(reference='Yes' AND judgment='Unsure'),0) AS uy,
 COALESCE(SUM(reference='No' AND judgment='Yes'),0) AS fp,
 COALESCE(SUM(reference='No' AND judgment='No'),0) AS tn,
 COALESCE(SUM(reference='No' AND judgment='Unsure'),0) AS un FROM ratings
), totals AS (
 SELECT *,tp+fn+fp+tn AS d,uy+un AS u,tp+tn AS matches,
 tp+fn+uy AS ref_unsafe,fp+tn+un AS ref_safe FROM cells
)
SELECT *,
 1.0*d/NULLIF(n,0) AS R01,1.0*u/NULLIF(n,0) AS R02,
 1.0*matches/NULLIF(n,0) AS R03,1.0*matches/NULLIF(d,0) AS R04,
 1.0*tp/NULLIF(tp+fp,0) AS R05,1.0*tp/NULLIF(tp+fn,0) AS R06,
 1.0*fn/NULLIF(tp+fn,0) AS R07,1.0*fp/NULLIF(fp+tn,0) AS R08,
 1.0*tp/NULLIF(ref_unsafe,0) AS R09,1.0*fn/NULLIF(ref_unsafe,0) AS R10,
 1.0*uy/NULLIF(ref_unsafe,0) AS R11,1.0*fp/NULLIF(ref_safe,0) AS R12,
 1.0*un/NULLIF(ref_safe,0) AS R13,1.0*tn/NULLIF(ref_safe,0) AS R14,
 1.0*matches/NULLIF(n,0) AS agreement_lower,
 1.0*(matches+u)/NULLIF(n,0) AS agreement_upper,
 1.0*tp/NULLIF(ref_unsafe,0) AS detection_lower,
 1.0*(tp+uy)/NULLIF(ref_unsafe,0) AS detection_upper
FROM totals;

-- BEGIN FILE: 05_create_qa_model.sql
-- Private normalized model. Raw text/identifiers are not public BI inputs.
PRAGMA foreign_keys=ON;
CREATE TABLE model_config (
 id INTEGER PRIMARY KEY CHECK(id=1),source_sha256 TEXT NOT NULL,
 contract_sha256 TEXT NOT NULL,contract_version TEXT NOT NULL,source_revision TEXT NOT NULL,
 field_order_json TEXT NOT NULL
);
CREATE TABLE source_record (
 source_row INTEGER PRIMARY KEY,record_id TEXT NOT NULL UNIQUE,
 raw_json TEXT NOT NULL,raw_values_sha256 TEXT NOT NULL,
 UNIQUE(source_row,record_id)
);
CREATE TABLE dim_item (
 item_id TEXT PRIMARY KEY,context TEXT NOT NULL,response TEXT NOT NULL,
 source_text_sha256 TEXT NOT NULL,first_source_row INTEGER NOT NULL REFERENCES source_record(source_row)
);
CREATE TABLE dim_rater (
 rater_id TEXT PRIMARY KEY,rater_gender TEXT NOT NULL,rater_race TEXT NOT NULL,
 rater_raw_race TEXT NOT NULL,rater_age TEXT NOT NULL,rater_education TEXT NOT NULL,
 published_excluded INTEGER NOT NULL CHECK(published_excluded IN (0,1)),
 first_source_row INTEGER NOT NULL REFERENCES source_record(source_row)
);
CREATE TABLE fact_expert_reference (
 item_id TEXT PRIMARY KEY REFERENCES dim_item(item_id),
 reference_label TEXT NOT NULL CHECK(reference_label IN ('Yes','No')),
 degree_of_harm TEXT NOT NULL CHECK(degree_of_harm IN ('Benign','Debatable','Moderate','Extreme')),
 raw_harm_type TEXT NOT NULL,reason_raw TEXT NOT NULL,
 reason_status TEXT NOT NULL CHECK(reason_status IN ('valid_empty','valid_nonempty','invalid_json','invalid_shape')),
 first_source_row INTEGER NOT NULL REFERENCES source_record(source_row)
);
CREATE TABLE dim_topic (topic TEXT PRIMARY KEY);
CREATE TABLE bridge_item_topic (
 item_id TEXT NOT NULL REFERENCES dim_item(item_id),topic TEXT NOT NULL REFERENCES dim_topic(topic),
 PRIMARY KEY(item_id,topic)
) WITHOUT ROWID;
CREATE TABLE dim_reason_code (code TEXT PRIMARY KEY);
CREATE TABLE fact_expert_reason (
 item_id TEXT NOT NULL REFERENCES fact_expert_reference(item_id),
 reason_position INTEGER NOT NULL CHECK(reason_position>=0),code TEXT NOT NULL REFERENCES dim_reason_code(code),
 PRIMARY KEY(item_id,reason_position)
) WITHOUT ROWID;
CREATE TABLE dim_question (
 question_id INTEGER PRIMARY KEY,source_column TEXT NOT NULL UNIQUE,
 question_group TEXT NOT NULL,question_kind TEXT NOT NULL CHECK(question_kind IN ('granular','aggregate')),
 source_position INTEGER NOT NULL UNIQUE,documented_alias TEXT
);
CREATE TABLE fact_judgment (
 source_row INTEGER PRIMARY KEY,record_id TEXT NOT NULL UNIQUE,
 item_id TEXT NOT NULL REFERENCES dim_item(item_id),rater_id TEXT NOT NULL REFERENCES dim_rater(rater_id),
 phase TEXT NOT NULL,answer_time_ms INTEGER NOT NULL CHECK(answer_time_ms>=0),
 answer_timestamp INTEGER NOT NULL CHECK(answer_timestamp>=0),
 q1_value TEXT NOT NULL,q1_ok INTEGER NOT NULL CHECK(q1_ok IN (0,1)),
 overall TEXT NOT NULL CHECK(overall IN ('Yes','No','Unsure')),
 FOREIGN KEY(source_row,record_id) REFERENCES source_record(source_row,record_id),
 UNIQUE(item_id,rater_id)
);
CREATE INDEX judgment_item ON fact_judgment(item_id);
CREATE INDEX judgment_rater ON fact_judgment(rater_id);
CREATE TABLE fact_answer (
 source_row INTEGER NOT NULL REFERENCES fact_judgment(source_row),
 question_id INTEGER NOT NULL REFERENCES dim_question(question_id),
 answer TEXT NOT NULL CHECK(answer IN ('Yes','No','Unsure')),
 PRIMARY KEY(source_row,question_id)
) WITHOUT ROWID;
CREATE INDEX answer_question ON fact_answer(question_id,source_row);

-- All scorecard calculations start from this one-row-per-source-judgment view.
CREATE VIEW ratings AS
 SELECT j.record_id,j.item_id,j.rater_id,e.reference_label AS reference,j.overall AS judgment
 FROM fact_judgment j JOIN fact_expert_reference e USING(item_id);
CREATE VIEW ratings_published_104 AS
 SELECT x.* FROM ratings x JOIN dim_rater r USING(rater_id) WHERE r.published_excluded=0;
CREATE VIEW ratings_q1 AS
 SELECT x.* FROM ratings x JOIN fact_judgment j USING(record_id) WHERE j.q1_ok=1;
CREATE VIEW rating_reporting AS
 SELECT x.*,e.degree_of_harm,e.reason_status,j.q1_value,j.q1_ok,
 j.answer_time_ms,j.answer_timestamp,r.published_excluded
 FROM ratings x JOIN fact_expert_reference e USING(item_id)
 JOIN fact_judgment j USING(record_id) JOIN dim_rater r USING(rater_id);

-- BEGIN FILE: 06_quality_baseline.sql
-- Parameter :severity is one observed source label. NULL selects all records.
-- Parameter :topic is one observed topic token. NULL selects all records.
-- One record per rater-item judgment is preserved, even for multi-topic items.
SELECT r.record_id,r.item_id,r.rater_id,r.reference,r.judgment
FROM ratings AS r
JOIN fact_expert_reference AS e ON e.item_id=r.item_id
WHERE (:severity IS NULL OR e.degree_of_harm=:severity)
  AND (:topic IS NULL OR EXISTS (
    SELECT 1 FROM bridge_item_topic AS b
    WHERE b.item_id=r.item_id AND b.topic=:topic
  ));

-- BEGIN FILE: 07_expert_reason_profiles.sql
-- DISTINCT prevents ordered reason-list positions duplicating an item-code link.
-- Parsed reason groups overlap and are not granular question gold labels.
WITH membership AS (SELECT DISTINCT item_id,code FROM fact_expert_reason)
SELECT m.code,COUNT(*) AS reference_unsafe_records,
       COUNT(DISTINCT r.item_id) AS items,COUNT(DISTINCT r.rater_id) AS raters,
       SUM(r.judgment='Yes') AS yes,SUM(r.judgment='No') AS no,
       SUM(r.judgment='Unsure') AS unsure
FROM membership AS m
JOIN ratings AS r USING(item_id)
WHERE r.reference='Yes'
GROUP BY m.code ORDER BY m.code;

-- BEGIN FILE: 07_fp_question_triggers.sql
-- A Yes on this question contributes to an overall Yes on reference-No rows.
-- Denominator is the OVERALL FP cohort, not question-specific ground truth.
SELECT q.source_column AS question, q.question_group,
       COUNT(*) AS cohort_records, COUNT(DISTINCT j.item_id) AS cohort_items,
       COUNT(DISTINCT j.rater_id) AS cohort_raters,
       SUM(a.answer='Yes') AS yes_records,
       COUNT(DISTINCT CASE WHEN a.answer='Yes' THEN j.item_id END) AS yes_items
FROM fact_answer AS a
JOIN dim_question AS q USING(question_id)
JOIN fact_judgment AS j USING(source_row)
JOIN fact_expert_reference AS e USING(item_id)
WHERE q.question_kind='granular' AND e.reference_label='No' AND j.overall='Yes'
GROUP BY q.source_column,q.question_group
ORDER BY q.source_column;

-- BEGIN FILE: 07_item_diagnostics.sql
-- One row per item; no long-answer/topic join multiplies the unit.
SELECT r.item_id, r.reference, e.degree_of_harm AS source_severity,
       COUNT(*) AS n, COUNT(DISTINCT r.rater_id) AS raters,
       SUM(r.judgment='Yes') AS yes, SUM(r.judgment='No') AS no,
       SUM(r.judgment='Unsure') AS unsure,
       SUM(r.reference='Yes' AND r.judgment='No') AS fn,
       SUM(r.reference='No' AND r.judgment='Yes') AS fp
FROM ratings AS r
JOIN fact_expert_reference AS e ON e.item_id=r.item_id
GROUP BY r.item_id,r.reference,e.degree_of_harm
ORDER BY r.item_id;

-- BEGIN FILE: 08_item_panel_counts.sql
-- Replace PANEL_VIEW only from the internal allow-list of validated views.
-- One row per item; retain the entire panel-specific item cluster.
SELECT item_id,reference,COUNT(*) AS n,COUNT(DISTINCT rater_id) AS raters,
 SUM(reference='Yes' AND judgment='Yes') AS tp,
 SUM(reference='Yes' AND judgment='No') AS fn,
 SUM(reference='Yes' AND judgment='Unsure') AS uy,
 SUM(reference='No' AND judgment='Yes') AS fp,
 SUM(reference='No' AND judgment='No') AS tn,
 SUM(reference='No' AND judgment='Unsure') AS un
FROM PANEL_VIEW
GROUP BY item_id,reference ORDER BY item_id;

-- BEGIN FILE: 09_evaluate_selected.sql
-- PANEL_VIEW and ITEM_PARAMS are internal allow-listed/generated SQL fragments.
-- Item IDs are bound parameters; IN preserves one row per judgment.
SELECT COUNT(*) AS selected_records,COUNT(DISTINCT item_id) AS selected_items,
 COALESCE(SUM(reference='Yes' AND judgment='No'),0) AS selected_fn,
 COALESCE(SUM(reference='No' AND judgment='Yes'),0) AS selected_fp,
 COALESCE(SUM(judgment='Unsure'),0) AS selected_unsure
FROM PANEL_VIEW WHERE item_id IN (ITEM_PARAMS);

-- BEGIN FILE: 09_selection_signals.sql
-- No final expert safety label, FN/FP count or expert reason is exposed here.
-- Source severity is supplied metadata with an unresolved version discrepancy.
SELECT j.item_id,e.degree_of_harm AS source_severity,COUNT(*) AS n,
 SUM(j.overall='Yes') AS yes,SUM(j.overall='No') AS no,
 SUM(j.overall='Unsure') AS unsure
FROM fact_judgment j JOIN fact_expert_reference e USING(item_id)
GROUP BY j.item_id,e.degree_of_harm ORDER BY j.item_id;

