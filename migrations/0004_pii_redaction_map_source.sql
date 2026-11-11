-- Distinguishes pipeline-generated redaction rows (immutable, produced during
-- the normal Run Redaction flow) from manually-added ones (editable/deletable
-- via the reference-table "Add missing value" flow). See
-- wiki/feature-updates/phase-1/plan-table-implementation.md, Open Question #6.

ALTER TABLE pii_redaction_map
    ADD COLUMN source ENUM('pipeline', 'manual') NOT NULL DEFAULT 'pipeline' AFTER redacted_text;
