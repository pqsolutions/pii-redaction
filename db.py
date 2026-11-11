"""
MySQL logging for uploads, plus the DB-backed PII rehydration store.

Expects PII_redation_upload_log to already exist:

    CREATE TABLE PII_redation_upload_log (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        uid           INT NOT NULL,
        file_count    INT,
        status        ENUM('pending','success','fail') NOT NULL DEFAULT 'pending',
        error_message TEXT NULL,
        input_file    VARCHAR(255) NULL,
        output_file   VARCHAR(255) NULL,
        uploaded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_on  TIMESTAMP NULL
    );

If the table already exists from before the status/error_message columns
were added, run:

    ALTER TABLE PII_redation_upload_log
      ADD COLUMN status ENUM('pending','success','fail') NOT NULL DEFAULT 'pending' AFTER file_count,
      ADD COLUMN error_message TEXT NULL AFTER status;

If the table already exists from before the input_file/output_file/completed_on
columns were added, run:

    ALTER TABLE PII_redation_upload_log
      ADD COLUMN input_file  VARCHAR(255) NULL AFTER error_message,
      ADD COLUMN output_file VARCHAR(255) NULL AFTER input_file,
      ADD COLUMN completed_on TIMESTAMP NULL AFTER uploaded_at;

One row is now logged per uploaded file (not per batch) — file_count stays
at 1 per row for backward compatibility with any existing reporting.

Also expects the process_id and cancelled columns from
migrations/0008_pii_redation_upload_log_cancel.sql — process_id groups every
row from one batch/manifest together so cancel_batch()/is_batch_cancelled()
can act on the whole batch at once.

Also expects the pii_client_codes / pii_id_sequences / pii_files /
pii_redaction_map tables from migrations/0001_pii_redaction_tables.sql, the
pii_files.output_format column from migrations/0002_pii_output_format.sql, the
pii_files.original_encrypted_path column from
migrations/0003_pii_original_encrypted_path.sql, the
pii_redaction_map.source column from migrations/0004_pii_redaction_map_source.sql,
the pii_files.process_name column from
migrations/0005_pii_files_process_name.sql, the pii_categories table from
migrations/0006_pii_categories.sql, and the pii_process_review_time table from
migrations/0009_pii_process_review_time.sql.
Clients and jobs are read from the existing `project` (Pid, ClientName,
ShortName, access) and `activity` (Aid, Pid, jobdescription) tables — this
module does not own or migrate those.

Per-user client scoping reads the existing `userwiseclient` table (uid,
PID — PID is a comma-separated string of project.Pid values allowed for
that uid). This module does not own or migrate that table either.

Connection details come from the environment (see .env.example).
"""

import os
import re

import pymysql
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_NAME = os.environ.get("DB_NAME")


def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        autocommit=True,
    )


def log_upload(uid: int, input_file: str, process_id: str = None) -> int:
    """Insert a pending upload record for one file and return its id for later status updates.
    process_id groups every file from the same batch/manifest together, so
    cancel_batch()/is_batch_cancelled() can act on the whole batch at once."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO PII_redation_upload_log (uid, process_id, file_count, input_file, status) "
                "VALUES (%s, %s, 1, %s, 'pending')",
                (uid, process_id, input_file),
            )
            return cur.lastrowid
    finally:
        conn.close()


def cancel_batch(process_id: str) -> int:
    """Mark every not-yet-completed upload-log row for this process_id as
    cancelled. dual_redaction_pipeline.py's main() checks is_batch_cancelled()
    before starting each file and stops the batch early once set. Returns the
    number of rows marked."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE PII_redation_upload_log SET cancelled=1 "
                "WHERE process_id=%s AND status='pending'",
                (process_id,),
            )
            return cur.rowcount
    finally:
        conn.close()


