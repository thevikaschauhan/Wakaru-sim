-- Explicit local/operator migration. No automatic startup migration.
CREATE TABLE knowledge_retained.cleanup_receipts (
 scope_key text NOT NULL, command_key text NOT NULL,
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 scope_sha256 text NOT NULL CHECK(scope_sha256 ~ '^[0-9a-f]{64}$'),
 status text NOT NULL CHECK(status IN('applied','refused')),
 result jsonb NOT NULL,
 PRIMARY KEY(scope_key,command_key)
);
CREATE TRIGGER cleanup_receipt_no_change BEFORE UPDATE OR DELETE ON knowledge_retained.cleanup_receipts FOR EACH ROW EXECUTE FUNCTION knowledge_retained.delivery_receipt_immutable();
CREATE TRIGGER cleanup_receipt_no_truncate BEFORE TRUNCATE ON knowledge_retained.cleanup_receipts FOR EACH STATEMENT EXECUTE FUNCTION knowledge_retained.delivery_receipt_immutable();
