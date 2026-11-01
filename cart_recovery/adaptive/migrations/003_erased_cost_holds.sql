-- In-flight billing survives content erasure without restoring model payloads.
CREATE TABLE af11_cost_holds (
 id uuid PRIMARY KEY,environment text NOT NULL,merchant uuid NOT NULL,
 lease_token uuid NOT NULL,reserved bigint NOT NULL CHECK(reserved>0),
 charged bigint NOT NULL CHECK(charged>=reserved),allocations jsonb NOT NULL CHECK(jsonb_typeof(allocations)='array'),
 finished_at timestamptz,unknown_usage boolean NOT NULL DEFAULT true
);
ALTER TABLE af11_cost_holds ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON af11_cost_holds FROM PUBLIC;
CREATE FUNCTION af11_preserve_cost_hold() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF ROW(NEW.id,NEW.environment,NEW.merchant,NEW.lease_token,NEW.reserved,NEW.allocations) IS DISTINCT FROM ROW(OLD.id,OLD.environment,OLD.merchant,OLD.lease_token,OLD.reserved,OLD.allocations)
 OR NEW.charged<OLD.charged OR (OLD.finished_at IS NOT NULL AND NOT OLD.unknown_usage AND NEW IS DISTINCT FROM OLD)
 THEN RAISE EXCEPTION 'immutable erased accounting binding';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER af11_cost_hold_immutable BEFORE UPDATE ON af11_cost_holds FOR EACH ROW EXECUTE FUNCTION af11_preserve_cost_hold();