def is_batch_cancelled(process_id: str) -> bool:
    """True if any row for this process_id has been marked cancelled."""
    if not process_id:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM PII_redation_upload_log WHERE process_id=%s AND cancelled=1 LIMIT 1",
                (process_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def update_upload_status(log_id: int, status: str, error_message: str = None,
                          output_file: str = None) -> None:
    """Mark a previously logged upload as 'success' or 'fail' and stamp completed_on."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE PII_redation_upload_log "
                "SET status=%s, error_message=%s, output_file=%s, completed_on=CURRENT_TIMESTAMP "
                "WHERE id=%s",
                (status, error_message, output_file, log_id),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review timer (wiki/feature-updates/plan-timer-implementation.md)
# ---------------------------------------------------------------------------

# Durations are stored as TIME (HH:MM:SS, migration 0010) but returned in
# seconds under the *_seconds keys the frontend timer uses.
REVIEW_TIME_SELECT = [
    ("active_seconds", "TIME_TO_SEC(active_time)"),
    ("idle_seconds", "TIME_TO_SEC(idle_time)"),
    ("tab_hidden_seconds", "TIME_TO_SEC(tab_hidden_time)"),
    ("idle_pause_count", "idle_pause_count"),
    ("tab_pause_count", "tab_pause_count"),
    ("idle_threshold_seconds", "idle_threshold_seconds"),
    ("system_seconds", "TIME_TO_SEC(system_time)"),
    ("download_count", "download_count"),
    ("started_at", "started_at"),
    ("last_download_at", "last_download_at"),
]


def get_system_seconds(process_id: str):
    """Pipeline (system) time for a batch: first upload-log row inserted ->
    last row completed. None when any file is still pending.

    Batches sent by the DMS (Process button) have no upload-log rows -- the
    DMS calls /upload server-to-server without a uid -- so for those the DMS's
    own job row is used instead: pii_dms_redaction_jobs.created_at (Process
    clicked) -> updated_at (redacted files saved back), once status is 'done'.
    None when neither table has a finished record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), SUM(completed_on IS NULL), "
                "TIMESTAMPDIFF(SECOND, MIN(uploaded_at), MAX(completed_on)) "
                "FROM PII_redation_upload_log WHERE process_id=%s",
                (process_id,),
            )
            total, pending, seconds = cur.fetchone()
            if total:
                if pending or seconds is None:
                    return None
                return max(0, int(seconds))

            cur.execute(
                "SELECT TIMESTAMPDIFF(SECOND, created_at, updated_at) FROM pii_dms_redaction_jobs "
                "WHERE redactor_job_id=%s AND status='done' ORDER BY updated_at DESC LIMIT 1",
                (process_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            return max(0, int(row[0]))
    finally:
        conn.close()


def get_review_time(process_id: str, uid: int):
    """Saved review-time row for this batch and user, or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(expr for _, expr in REVIEW_TIME_SELECT)} FROM pii_process_review_time "
                "WHERE process_id=%s AND uid=%s",
                (process_id, uid),
            )
            row = cur.fetchone()
            if row is None:
                return None
            result = dict(zip((key for key, _ in REVIEW_TIME_SELECT), row))
            for key in ("active_seconds", "idle_seconds", "tab_hidden_seconds", "system_seconds"):
                if result[key] is not None:
                    result[key] = int(result[key])
            for key in ("started_at", "last_download_at"):
                if result[key] is not None:
                    result[key] = result[key].isoformat()
            return result
    finally:
        conn.close()


def save_review_time(process_id: str, uid: int, active_seconds: int, idle_seconds: int,
                     tab_hidden_seconds: int, idle_pauses: int, tab_pauses: int,
                     wall_seconds: int, idle_threshold_seconds: int, system_seconds) -> None:
    """Save the review time on a Download click: update the batch's row if
    it exists, otherwise insert it. Totals only ever grow (GREATEST) so a late
    or out-of-order request can't lower them; started_at is back-dated from
    the browser's elapsed wall time on the first save only, so the client's
    clock is never trusted.

    UPDATE-then-INSERT rather than INSERT ... ON DUPLICATE KEY UPDATE: with
    innodb_autoinc_lock_mode=1 the upsert burns an AUTO_INCREMENT id on every
    repeat download, leaving gaps in id.

    created_at/updated_at are set explicitly: MySQL 5.5 allows only one
    TIMESTAMP column with a CURRENT_TIMESTAMP default, so the table uses
    DATETIME columns with no defaults (migration 0009).

    Durations arrive in seconds and are stored as TIME via SEC_TO_TIME()
    (migration 0010); GREATEST() compares them as seconds."""
    update_sql = (
        "UPDATE pii_process_review_time SET "
        " active_time=SEC_TO_TIME(GREATEST(TIME_TO_SEC(active_time), %s)), "
        " idle_time=SEC_TO_TIME(GREATEST(TIME_TO_SEC(idle_time), %s)), "
        " tab_hidden_time=SEC_TO_TIME(GREATEST(TIME_TO_SEC(tab_hidden_time), %s)), "
        " idle_pause_count=GREATEST(idle_pause_count, %s), "
        " tab_pause_count=GREATEST(tab_pause_count, %s), "
        " idle_threshold_seconds=%s, "
        " system_time=COALESCE(SEC_TO_TIME(%s), system_time), "
        " download_count=download_count + 1, "
        " last_download_at=CURRENT_TIMESTAMP, "
        " updated_at=CURRENT_TIMESTAMP "
        "WHERE process_id=%s AND uid=%s"
    )
    update_params = (active_seconds, idle_seconds, tab_hidden_seconds, idle_pauses, tab_pauses,
                     idle_threshold_seconds, system_seconds, process_id, uid)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # download_count always changes, so rowcount is 1 whenever the row exists.
            if cur.execute(update_sql, update_params):
                return
            try:
                cur.execute(
                    "INSERT INTO pii_process_review_time "
                    "(process_id, uid, active_time, idle_time, tab_hidden_time, idle_pause_count, "
                    " tab_pause_count, idle_threshold_seconds, system_time, download_count, "
                    " started_at, last_download_at, created_at, updated_at) "
                    "VALUES (%s, %s, SEC_TO_TIME(%s), SEC_TO_TIME(%s), SEC_TO_TIME(%s), %s, %s, %s, "
                    " SEC_TO_TIME(%s), 1, "
                    " CURRENT_TIMESTAMP - INTERVAL %s SECOND, CURRENT_TIMESTAMP, "
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (process_id, uid, active_seconds, idle_seconds, tab_hidden_seconds, idle_pauses,
                     tab_pauses, idle_threshold_seconds, system_seconds, wall_seconds),
                )
            except pymysql.err.IntegrityError:
                # A concurrent download for the same batch inserted the row first.
                cur.execute(update_sql, update_params)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Client / Job lookups (read-only against the existing project/activity tables)
# ---------------------------------------------------------------------------

def get_clients(allowed_pids: set = None) -> list:
    """Active clients for the upload dropdown, optionally scoped to a set
    of allowed project.Pid values (see get_allowed_pids). Pass None for the
    unscoped/all-clients list; pass an empty set to get back no clients."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if allowed_pids is not None:
                if not allowed_pids:
                    return []
                placeholders = ",".join(["%s"] * len(allowed_pids))
                cur.execute(
                    f"SELECT Pid, ClientName, ShortName FROM project "
                    f"WHERE access=1 AND Pid IN ({placeholders}) ORDER BY ClientName",
                    tuple(allowed_pids),
                )
            else:
                cur.execute(
                    "SELECT Pid, ClientName, ShortName FROM project WHERE access=1 ORDER BY ClientName"
                )
            return [
                {"pid": row[0], "name": row[1], "short_name": row[2]}
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def get_allowed_pids(uid: int) -> set:
    """Client project.Pid values this uid is permitted to see, from
    userwiseclient.PID (a comma-separated list). Returns an empty set if
    the uid has no row — callers should treat that as "no clients
    allowed", not "all clients". A uid can have more than one row, so the
    PIDs of every row are combined (reading only the first row made
    permitted clients randomly come back as 403)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT PID FROM userwiseclient WHERE uid=%s", (uid,))
            pids = set()
            for (pid_list,) in cur.fetchall():
                pids.update(int(p) for p in str(pid_list or "").split(",") if p.strip().isdigit())
            return pids
    finally:
        conn.close()


def get_client(pid: int):
    """Look up a single active client by project.Pid. Returns a dict or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT Pid, ClientName, ShortName FROM project WHERE Pid=%s AND access=1",
                (pid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"pid": row[0], "name": row[1], "short_name": row[2]}
    finally:
        conn.close()


def get_job(aid: int):
    """Look up a single job (activity row) by Aid. Returns a dict or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT Aid, Pid, jobdescription FROM activity WHERE Aid=%s", (aid,))
            row = cur.fetchone()
            if not row:
                return None
            return {"aid": row[0], "pid": row[1], "description": row[2]}
    finally:
        conn.close()


def search_jobs(client_id: int, q: str = "", limit: int = 500) -> list:
    """Jobs (activity.jobdescription) for one client (project.Pid), for the job dropdown."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            like = f"%{q.strip()}%" if q and q.strip() else "%"
            cur.execute(
                "SELECT Aid, jobdescription, Date FROM activity "
                "WHERE Pid=%s AND jobdescription LIKE %s "
                "ORDER BY jobdescription ASC LIMIT %s",
                (client_id, like, limit),
            )
            return [
                {
                    "aid": row[0],
                    "description": row[1],
                    "date": row[2].isoformat() if row[2] else None,
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Client code resolution (pii_client_codes)
# ---------------------------------------------------------------------------

_CODE_STRIP_RE = re.compile(r"[^A-Za-z]")


def _derive_code(text: str) -> str:
    letters = _CODE_STRIP_RE.sub("", text or "").upper()
    return (letters + "XXX")[:3]


def get_or_create_client_code(pid: int, client_name: str, short_name: str = None) -> str:
    """Return the stable 3-letter code for a client (project.Pid).

    Uses project.ShortName when it's set and not already taken by a
    different client; otherwise derives a code from ClientName. Either way
    the result is cached in pii_client_codes so the same client always gets
    the same code, even though ~85% of clients have no ShortName today and
    a code would otherwise be regenerated (and could collide differently)
    on every upload.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM pii_client_codes WHERE pid=%s", (pid,))
            row = cur.fetchone()
            if row:
                return row[0]

            attempts = []
            if short_name and short_name.strip():
                attempts.append((_derive_code(short_name), "shortname"))
            attempts.append((_derive_code(client_name), "generated"))

            for base, source in attempts:
                for suffix in [""] + [str(n) for n in range(2, 100)]:
                    code = (base[: 3 - len(suffix)] + suffix) if suffix else base
                    try:
                        cur.execute(
                            "INSERT INTO pii_client_codes (pid, code, source) VALUES (%s, %s, %s)",
                            (pid, code, source),
                        )
                        return code
                    except pymysql.err.IntegrityError:
                        cur.execute("SELECT code FROM pii_client_codes WHERE pid=%s", (pid,))
                        row = cur.fetchone()
                        if row:
                            return row[0]  # a concurrent request already created it
                        continue  # code taken by a different pid — try next suffix
            raise RuntimeError(f"Could not allocate a client code for pid={pid}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FIL-/PRC- id generation (pii_id_sequences)
# ---------------------------------------------------------------------------

def next_id(prefix: str, client_code: str) -> str:
    """Atomically allocate the next number for `prefix` ('FIL' or 'PRC') and
    format it as e.g. FIL-ABC-100001."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pii_id_sequences SET next_value = LAST_INSERT_ID(next_value + 1) WHERE prefix=%s",
                (prefix,),
            )
            cur.execute("SELECT LAST_INSERT_ID()")
            n = cur.fetchone()[0]
            return f"{prefix}-{client_code}-{n:06d}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pii_files / pii_redaction_map
# ---------------------------------------------------------------------------

def record_file(file_id: str, process_id: str, client_id: int, job_id: int,
                 filename: str, redacted_path: str = None, output_format: str = "excel",
                 process_name: str = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pii_files (file_id, process_id, process_name, client_id, job_id, filename, redacted_path, output_format) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (file_id, process_id, process_name, client_id, job_id, filename, redacted_path, output_format),
            )
    finally:
        conn.close()


def update_file_output(file_id: str, redacted_path: str, output_format: str) -> None:
    """Patch a pii_files row with the *actual* output path/format once the pipeline
    has finished — needed because "As is" mode can fall back to Excel output at
    runtime (see plan-as-it-is-implementation.md, Open Question #5), so the true
    extension/format isn't known until after the pipeline completes."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pii_files SET redacted_path=%s, output_format=%s WHERE file_id=%s",
                (redacted_path, output_format, file_id),
            )
    finally:
        conn.close()


def get_file(file_id: str):
    """Look up a pii_files row by file_id. Returns a dict or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, process_id, process_name, client_id, job_id, filename, redacted_path, output_format, "
                "original_encrypted_path FROM pii_files WHERE file_id=%s",
                (file_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "file_id": row[0], "process_id": row[1], "process_name": row[2], "client_id": row[3],
                "job_id": row[4], "filename": row[5], "redacted_path": row[6],
                "output_format": row[7], "original_encrypted_path": row[8],
            }
    finally:
        conn.close()


def set_original_encrypted_path(file_id: str, original_encrypted_path: str) -> None:
    """Record where the encrypted original of an image was stored, for
    "As is" rehydration (see migrations/0003_pii_original_encrypted_path.sql)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pii_files SET original_encrypted_path=%s WHERE file_id=%s",
                (original_encrypted_path, file_id),
            )
    finally:
        conn.close()


def insert_redaction_rows(client_id: int, job_id: int, process_id: str, file_id: str,
                           pairs: list) -> None:
    """Bulk insert (source_text, redacted_text) pairs into pii_redaction_map."""
    if not pairs:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO pii_redaction_map "
                "(client_id, job_id, process_id, file_id, source_text, redacted_text) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [(client_id, job_id, process_id, file_id, src, red) for src, red in pairs],
            )
    finally:
        conn.close()


def get_redaction_map(file_id: str = None, process_id: str = None) -> tuple:
    """Conflict-aware {redacted_text: source_text} map for rehydration,
    scoped to one file_id's whole process (or a process_id directly) — reuses
    get_redaction_map_for_process()'s per-process merge (Section 3.0.2),
    collapsed to the shape unmask_as_is()/_apply_token_map() expect. Returns
    (mapping, conflicts): `mapping` excludes any token with 2+ distinct
    source_text values across the process — only possible for rows recorded
    before token allocation became job-scoped (see get_secret_map_for_job());
    `conflicts` is the list of those left out, each
    `{"token": ..., "candidates": {source_text: [file_id, ...]}}`, left as
    literal `<*TOKEN*>` text in the output rather than guessed."""
    if not file_id and not process_id:
        raise ValueError("file_id or process_id required")
    if file_id and not process_id:
        file_row = get_file(file_id)
        if not file_row:
            raise ValueError(f"Unknown file_id: {file_id}")
        process_id = file_row["process_id"]

    token_map = get_redaction_map_for_process(process_id)
    mapping = {}
    conflicts = []
    for token, candidates in token_map.items():
        if len(candidates) == 1:
            mapping[token] = next(iter(candidates))
        else:
            conflicts.append({"token": token, "candidates": candidates})
    return mapping, conflicts


def get_redaction_map_for_process(process_id: str) -> dict:
    """Conflict-preserving token map for a batch: {redacted_text: {source_text:
    [file_id, ...]}}. Unlike get_redaction_map(process_id=...), never silently
    merges same-named tokens from different files — a token that maps to 2+
    distinct source_text values across the batch stays visible as a conflict
    for the caller to handle (see
    wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md, Section 3.0.2)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT redacted_text, source_text, file_id FROM pii_redaction_map WHERE process_id=%s",
                (process_id,),
            )
            result = {}
            for redacted_text, source_text, file_id in cur.fetchall():
                by_value = result.setdefault(redacted_text, {})
                by_value.setdefault(source_text, []).append(file_id)
            return result
    finally:
        conn.close()


_TOKEN_RE = re.compile(r"<\*([A-Z0-9_]+?)_(\d+)\*>")


def get_secret_map_for_job(client_id: int, job_id: int) -> tuple:
    """Seed a job-scoped secret_map/candidate_counters from every prior
    pii_redaction_map row for this client+job (across all process_ids), so
    dual_redaction_pipeline.py's token allocation stays stable across
    separate upload runs within the same job — the same real value always
    mints the same token, and two distinct values never coincidentally land
    on the same token number just because each run's counters restarted at 1
    (see wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md,
    Section 3.0.1). Shape matches get_candidate_token()'s in-memory
    structures exactly: secret_map[value_lower] = {"candidate": token,
    "original_value": value}; candidate_counters[category] = highest N
    already used for that category, so future minting continues from there.

    Ordered oldest-first so that if a value was ever recorded under two
    different tokens (e.g. from before this job-scoped sharing existed), the
    earliest one wins and stays the canonical token going forward — counters
    still account for every row seen, not just the first per value."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_text, redacted_text FROM pii_redaction_map "
                "WHERE client_id=%s AND job_id=%s ORDER BY created_at ASC",
                (client_id, job_id),
            )
            secret_map = {}
            candidate_counters = {}
            for source_text, redacted_text in cur.fetchall():
                key = (source_text or "").lower()
                if key and key not in secret_map:
                    secret_map[key] = {"candidate": redacted_text, "original_value": source_text}
                m = _TOKEN_RE.fullmatch((redacted_text or "").strip())
                if m:
                    cat, n = m.group(1), int(m.group(2))
                    candidate_counters[cat] = max(candidate_counters.get(cat, 0), n)
            return secret_map, candidate_counters
    finally:
        conn.close()


def list_processes(client_id: int, job_id: int, limit: int = 50) -> list:
    """Distinct processes (upload batches) for one client+job, newest first —
    drives the rehydrate page's Process Name picker
    (wiki/feature-updates/phase-1/plan-rehyrdration-implementation.md,
    Section 3.4.1). Excludes legacy batches uploaded before
    migrations/0005_pii_files_process_name.sql (process_name IS NULL)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT process_id, process_name, MIN(created_at) AS created_at, COUNT(*) AS file_count "
                "FROM pii_files WHERE process_name IS NOT NULL AND client_id=%s AND job_id=%s "
                "GROUP BY process_id, process_name ORDER BY created_at DESC LIMIT %s",
                (client_id, job_id, limit),
            )
            return [
                {
                    "process_id": row[0], "process_name": row[1],
                    "created_at": row[2].isoformat() if row[2] else None,
                    "file_count": row[3],
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reference table + manual "Add missing value" flow
# (wiki/feature-updates/phase-1/plan-table-implementation.md)
# ---------------------------------------------------------------------------

def get_files_for_process(process_id: str) -> list:
    """All pii_files rows for one batch (process_id) — drives the per-file
    preview/table tabs on the results screen."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, process_id, process_name, client_id, job_id, filename, redacted_path, "
                "output_format, original_encrypted_path FROM pii_files WHERE process_id=%s "
                "ORDER BY created_at",
                (process_id,),
            )
            cols = ["file_id", "process_id", "process_name", "client_id", "job_id", "filename",
                    "redacted_path", "output_format", "original_encrypted_path"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def get_redaction_rows(file_id: str) -> list:
    """All pii_redaction_map rows for one file, including id/source — unlike
    get_redaction_map(), which only returns a flattened (mapping, conflicts)
    view for rehydration, this keeps row identity so the reference table can
    edit/delete individual manually-added rows."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_text, redacted_text, source FROM pii_redaction_map "
                "WHERE file_id=%s ORDER BY id",
                (file_id,),
            )
            return [
                {"id": row[0], "source_text": row[1], "redacted_text": row[2], "source": row[3]}
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def get_redaction_row(row_id: int):
    """Look up a single pii_redaction_map row by id. Returns a dict or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, client_id, job_id, process_id, file_id, source_text, "
                "redacted_text, source FROM pii_redaction_map WHERE id=%s",
                (row_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = ["id", "client_id", "job_id", "process_id", "file_id",
                    "source_text", "redacted_text", "source"]
            return dict(zip(cols, row))
    finally:
        conn.close()


def insert_redaction_row(client_id: int, job_id: int, process_id: str, file_id: str,
                          source_text: str, redacted_text: str, source: str = "manual") -> int:
    """Single-row insert into pii_redaction_map (the manual "Add missing value"
    flow) — unlike insert_redaction_rows(), which is a bulk insert called once
    per pipeline run. Returns the new row's id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pii_redaction_map "
                "(client_id, job_id, process_id, file_id, source_text, redacted_text, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (client_id, job_id, process_id, file_id, source_text, redacted_text, source),
            )
            return cur.lastrowid
    finally:
        conn.close()


def delete_redaction_row(row_id: int) -> None:
    """Remove a redaction row (manual or auto-detected) — the reference table
    lets a reviewer delete either, restoring the original text in the output
    file first (see _restore_row in app.py)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pii_redaction_map WHERE id=%s", (row_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Categories — shared, user-managed list backing the reference table's
# "Category" dropdown (previously free text). See migrations/0006_pii_categories.sql.
# ---------------------------------------------------------------------------

def get_categories() -> list:
    """All category names for the Category dropdown, alphabetical."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM pii_categories ORDER BY name")
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def add_category(name: str) -> str:
    """Add a new category to the shared dropdown list. Normalizes the same
    way next_token_for_category() (redaction_formats.py) normalizes a
    category into a token prefix, so what the user picks here always matches
    what ends up embedded in the redacted_text token. Idempotent (UNIQUE name,
    INSERT IGNORE) — adding an existing category is a no-op. Returns the
    normalized name that was stored."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_").upper()
    if not normalized:
        raise ValueError("Category name must contain at least one letter or number")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO pii_categories (name) VALUES (%s)", (normalized,))
    finally:
        conn.close()
    return normalized


# ---------------------------------------------------------------------------
# Dashboard analytics (wiki/feature-updates/phase-1/plan-dashboard-implementation.md)
# Org-wide aggregate queries for the admin-only /dashboard page — Section 5
# Q1's answer: access is gated to a fixed admin uid allowlist in app.py
# (DASHBOARD_ADMIN_UIDS), not per-user client scoping, so these intentionally
# read across every client/job with no allowed_pids filtering.
# ---------------------------------------------------------------------------

def _dashboard_date_clause(column: str, start_date: str = None, end_date: str = None):
    """Build an optional 'AND column >= .. AND column <= ..' SQL fragment +
    params for the dashboard's date-range filter (Section 5 Q4). `end_date`
    is treated as inclusive of the whole day. Returns ("", []) when both
    are None (all-time, no filter)."""
    clauses = []
    params = []
    if start_date:
        clauses.append(f"{column} >= %s")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        clauses.append(f"{column} <= %s")
        params.append(f"{end_date} 23:59:59")
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def get_dashboard_summary(start_date: str = None, end_date: str = None) -> dict:
    """Top-line totals for the /dashboard stat cards."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            upload_clause, upload_params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), "
                "COUNT(DISTINCT uid) "
                f"FROM PII_redation_upload_log WHERE 1=1{upload_clause}",
                upload_params,
            )
            total_uploads, success_count, fail_count, pending_count, distinct_users = cur.fetchone()
            total_uploads = total_uploads or 0
            success_count = success_count or 0
            fail_count = fail_count or 0
            pending_count = pending_count or 0
            distinct_users = distinct_users or 0
            completed = success_count + fail_count
            success_rate = (success_count / completed * 100) if completed else None

            files_clause, files_params = _dashboard_date_clause("created_at", start_date, end_date)
            cur.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT client_id) FROM pii_files WHERE 1=1{files_clause}",
                files_params,
            )
            total_files, distinct_clients = cur.fetchone()

            map_clause, map_params = _dashboard_date_clause("created_at", start_date, end_date)
            cur.execute(
                f"SELECT COUNT(*) FROM pii_redaction_map WHERE 1=1{map_clause}",
                map_params,
            )
            total_redacted = cur.fetchone()[0] or 0

            return {
                "total_uploads": total_uploads,
                "success_count": success_count,
                "fail_count": fail_count,
                "pending_count": pending_count,
                "success_rate": success_rate,
                "total_files": total_files or 0,
                "distinct_clients": distinct_clients or 0,
                "distinct_users": distinct_users,
                "total_redacted": total_redacted,
            }
    finally:
        conn.close()


