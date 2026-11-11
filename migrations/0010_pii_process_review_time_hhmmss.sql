-- Store review/system durations as TIME (shown as HH:MM:SS, e.g. 00:01:11)
-- instead of integer seconds, so they read naturally in the table
-- (wiki/feature-updates/plan-timer-implementation.md, Section 8).
--
-- New columns are added, existing rows converted with SEC_TO_TIME(), then the
-- old *_seconds columns dropped. TIME, not VARCHAR, so values still compare
-- and sum correctly (TIME_TO_SEC() gives seconds back). idle_threshold_seconds
-- stays an INT — it's a setting, not a measured duration.

ALTER TABLE pii_process_review_time
    ADD COLUMN active_time     TIME NOT NULL DEFAULT '00:00:00' AFTER uid,
    ADD COLUMN idle_time       TIME NOT NULL DEFAULT '00:00:00' AFTER active_time,
    ADD COLUMN tab_hidden_time TIME NOT NULL DEFAULT '00:00:00' AFTER idle_time,
    ADD COLUMN system_time     TIME NULL AFTER idle_threshold_seconds;

UPDATE pii_process_review_time SET
    active_time     = SEC_TO_TIME(active_seconds),
    idle_time       = SEC_TO_TIME(idle_seconds),
    tab_hidden_time = SEC_TO_TIME(tab_hidden_seconds),
    system_time     = SEC_TO_TIME(system_seconds);

ALTER TABLE pii_process_review_time
    DROP COLUMN active_seconds,
    DROP COLUMN idle_seconds,
    DROP COLUMN tab_hidden_seconds,
    DROP COLUMN system_seconds;
