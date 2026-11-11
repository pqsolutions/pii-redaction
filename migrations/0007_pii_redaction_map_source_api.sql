-- Adds a third pii_redaction_map.source value for rows created by the bulk
-- redaction-request API (caller-supplied values, no automated detection),
-- distinguishing them from pipeline-detected and reference-table-manual rows.
-- See wiki/feature-updates/phase-1/plan-api-redaction-implementation.md,
-- Section 6.

ALTER TABLE pii_redaction_map
    MODIFY COLUMN source ENUM('pipeline', 'manual', 'api') NOT NULL DEFAULT 'pipeline';