def get_upload_volume_over_time(start_date: str = None, end_date: str = None) -> list:
    """Uploads per calendar day within the range, oldest first — drives the
    dashboard's 'Upload Volume Over Time' bar chart."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT DATE(uploaded_at) AS d, COUNT(*) FROM PII_redation_upload_log "
                f"WHERE 1=1{clause} GROUP BY DATE(uploaded_at) ORDER BY d",
                params,
            )
            return [
                {"date": d.isoformat() if hasattr(d, "isoformat") else str(d), "count": c}
                for d, c in cur.fetchall()
            ]
    finally:
        conn.close()


def get_upload_status_breakdown(start_date: str = None, end_date: str = None) -> dict:
    """{'pending': N, 'success': N, 'fail': N} within the range — drives the
    dashboard's success/fail donut chart."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT status, COUNT(*) FROM PII_redation_upload_log "
                f"WHERE 1=1{clause} GROUP BY status",
                params,
            )
            counts = {"pending": 0, "success": 0, "fail": 0}
            for status, c in cur.fetchall():
                counts[status] = c
            return counts
    finally:
        conn.close()


def get_pii_category_breakdown(start_date: str = None, end_date: str = None, limit: int = 12) -> list:
    """Count of redacted values per PII category within the range, highest
    first. Category isn't a stored column — it's embedded in redacted_text
    as a token like '<*PERSON_1*>', so this reads every matching row's
    redacted_text and buckets it in Python with the same token-shape regex
    already used elsewhere (_TOKEN_RE above) rather than inventing a new
    one. Anything that doesn't match the token shape (e.g. legacy/manual
    rows) is bucketed as 'OTHER'."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("created_at", start_date, end_date)
            cur.execute(
                f"SELECT redacted_text FROM pii_redaction_map WHERE 1=1{clause}",
                params,
            )
            counts = {}
            for (redacted_text,) in cur.fetchall():
                m = _TOKEN_RE.fullmatch((redacted_text or "").strip())
                category = m.group(1) if m else "OTHER"
                counts[category] = counts.get(category, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            return [{"category": cat, "count": n} for cat, n in ranked[:limit]]
    finally:
        conn.close()


def get_top_clients(start_date: str = None, end_date: str = None, limit: int = 10) -> list:
    """Clients ranked by file volume within the range — drives the
    dashboard's 'Top Clients' table. Date filter applies to pii_files.created_at."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("f.created_at", start_date, end_date)
            cur.execute(
                "SELECT p.Pid, p.ClientName, COUNT(DISTINCT f.file_id) AS file_count, "
                "COUNT(m.id) AS redaction_count, MAX(f.created_at) AS last_activity "
                "FROM pii_files f "
                "JOIN project p ON p.Pid = f.client_id "
                "LEFT JOIN pii_redaction_map m ON m.file_id = f.file_id "
                f"WHERE 1=1{clause} "
                "GROUP BY p.Pid, p.ClientName "
                "ORDER BY file_count DESC, redaction_count DESC "
                "LIMIT %s",
                params + [limit],
            )
            return [
                {
                    "client_id": row[0],
                    "client_name": row[1],
                    "file_count": row[2],
                    "redaction_count": row[3],
                    "last_activity": row[4].strftime("%Y-%m-%d %H:%M") if row[4] else None,
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def get_vertical_breakdown(start_date: str = None, end_date: str = None) -> list:
    """Upload activity grouped by team/vertical, highest volume first —
    drives the dashboard's Vertical tab (wiki/feature-updates/phase-1/
    plan-dashboard-implementation.md, Section 10). Joins via
    user.TeamID = coemaster.Code (Section 10.4 Q1's answer, verified: 1,666
    of 1,863 user rows match). Shows every matched coemaster team by name —
    both the 6 client-facing IsBusinessVertical=1 rows (Financial Planning,
    SMSF, Business Services, Bookkeeping, Mortgage, NDIS) and internal
    teams (Technology Group, HR, Marketing, etc.) — rather than collapsing
    non-vertical teams into 'Unassigned'; that filter was removed after
    confirming it was hiding real teams (e.g. 4 Technology Group employees'
    activity, including admin/dev testing) rather than a genuine
    data-quality gap. 'Unassigned' now means only what it says: TeamID is
    NULL/empty, or doesn't match any coemaster row at all (Q4's answer —
    bucketed rather than dropped, so totals stay accurate). No PII-redacted
    column: only upload volume / success-fail, which are fully reliable
    (Q2's answer — redaction counts can't be attributed per-user for most
    historical rows, see Section 10.2)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("l.uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT COALESCE(c.Name, 'Unassigned') AS vertical_name, "
                "COUNT(*) AS upload_count, "
                "COUNT(DISTINCT l.uid) AS user_count, "
                "SUM(CASE WHEN l.status='success' THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN l.status='fail' THEN 1 ELSE 0 END) AS fail_count "
                "FROM PII_redation_upload_log l "
                "LEFT JOIN user u ON u.Uid = l.uid "
                "LEFT JOIN coemaster c ON c.Code = u.TeamID "
                f"WHERE 1=1{clause} "
                "GROUP BY vertical_name "
                "ORDER BY upload_count DESC",
                params,
            )
            return [
                {
                    "vertical_name": row[0],
                    "upload_count": row[1],
                    "user_count": row[2],
                    "success_count": int(row[3] or 0),
                    "fail_count": int(row[4] or 0),
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def _resolve_tlid_to_chid(cur) -> dict:
    """One CHID per TLID, keyed by TLID as a string. Some TLIDs have more
    than one teamleaderusermaster row (a stale/duplicate mapping, not a
    genuine one-to-many — e.g. TLID 48 has both a row pointing at a real
    Cluster Head and a leftover row with CHID NULL). Doing this join with
    plain SQL (FIND_IN_SET(u.TLID, tl.TLid), which — since
    teamleaderusermaster.TLID is a plain int, not a list — is really just
    u.TLID = tl.TLID) fans out one upload into two output rows whenever a
    TLID has duplicate teamleaderusermaster rows, double-counting that user
    into two different buckets (verified directly: TLID 48 has exactly this
    shape, and produced exactly this double-count before this fix). This
    resolves it once in Python instead: prefer a row with a non-null CHID
    over one without, and break ties with the highest TLMID (assumed most
    recently added = most current)."""
    cur.execute("SELECT TLMID, TLID, CHID FROM teamleaderusermaster WHERE TLID IS NOT NULL")
    best = {}
    for tlmid, tlid, chid in cur.fetchall():
        key = str(tlid)
        rank = (chid is not None, tlmid)
        if key not in best or rank > best[key][:2]:
            best[key] = (chid is not None, tlmid, chid)
    return {key: v[2] for key, v in best.items()}


def get_cluster_breakdown(start_date: str = None, end_date: str = None) -> list:
    """Upload activity grouped by Cluster Head, most active first — drives
    the dashboard's Cluster tab summary table. Adapted from the
    org-hierarchy query supplied in plan-dashboard-implementation.md
    Section 10 (its WHERE FIND_IN_SET(u.uid, 673) single-employee filter —
    equivalent to u.uid = 673 — is removed here to report org-wide), but
    resolved via _resolve_tlid_to_chid() rather than a raw SQL join — see
    that function's docstring for why. Shows both total team size (org
    headcount — not date-filtered, since org structure has no history in
    this schema) and active users in range side by side (Q3's answer —
    adoption rate, not just raw counts). Users who don't resolve to any
    Cluster Head are rolled into a trailing 'Unassigned' row (Q4's answer)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            tlid_to_chid = _resolve_tlid_to_chid(cur)

            cur.execute("SELECT Uid, TLID, Firstname, Lastname FROM user")
            all_users = cur.fetchall()
            uid_to_name = {uid: f"{fn} {ln}".strip() for uid, _, fn, ln in all_users}

            clause, params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT uid, COUNT(*), "
                "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) "
                f"FROM PII_redation_upload_log WHERE 1=1{clause} GROUP BY uid",
                params,
            )
            upload_stats = {
                uid: (cnt, int(s or 0), int(f or 0)) for uid, cnt, s, f in cur.fetchall()
            }

            buckets = {}
            for uid, tlid, _fn, _ln in all_users:
                chid = tlid_to_chid.get(str(tlid).strip()) if tlid and str(tlid).strip() else None
                name = uid_to_name.get(chid) if chid is not None else None
                key = name or "Unassigned"
                b = buckets.setdefault(
                    key,
                    {"team_size": 0, "upload_count": 0, "active_users": 0, "success": 0, "fail": 0},
                )
                b["team_size"] += 1
                stats = upload_stats.get(uid)
                if stats:
                    cnt, success, fail = stats
                    b["upload_count"] += cnt
                    b["active_users"] += 1
                    b["success"] += success
                    b["fail"] += fail

            rows = [
                {
                    "cluster_head_name": name,
                    "team_size": b["team_size"],
                    "upload_count": b["upload_count"],
                    "active_user_count": b["active_users"],
                    "success_count": b["success"],
                    "fail_count": b["fail"],
                }
                for name, b in buckets.items()
            ]
            rows.sort(key=lambda r: (-r["upload_count"], r["cluster_head_name"]))
            return rows
    finally:
        conn.close()


def get_cluster_user_breakdown(start_date: str = None, end_date: str = None) -> list:
    """Per-user upload counts within range, grouped by Cluster Head — drives
    the Cluster tab's card grid (one card per Cluster Head, one row per
    active user, matching the "Users by Cluster" card design supplied for
    this view). Unlike get_cluster_breakdown()'s aggregate table, this
    keeps individual user identity (first name + Employeeid) rather than
    just a headcount, and titles each card with the Cluster Head's
    Firstname alone (which can itself be multi-word, e.g. "Senthil Kumar"
    — this is NOT the first word of "Firstname Lastname", it's the whole
    Firstname field with Lastname dropped). Only users with at least one
    upload in range appear — this is a usage view, not an org-headcount
    view. Uses the same _resolve_tlid_to_chid() resolution as
    get_cluster_breakdown() (see its docstring for why a raw SQL join
    isn't used here). Buckets are keyed internally by the Cluster Head's
    uid (not by name) so two different Cluster Heads who happen to share a
    first name never merge into one card. Returns a list of
    {cluster_head_name, users: [{name, employee_id, file_count}, ...]},
    cards sorted alphabetically by display name (so 'Unassigned' sits
    wherever it falls alphabetically, not forced to the end), users within
    each card sorted alphabetically by name."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            tlid_to_chid = _resolve_tlid_to_chid(cur)

            cur.execute("SELECT Uid, TLID, Firstname, Employeeid FROM user")
            user_lookup = {
                uid: (tlid, firstname, employee_id)
                for uid, tlid, firstname, employee_id in cur.fetchall()
            }

            clause, params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT uid, COUNT(*) FROM PII_redation_upload_log "
                f"WHERE 1=1{clause} GROUP BY uid",
                params,
            )
            file_counts = cur.fetchall()

            buckets = {}  # bucket_key (chid, or None for Unassigned) -> {"display": name, "users": [...]}
            for uid, cnt in file_counts:
                info = user_lookup.get(uid)
                if not info:
                    continue
                tlid, firstname, employee_id = info
                chid = tlid_to_chid.get(str(tlid).strip()) if tlid and str(tlid).strip() else None
                if chid is not None and chid in user_lookup:
                    bucket_key, display_name = chid, user_lookup[chid][1]
                else:
                    bucket_key, display_name = None, "Unassigned"
                b = buckets.setdefault(bucket_key, {"display": display_name, "users": []})
                b["users"].append(
                    {"name": firstname, "employee_id": employee_id or "—", "file_count": cnt}
                )

            result = [
                {
                    "cluster_head_name": b["display"],
                    "users": sorted(b["users"], key=lambda u: (u["name"] or "").lower()),
                }
                for b in buckets.values()
            ]
            result.sort(key=lambda c: c["cluster_head_name"].lower())
            return result
    finally:
        conn.close()


def get_tl_user_breakdown(start_date: str = None, end_date: str = None) -> list:
    """Per-user upload counts within range, grouped by the employee's own
    Team Leader — drives the dashboard's TL tab card grid (one card per
    TL, one row per active user), one hierarchy level below the Cluster
    tab's Cluster Head grouping (get_cluster_user_breakdown()). Adapted
    from the TL-resolution query supplied for this view
    (plan-dashboard-implementation.md, Section 18): unlike the Cluster
    tab, TL resolution doesn't need teamleaderusermaster at all — an
    employee's own user.TLID *is* their TL's user.Uid directly (the
    supplied query's `JOIN user utl ON utl.uid = u.TLID`), so this looks
    the TL up straight from the in-memory user table. Its
    `WHERE FIND_IN_SET(u.uid, @uidCsv)` scoped to one fixed employee set;
    here every user active in range is resolved instead, to report
    org-wide. Only users with at least one upload in range appear. Users
    with no TLID, or a TLID that doesn't resolve to a real user row, are
    bucketed as 'Unassigned' rather than dropped. Returns a list of
    {tl_name, users: [{name, employee_id, file_count}, ...]}, cards sorted
    alphabetically by TL name, users within each card sorted
    alphabetically by name."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT Uid, TLID, Firstname, Employeeid FROM user")
            user_lookup = {
                uid: (tlid, firstname, employee_id)
                for uid, tlid, firstname, employee_id in cur.fetchall()
            }

            clause, params = _dashboard_date_clause("uploaded_at", start_date, end_date)
            cur.execute(
                "SELECT uid, COUNT(*) FROM PII_redation_upload_log "
                f"WHERE 1=1{clause} GROUP BY uid",
                params,
            )
            file_counts = cur.fetchall()

            buckets = {}  # bucket_key (TL's uid, or None for Unassigned) -> {"display": name, "users": [...]}
            for uid, cnt in file_counts:
                info = user_lookup.get(uid)
                if not info:
                    continue
                tlid, firstname, employee_id = info
                tl_uid = None
                if tlid and str(tlid).strip():
                    try:
                        tl_uid = int(str(tlid).strip())
                    except ValueError:
                        tl_uid = None
                tl_info = user_lookup.get(tl_uid) if tl_uid is not None else None
                if tl_info is not None:
                    bucket_key, display_name = tl_uid, tl_info[1]
                else:
                    bucket_key, display_name = None, "Unassigned"
                b = buckets.setdefault(bucket_key, {"display": display_name, "users": []})
                b["users"].append(
                    {"name": firstname, "employee_id": employee_id or "—", "file_count": cnt}
                )

            result = [
                {
                    "tl_name": b["display"],
                    "users": sorted(b["users"], key=lambda u: (u["name"] or "").lower()),
                }
                for b in buckets.values()
            ]
            result.sort(key=lambda c: c["tl_name"].lower())
            return result
    finally:
        conn.close()


