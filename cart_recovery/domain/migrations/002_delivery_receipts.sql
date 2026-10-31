-- Explicit local/operator migration. No startup registration.
-- Retain command identity independently of erasable subject data.
CREATE TABLE knowledge_retained.delivery_receipts (
 scope_key text NOT NULL,
 command_key text NOT NULL,
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 scope_sha256 text NOT NULL CHECK(scope_sha256 ~ '^[0-9a-f]{64}$'),
 operation text NOT NULL CHECK(operation IN('scope_sync','source','withdraw_source','erase_subject')),
 PRIMARY KEY(scope_key,command_key)
);
CREATE FUNCTION knowledge_retained.delivery_receipt_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'delivery_receipt_retained'; END $$;
CREATE TRIGGER delivery_receipt_no_change BEFORE UPDATE OR DELETE ON knowledge_retained.delivery_receipts FOR EACH ROW EXECUTE FUNCTION knowledge_retained.delivery_receipt_immutable();
CREATE TRIGGER delivery_receipt_no_truncate BEFORE TRUNCATE ON knowledge_retained.delivery_receipts FOR EACH STATEMENT EXECUTE FUNCTION knowledge_retained.delivery_receipt_immutable();
