-- Manual review time per batch (wiki/feature-updates/plan-timer-implementation.md).
--
-- Measured in the browser from the first click on the preview's "Select" tool
-- to the latest Download click, excluding time the tab was hidden and time
-- with no mouse/keyboard input for REVIEW_IDLE_SECONDS (default 15). The
-- excluded time is still logged (idle_seconds / tab_hidden_seconds) so the
-- numbers can be understood later. Upserted on every Download click — one row
-- per batch per user.
--
-- system_seconds is the pipeline's own time for the batch, copied from
-- PII_redation_upload_log (MIN(uploaded_at) -> MAX(completed_on)) at save
-- time, so review vs system time can be compared from this one table.
--
-- DATETIME with no defaults, not TIMESTAMP DEFAULT CURRENT_TIMESTAMP: the
-- production DB is MySQL 5.5, which allows only one TIMESTAMP column with a
-- CURRENT_TIMESTAMP default/on-update per table. db.save_review_time() sets
-- created_at/updated_at explicitly.

CREATE TABLE IF NOT EXISTS pii_process_review_time (
    id                     INT AUTO_INCREMENT PRIMARY KEY,
    process_id             VARCHAR(50) NOT NULL,
    uid                    INT NOT NULL,
    active_seconds         INT NOT NULL DEFAULT 0,
    idle_seconds           INT NOT NULL DEFAULT 0,
    tab_hidden_seconds     INT NOT NULL DEFAULT 0,
    idle_pause_count       INT NOT NULL DEFAULT 0,
    tab_pause_count        INT NOT NULL DEFAULT 0,
    idle_threshold_seconds INT NOT NULL,
    system_seconds         INT NULL,
    download_count         INT NOT NULL DEFAULT 0,
    started_at             DATETIME NULL,
    last_download_at       DATETIME NULL,
    created_at             DATETIME NOT NULL,
    updated_at             DATETIME NOT NULL,
    UNIQUE KEY uq_review_time_process_uid (process_id, uid)
);