def get_review_time_breakdown(start_date: str = None, end_date: str = None, recent_limit: int = 25) -> dict:
    """Review-timer analytics for the dashboard's time cards and Review Time
    tab (wiki/feature-updates/plan-timer-implementation.md). One
    pii_process_review_time row = one batch reviewed by one user; the date
    filter applies to last_download_at (when the review was last saved).
    All durations are returned in seconds; elapsed = active + idle +
    tab-hidden, the same "Elapsed" shown on the results-screen pill.
    System time is averaged only over batches that have one (NULL when the
    upload had no uid, so there were no upload-log rows to time).

    Returns {summary, per_user, per_client, batches, recent}; batches is every
    row in range, newest first (the page shows `recent`, the first
    `recent_limit`; the Excel export shows all)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            clause, params = _dashboard_date_clause("r.last_download_at", start_date, end_date)
            cur.execute(
                "SELECT r.process_id, r.uid, TIME_TO_SEC(r.active_time), TIME_TO_SEC(r.idle_time), "
                "TIME_TO_SEC(r.tab_hidden_time), TIME_TO_SEC(r.system_time), r.download_count, "
                "r.last_download_at, f.client_id, f.process_name, f.file_count "
                "FROM pii_process_review_time r "
                "LEFT JOIN (SELECT process_id, MIN(client_id) AS client_id, MAX(process_name) AS process_name, "
                "           COUNT(*) AS file_count FROM pii_files GROUP BY process_id) f "
                "  ON f.process_id = r.process_id "
                f"WHERE 1=1{clause} ORDER BY r.last_download_at DESC",
                params,
            )
            rows = cur.fetchall()

            cur.execute("SELECT Uid, Firstname, Lastname, Employeeid FROM user")
            users = {uid: (" ".join(p for p in (first, last) if p) or f"uid {uid}", emp or "—")
                     for uid, first, last, emp in cur.fetchall()}
            client_ids = sorted({r[8] for r in rows if r[8] is not None})
            clients = {}
            if client_ids:
                cur.execute(
                    f"SELECT Pid, ClientName FROM project WHERE Pid IN ({', '.join(['%s'] * len(client_ids))})",
                    client_ids,
                )
                clients = dict(cur.fetchall())
    finally:
        conn.close()

    batches = []
    for (process_id, uid, active, idle, hidden, system, downloads, last_dl,
         client_id, process_name, file_count) in rows:
        active, idle, hidden = int(active or 0), int(idle or 0), int(hidden or 0)
        name, employee_id = users.get(uid, (f"uid {uid}", "—"))
        batches.append({
            "process_id": process_id,
            "process_name": process_name or "—",
            "uid": uid,
            "user_name": name,
            "employee_id": employee_id,
            "client_id": client_id,
            "client_name": clients.get(client_id, "—" if client_id is None else f"Client {client_id}"),
            "file_count": file_count or 0,
            "active_seconds": active,
            "idle_seconds": idle,
            "tab_hidden_seconds": hidden,
            "elapsed_seconds": active + idle + hidden,
            "system_seconds": int(system) if system is not None else None,
            "download_count": downloads or 0,
            "last_download_at": last_dl.strftime("%Y-%m-%d %H:%M") if last_dl else None,
        })

    def aggregate(items):
        n = len(items)
        systems = [b["system_seconds"] for b in items if b["system_seconds"] is not None]
        total = lambda key: sum(b[key] for b in items)
        return {
            "batch_count": n,
            "file_count": total("file_count"),
            "total_active_seconds": total("active_seconds"),
            "total_idle_seconds": total("idle_seconds"),
            "total_elapsed_seconds": total("elapsed_seconds"),
            "avg_active_seconds": round(total("active_seconds") / n) if n else None,
            "avg_idle_seconds": round(total("idle_seconds") / n) if n else None,
            "avg_elapsed_seconds": round(total("elapsed_seconds") / n) if n else None,
            "avg_system_seconds": round(sum(systems) / len(systems)) if systems else None,
        }

    by_user, by_client = {}, {}
    for b in batches:
        by_user.setdefault(b["uid"], []).append(b)
        by_client.setdefault(b["client_id"], []).append(b)

    per_user = [
        {"user_name": items[0]["user_name"], "employee_id": items[0]["employee_id"], **aggregate(items)}
        for items in by_user.values()
    ]
    per_user.sort(key=lambda u: u["total_active_seconds"], reverse=True)
    per_client = [{"client_name": items[0]["client_name"], **aggregate(items)} for items in by_client.values()]
    per_client.sort(key=lambda c: c["total_active_seconds"], reverse=True)

    summary = aggregate(batches)
    summary["user_count"] = len(by_user)
    return {"summary": summary, "per_user": per_user, "per_client": per_client,
            "batches": batches, "recent": batches[:recent_limit]}
