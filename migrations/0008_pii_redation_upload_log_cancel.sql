-- Batch cancel support (ported from PII_Code_19 Aug's queue_manager.py, per
-- wiki/plan-compare-wiki.md Q2 — reuses this existing per-file upload log
-- rather than standing up a second, parallel job-state store).
--
-- process_id lets every PII_redation_upload_log row belonging to one batch
-- (one /upload call, one manifest) be identified and cancelled together;
-- cancelled is checked by dual_redaction_pipeline.py's main() processing
-- loop before starting each file.

ALTER TABLE PII_redation_upload_log
    ADD COLUMN process_id VARCHAR(50) NULL AFTER uid,
    ADD COLUMN cancelled TINYINT(1) NOT NULL DEFAULT 0 AFTER status;

CREATE INDEX idx_pii_redation_upload_log_process_id ON PII_redation_upload_log (process_id);
