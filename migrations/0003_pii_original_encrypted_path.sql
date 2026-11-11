-- Adds storage for an encrypted copy of the original file, needed for image
-- (PNG/JPG/JPEG) "As is" rehydration.
--
-- Unlike PDF/DOCX/XLSX "As is" redaction, which burns the replacement token
-- in as real, searchable text and can rehydrate by finding-and-replacing that
-- text, image "As is" redaction draws an opaque box directly onto pixels —
-- there is no text layer left to search. So image rehydration instead
-- restores a securely stored copy of the original file (see
-- crypto_utils.encrypt_bytes / dual_redaction_pipeline._store_original_encrypted).

ALTER TABLE pii_files
  ADD COLUMN original_encrypted_path VARCHAR(500) NULL AFTER output_format;
