-- Explicit operator/fixture migration. Never applied during service startup.
-- Back up knowledge_data separately. Overlay current knowledge_retained fences
-- before releasing a restored database from quarantine.
CREATE SCHEMA knowledge_data;
CREATE SCHEMA knowledge_retained;
CREATE TABLE knowledge_retained.control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    quarantined boolean NOT NULL DEFAULT true,
    key_fingerprint text
);
INSERT INTO knowledge_retained.control(singleton) VALUES(true);
CREATE TABLE knowledge_retained.tenants (
    tenant_key text PRIMARY KEY,
    erased boolean NOT NULL DEFAULT false
);
CREATE TABLE knowledge_retained.subjects (
    scope_key text PRIMARY KEY,
    tenant_key text NOT NULL REFERENCES knowledge_retained.tenants,
    identity_revision bigint NOT NULL CHECK(identity_revision > 0),
    privacy_generation bigint NOT NULL CHECK(privacy_generation > 0),
    data_version bigint NOT NULL DEFAULT 0 CHECK(data_version >= 0),
    erased boolean NOT NULL DEFAULT false
);
CREATE INDEX knowledge_subjects_tenant ON knowledge_retained.subjects(tenant_key);
CREATE TABLE knowledge_retained.withdrawals (
    scope_key text NOT NULL REFERENCES knowledge_retained.subjects,
    source_key text NOT NULL,
    PRIMARY KEY(scope_key, source_key)
);
-- Payload-free invalidation ledger. No external delivery is enabled in this slice.
CREATE TABLE knowledge_retained.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_key text NOT NULL,
    scope_key text,
    data_version bigint,
    kind text NOT NULL CHECK(kind IN ('write','source_withdrawal','identity_revision','subject_erasure','merchant_erasure'))
);
CREATE TABLE knowledge_data.subjects (
    scope_key text PRIMARY KEY,
    scope jsonb NOT NULL,
    payload jsonb,
    data_version bigint NOT NULL CHECK(data_version >= 0)
);
CREATE TABLE knowledge_data.sources (
    scope_key text NOT NULL REFERENCES knowledge_data.subjects ON DELETE CASCADE,
    source_key text NOT NULL,
    receipt jsonb NOT NULL,
    PRIMARY KEY(scope_key, source_key)
);
CREATE TABLE knowledge_data.catalog (
    scope_key text NOT NULL REFERENCES knowledge_data.subjects ON DELETE CASCADE,
    entity_key text NOT NULL,
    receipt jsonb NOT NULL,
    PRIMARY KEY(scope_key, entity_key)
);
CREATE TABLE knowledge_data.commands (
    scope_key text NOT NULL REFERENCES knowledge_data.subjects ON DELETE CASCADE,
    command_key text NOT NULL,
    request_hash text NOT NULL,
    data_version bigint NOT NULL,
    PRIMARY KEY(scope_key, command_key)
);
