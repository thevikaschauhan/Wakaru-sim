-- Apply explicitly to the shared v2 semantic database. Never auto-migrate at boot.
CREATE TABLE af6_threshold_registry (sha256 text PRIMARY KEY, policy jsonb NOT NULL);
CREATE TABLE af6_budget_policies (environment text PRIMARY KEY, limits jsonb NOT NULL);
CREATE TABLE af6_deployments (
 environment text NOT NULL, workflow text NOT NULL DEFAULT 'planning', binding text NOT NULL, enabled boolean NOT NULL DEFAULT false,
 revision bigint NOT NULL DEFAULT 0, threshold_sha256 text NOT NULL REFERENCES af6_threshold_registry(sha256), PRIMARY KEY(environment,workflow)
);
CREATE TABLE af6_requests (
 environment text NOT NULL,merchant uuid NOT NULL,request_key text NOT NULL,
 request_hash text NOT NULL,request jsonb NOT NULL,selected jsonb,
 lease_token uuid,lease_until timestamptz,attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 retry_at timestamptz, failures integer NOT NULL DEFAULT 0,
 PRIMARY KEY(environment,merchant,request_key)
);
CREATE TABLE af6_attempts (
 id uuid PRIMARY KEY,environment text NOT NULL,merchant uuid NOT NULL,request_key text NOT NULL,
 lease_token uuid NOT NULL,reserved bigint NOT NULL CHECK(reserved>0),charged bigint NOT NULL CHECK(charged>=0),
 result jsonb,raw_sha256 text,started_at timestamptz NOT NULL DEFAULT clock_timestamp(),finished_at timestamptz,
 FOREIGN KEY(environment,merchant,request_key) REFERENCES af6_requests(environment,merchant,request_key)
);
CREATE TABLE af6_budgets (
 environment text NOT NULL,scope text NOT NULL,identity text NOT NULL,period text NOT NULL,
 spent bigint NOT NULL DEFAULT 0 CHECK(spent>=0),PRIMARY KEY(environment,scope,identity,period)
);
CREATE TABLE af6_allocations (
 attempt uuid REFERENCES af6_attempts(id),environment text NOT NULL,scope text NOT NULL,identity text NOT NULL,period text NOT NULL,
 PRIMARY KEY(attempt,scope,identity,period),FOREIGN KEY(environment,scope,identity,period) REFERENCES af6_budgets(environment,scope,identity,period)
);
CREATE TABLE af6_evaluations (
 environment text NOT NULL,merchant uuid NOT NULL,idempotency_key text NOT NULL,request_hash text NOT NULL,
 result jsonb,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(environment,merchant,idempotency_key)
);
CREATE TABLE af6_circuits (
 environment text NOT NULL,merchant uuid NOT NULL,failures integer NOT NULL DEFAULT 0,open_until timestamptz,probe_token uuid,probe_until timestamptz,
 PRIMARY KEY(environment,merchant)
);
-- First complete result wins, including low confidence and malformed outputs.
CREATE FUNCTION af6_preserve_result() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_TABLE_NAME='af6_requests' THEN
  IF ROW(NEW.environment,NEW.merchant,NEW.request_key,NEW.request_hash,NEW.request) IS DISTINCT FROM ROW(OLD.environment,OLD.merchant,OLD.request_key,OLD.request_hash,OLD.request) OR (OLD.selected IS NOT NULL AND NEW.selected IS DISTINCT FROM OLD.selected) THEN RAISE EXCEPTION 'immutable semantic request/result'; END IF;
 ELSIF OLD.result IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'immutable evaluation'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER af6_request_immutable BEFORE UPDATE ON af6_requests FOR EACH ROW EXECUTE FUNCTION af6_preserve_result();
CREATE TRIGGER af6_evaluation_immutable BEFORE UPDATE ON af6_evaluations FOR EACH ROW EXECUTE FUNCTION af6_preserve_result();
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['af6_budget_policies','af6_threshold_registry','af6_deployments','af6_requests','af6_attempts','af6_budgets','af6_allocations','af6_evaluations','af6_circuits'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
 EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tab);
 END LOOP; END $$;

CREATE FUNCTION af6_deployment_revision() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 NEW.revision := OLD.revision+1; RETURN NEW; END $$;
CREATE TRIGGER af6_deployment_changed BEFORE UPDATE ON af6_deployments FOR EACH ROW EXECUTE FUNCTION af6_deployment_revision();

CREATE FUNCTION af6_immutable_policy() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 RAISE EXCEPTION 'immutable threshold registry'; END $$;
CREATE TRIGGER af6_threshold_immutable BEFORE UPDATE OR DELETE ON af6_threshold_registry FOR EACH ROW EXECUTE FUNCTION af6_immutable_policy();

CREATE TRIGGER af6_budget_policy_immutable BEFORE UPDATE OR DELETE ON af6_budget_policies FOR EACH ROW EXECUTE FUNCTION af6_immutable_policy();
