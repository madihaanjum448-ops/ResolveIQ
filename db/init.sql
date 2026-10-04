-- ResolveIQ PostgreSQL Schema & Initial Data
-- Capability-Aware Procurement Exception Resolver

-- 1. Capability Profile table
CREATE TABLE IF NOT EXISTS capability_profile (
    customer_id VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'FALLBACK', -- NATIVE | FALLBACK | MANUAL
    fallback VARCHAR(64),
    healthy BOOLEAN DEFAULT TRUE,
    last_ok TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    max_age_min INT DEFAULT 15,
    PRIMARY KEY (customer_id, capability)
);

-- 2. Cases table
CREATE TABLE IF NOT EXISTS cases (
    id VARCHAR(64) PRIMARY KEY,
    customer VARCHAR(64) NOT NULL,
    po_number VARCHAR(64) NOT NULL,
    supplier VARCHAR(255) DEFAULT '',
    status VARCHAR(32) NOT NULL, -- OPEN | WAITING | AWAITING_APPROVAL | CLOSED | ESCALATED
    case_class VARCHAR(64) NOT NULL,
    gap INT DEFAULT 0,
    hold REAL DEFAULT 0.0,
    action VARCHAR(64),
    decision VARCHAR(32), -- ALLOWED | NEEDS_HUMAN | BLOCKED
    reasons TEXT, -- JSON array string
    draft TEXT,
    attempts INT DEFAULT 0,
    created VARCHAR(64),
    updated_at VARCHAR(64),
    CONSTRAINT uq_customer_po UNIQUE (customer, po_number)
);

-- 3. Evidence table with Provenance
CREATE TABLE IF NOT EXISTS evidence (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    kind VARCHAR(64) NOT NULL, -- po | grn | invoice | email_claim | shipment | manual
    data TEXT NOT NULL, -- JSON string
    provenance VARCHAR(255),
    method VARCHAR(64), -- native | csv | pdf_regex | pdf_llm | llm_extract | human
    confidence REAL DEFAULT 1.0,
    is_claim INT DEFAULT 0,
    fetched_at VARCHAR(64)
);

-- 4. Events / Audit Log Timeline
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    at VARCHAR(64) NOT NULL,
    type VARCHAR(64) NOT NULL,
    detail TEXT
);

-- 5. Verifications table (tracks every verification attempt and outcome)
CREATE TABLE IF NOT EXISTS verifications (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    at VARCHAR(64) NOT NULL,
    case_class VARCHAR(64) NOT NULL,
    ok BOOLEAN NOT NULL,
    why TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(64),
    details TEXT
);

-- 6. Suppliers table
CREATE TABLE IF NOT EXISTS suppliers (
    id SERIAL PRIMARY KEY,
    customer_id VARCHAR(64) NOT NULL,
    supplier_name VARCHAR(255) NOT NULL,
    supplier_email VARCHAR(255),
    msme_flag INT DEFAULT 0,
    tolerance_pct REAL DEFAULT 2.0,
    price_tolerance_pct REAL DEFAULT 1.0,
    created_at VARCHAR(64),
    CONSTRAINT uq_customer_supplier UNIQUE (customer_id, supplier_name)
);

-- 7. Emails table (WF1 dedupe and linking)
CREATE TABLE IF NOT EXISTS emails (
    message_id VARCHAR(255) PRIMARY KEY,
    case_id VARCHAR(64),
    subject TEXT,
    body TEXT,
    method VARCHAR(64),
    received_at VARCHAR(64)
);

-- 8. Action Keys table (Idempotency gate)
CREATE TABLE IF NOT EXISTS action_keys (
    key VARCHAR(255) PRIMARY KEY,
    created_at VARCHAR(64)
);

-- 9. Path Log table (Tracks capability router decisions and fallbacks)
CREATE TABLE IF NOT EXISTS path_log (
    id SERIAL PRIMARY KEY,
    at VARCHAR(64) NOT NULL,
    customer VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    path VARCHAR(32) NOT NULL,
    note TEXT
);

-- 10. Health table (Runtime status of integrations)
CREATE TABLE IF NOT EXISTS health (
    customer VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    healthy INT DEFAULT 1,
    updated_at VARCHAR(64),
    PRIMARY KEY (customer, capability)
);

-- 11. Config table
CREATE TABLE IF NOT EXISTS config (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT,
    updated_at VARCHAR(64)
);

-- Seed Capability Profiles for Company X and Company Y
INSERT INTO capability_profile (customer_id, capability, provider, fallback, healthy, max_age_min)
VALUES
    ('X', 'po', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'grn', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'invoice', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'shipment', 'NATIVE', 'email', TRUE, 15),
    ('X', 'mismatch', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'messaging', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'approvals', 'NATIVE', 'csv', TRUE, 15),
    ('X', 'writeback', 'MANUAL', NULL, TRUE, 15),
    ('X', 'verify', 'NATIVE', 'csv', TRUE, 15),
    ('Y', 'po', 'FALLBACK', 'csv', TRUE, 15),
    ('Y', 'grn', 'FALLBACK', 'csv', TRUE, 15),
    ('Y', 'invoice', 'FALLBACK', 'pdf', TRUE, 15),
    ('Y', 'shipment', 'FALLBACK', 'email', TRUE, 15),
    ('Y', 'mismatch', 'FALLBACK', 'csv', TRUE, 15),
    ('Y', 'messaging', 'FALLBACK', 'csv', TRUE, 15),
    ('Y', 'approvals', 'FALLBACK', 'csv', TRUE, 15),
    ('Y', 'writeback', 'MANUAL', NULL, TRUE, 15),
    ('Y', 'verify', 'FALLBACK', 'csv', TRUE, 15)
ON CONFLICT (customer_id, capability) DO NOTHING;
