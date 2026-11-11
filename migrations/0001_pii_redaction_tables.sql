-- PII redaction DB-backed rehydration store.
-- Adds new tables only — does not modify the existing `project` / `activity`
-- tables (which this app reads from but does not own).
--
-- client_id  -> project.Pid   (project.access = 1 for the client dropdown)
-- job_id     -> activity.Aid  (searched live via activity.jobdescription)

-- Caches the resolved 3-letter code per client so it stays stable across
-- uploads. project.ShortName is used when present; otherwise a code is
-- generated from ClientName and cached here (see db.get_or_create_client_code).
CREATE TABLE IF NOT EXISTS pii_client_codes (
    pid        INT NOT NULL PRIMARY KEY,
    code       VARCHAR(3) NOT NULL UNIQUE,
    source     ENUM('shortname','generated') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Atomic per-prefix counters for FIL-/PRC- id generation.
CREATE TABLE IF NOT EXISTS pii_id_sequences (
    prefix     VARCHAR(8) NOT NULL PRIMARY KEY,
    next_value INT NOT NULL
);

INSERT IGNORE INTO pii_id_sequences (prefix, next_value) VALUES ('FIL', 100001);
INSERT IGNORE INTO pii_id_sequences (prefix, next_value) VALUES ('PRC', 1);

-- One row per uploaded file — lets /rehydrate find the redacted output from a file_id.
CREATE TABLE IF NOT EXISTS pii_files (
    file_id       VARCHAR(20) NOT NULL PRIMARY KEY,
    process_id    VARCHAR(20) NOT NULL,
    client_id     INT NOT NULL,
    job_id        INT NOT NULL,
    filename      VARCHAR(255) NOT NULL,
    redacted_path VARCHAR(500),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX (process_id)
);

-- One row per unique PII value redacted in a file (replaces the per-file
-- secrets/pii_secret_log_*.xlsx as the source of truth for rehydration).
CREATE TABLE IF NOT EXISTS pii_redaction_map (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    client_id     INT NOT NULL,
    job_id        INT NOT NULL,
    process_id    VARCHAR(20) NOT NULL,
    file_id       VARCHAR(20) NOT NULL,
    source_text   TEXT NOT NULL,
    redacted_text TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX (file_id),
    INDEX (process_id),
    INDEX (client_id),
    INDEX (job_id)
);
