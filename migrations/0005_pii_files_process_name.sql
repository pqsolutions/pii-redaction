-- User-entered label for a batch of files (the "Process Name" textbox above
-- the Client dropdown), stored alongside the auto-generated process_id so
-- runs can be tracked/searched by a human-readable name.

ALTER TABLE pii_files
    ADD COLUMN process_name VARCHAR(255) NULL AFTER process_id;
