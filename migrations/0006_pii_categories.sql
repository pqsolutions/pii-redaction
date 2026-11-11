-- User-managed list of PII categories for the reference table's "Category"
-- dropdown (previously free text). Seeded from dual_redaction_pipeline.py's
-- SLM_VALID_TOKENS so the dropdown isn't empty on first use; more can be
-- added from the UI's "+ Add category" button (db.add_category()).

CREATE TABLE IF NOT EXISTS pii_categories (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT IGNORE INTO pii_categories (name) VALUES
    ('PERSON'), ('SIGNATURE'), ('IMAGE'), ('EMAIL'), ('PHONE'), ('ADDRESS'),
    ('DOB'), ('PLACE_OF_BIRTH'), ('GENDER'), ('MARITAL_STATUS'), ('GOVERNMENT_ID'),
    ('BUSINESS_ID'), ('BANK_DETAILS'), ('INVESTMENT_ID'), ('EMPLOYMENT_ID'),
    ('PROPERTY_DETAILS'), ('FAMILY_INFO'), ('SENSITIVE_INFO'), ('FINANCIAL_STATUS');
