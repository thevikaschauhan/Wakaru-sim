-- Explicit migration; no runtime enablement. Tombstones outlive erased payloads.
ALTER TABLE af6_requests ADD COLUMN journey_id uuid;
ALTER TABLE af6_evaluations ADD COLUMN journey_id uuid;
-- Pre-AF11 rows without an authoritative journey binding are never guessed.
-- Inventory reports them as unmapped; qualification requires migration reconciliation.
CREATE TABLE af11_erasure_fences(merchant uuid NOT NULL,journey_id uuid NOT NULL,erased_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(merchant,journey_id));
ALTER TABLE af11_erasure_fences ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON af11_erasure_fences FROM PUBLIC;
CREATE FUNCTION af11_semantic_privacy() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.merchant::text||':adaptive-privacy',0));
 IF NEW.journey_id IS NOT NULL AND EXISTS(SELECT 1 FROM af11_erasure_fences WHERE merchant=NEW.merchant AND journey_id=NEW.journey_id) THEN RAISE EXCEPTION 'adaptive privacy fence';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER af11_request_privacy BEFORE INSERT ON af6_requests FOR EACH ROW EXECUTE FUNCTION af11_semantic_privacy();
CREATE TRIGGER af11_evaluation_privacy BEFORE INSERT ON af6_evaluations FOR EACH ROW EXECUTE FUNCTION af11_semantic_privacy();
