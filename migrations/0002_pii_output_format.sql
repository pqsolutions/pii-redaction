-- Adds output format tracking to pii_files, for the "As is" vs "Excel"
-- download option (wiki/feature-updates/phase-1/plan-as-it-is-implementation.md).
--
-- Needed because redacted_path's file extension alone is ambiguous for
-- .xlsx: an xlsx-in/xlsx-out "As is" file and a PDF/DOCX "Excel" (flattened)
-- file are both .xlsx but need different readers on rehydration.

ALTER TABLE pii_files
  ADD COLUMN output_format ENUM('excel','as_is') NOT NULL DEFAULT 'excel' AFTER redacted_path;
