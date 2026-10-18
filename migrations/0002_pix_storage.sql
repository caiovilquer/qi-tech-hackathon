-- Stage 4: durable internal coordination. No HTTP route is enabled by this DDL.
CREATE TABLE pix_account (
    tenant_id varchar(64) NOT NULL,
    branch varchar(4) NOT NULL,
    account varchar(20) NOT NULL,
    document varchar(14) NOT NULL,
    PRIMARY KEY (tenant_id, branch, account),
    CHECK (tenant_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    CHECK (branch ~ '^[0-9]{1,4}$' AND account ~ '^[0-9]{1,20}$')
);
CREATE TABLE analysis_claim (
    tenant_id varchar(64) NOT NULL,
    external_id varchar(128) NOT NULL,
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    owner_id uuid NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    lease_until timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('in_progress','completed')),
    analysis_key uuid,
    PRIMARY KEY (tenant_id, external_id),
    CHECK (tenant_id ~ '^[A-Za-z0-9_-]{1,64}$' AND length(external_id)>0),
    CHECK ((state='completed') = (analysis_key IS NOT NULL))
);
CREATE INDEX ix_analysis_claim_lease ON analysis_claim (state,lease_until);
CREATE TABLE source_initial_check (
    tenant_id varchar(64) NOT NULL,
    branch varchar(4) NOT NULL,
    account varchar(20) NOT NULL,
    document varchar(14) NOT NULL,
    claim_external_id varchar(128) NOT NULL,
    owner_id uuid NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    lease_until timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('in_progress','completed')),
    result jsonb,
    completed_at timestamptz,
    PRIMARY KEY (tenant_id,branch,account,document),
    FOREIGN KEY (tenant_id,claim_external_id) REFERENCES analysis_claim (tenant_id,external_id),
    CHECK ((state='completed') = (result IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX ix_source_initial_lease ON source_initial_check (state,lease_until);
CREATE TABLE pix_transaction (
    id uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    external_id varchar(128) NOT NULL,
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    raw_body bytea CHECK (octet_length(raw_body) <= 65536),
    amount_cents bigint NOT NULL CHECK (amount_cents BETWEEN 1 AND 100000000000),
    payer_branch varchar(4) NOT NULL,
    payer_account varchar(20) NOT NULL,
    payer_document varchar(14) NOT NULL,
    payee_document varchar(14) NOT NULL,
    participant varchar(8) NOT NULL,
    destination_branch varchar(4) NOT NULL,
    destination_account varchar(20) NOT NULL,
    transaction_at timestamptz NOT NULL,
    source_opened_at timestamptz NOT NULL,
    destination_opened_at timestamptz NOT NULL,
    device_id varchar(128) NOT NULL CHECK (length(btrim(device_id))>0),
    channel varchar(64) NOT NULL,
    ip text,
    geolocation jsonb,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_pix_tenant_external_id UNIQUE (tenant_id,external_id),
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,external_id) REFERENCES analysis_claim (tenant_id,external_id),
    CHECK (source_opened_at<=transaction_at AND destination_opened_at<=transaction_at)
);
CREATE INDEX ix_pix_payer_time ON pix_transaction (tenant_id,payer_branch,payer_account,payer_document,transaction_at);
CREATE INDEX ix_pix_destination_time ON pix_transaction (tenant_id,participant,destination_branch,destination_account,transaction_at);
CREATE TABLE risk_analysis (
    key uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    pix_id uuid NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('approved','two_factor_required','rejected')),
    decision_at timestamptz NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    policy_version varchar(128) NOT NULL,
    score numeric NOT NULL CHECK (score>=0 AND score=trunc(score)),
    device_state text NOT NULL CHECK (device_state IN ('registered','unregistered','revoked','unknown')),
    night_qualifies boolean NOT NULL,
    public_response jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    UNIQUE (tenant_id,key),
    FOREIGN KEY (tenant_id,pix_id) REFERENCES pix_transaction (tenant_id,id),
    CHECK (public_response->>'key'=key::text AND public_response->>'status'=status),
    CHECK (jsonb_typeof(public_response->'metadata')='object'),
    CHECK (public_response ?& ARRAY['key','status','metadata'] AND
           public_response-'key'-'status'-'metadata'='{}'::jsonb)
);
CREATE INDEX ix_analysis_decision ON risk_analysis (tenant_id,decision_at,pix_id);
ALTER TABLE analysis_claim ADD FOREIGN KEY (tenant_id,analysis_key) REFERENCES risk_analysis (tenant_id,key);
CREATE TABLE request_receipt (
    id uuid PRIMARY KEY,
    request_id varchar(128) NOT NULL,
    tenant_id varchar(64),
    external_id varchar(128),
    payload_hash char(64) CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    http_status integer CHECK (http_status BETWEEN 100 AND 599),
    category varchar(64),
    analysis_key uuid,
    FOREIGN KEY (tenant_id,analysis_key) REFERENCES risk_analysis (tenant_id,key)
);
CREATE TABLE bank_challenge (
    tenant_id varchar(64) NOT NULL,
    provider varchar(64) NOT NULL,
    challenge_id varchar(128) NOT NULL,
    pix_id uuid NOT NULL,
    analysis_key uuid NOT NULL,
    operation_fingerprint char(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,provider,challenge_id),
    UNIQUE (tenant_id,pix_id),
    FOREIGN KEY (tenant_id,pix_id) REFERENCES pix_transaction (tenant_id,id),
    FOREIGN KEY (tenant_id,analysis_key) REFERENCES risk_analysis (tenant_id,key)
);
CREATE TABLE bank_operation_link (
    tenant_id varchar(64) NOT NULL,
    provider varchar(64) NOT NULL,
    operation_id varchar(128) NOT NULL,
    pix_id uuid NOT NULL,
    operation_fingerprint char(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,provider,operation_id),
    UNIQUE (tenant_id,provider,pix_id),
    FOREIGN KEY (tenant_id,pix_id) REFERENCES pix_transaction (tenant_id,id)
);
CREATE TABLE bank_event (
    id uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    provider varchar(64) NOT NULL,
    event_id varchar(128) NOT NULL,
    payload_hash char(64) NOT NULL,
    pix_id uuid NOT NULL,
    analysis_key uuid NOT NULL,
    operation_fingerprint char(64) NOT NULL,
    kind text NOT NULL CHECK (kind IN ('execution','auth')),
    state text NOT NULL,
    terminal boolean NOT NULL,
    challenge_id varchar(128),
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_version bigint NOT NULL CHECK (source_version>=0),
    payload jsonb NOT NULL,
    UNIQUE (tenant_id,provider,event_id),
    FOREIGN KEY (tenant_id,pix_id) REFERENCES pix_transaction (tenant_id,id),
    FOREIGN KEY (tenant_id,analysis_key) REFERENCES risk_analysis (tenant_id,key),
    CHECK (occurred_at<=received_at),
    CHECK ((kind='execution' AND state IN ('executed','failed','cancelled') AND terminal AND challenge_id IS NULL)
        OR (kind='auth' AND state IN ('passed','failed','expired') AND challenge_id IS NOT NULL
            AND (state='failed' OR terminal)))
);
CREATE INDEX ix_bank_event_operation ON bank_event (tenant_id,pix_id,occurred_at,source_version,received_at);
CREATE INDEX ix_bank_event_challenge ON bank_event (tenant_id,provider,challenge_id,occurred_at,source_version);
CREATE TABLE provider_snapshot (
    id uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    key_hash char(64) NOT NULL,
    kind text NOT NULL CHECK (kind IN ('security','reputation')),
    observed_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    source_version bigint NOT NULL CHECK (source_version>=0),
    fact_hash char(64) NOT NULL,
    record jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,key_hash,observed_at,source_version),
    CHECK (observed_at<=available_at AND observed_at<valid_until)
);
CREATE INDEX ix_provider_snapshot_latest ON provider_snapshot (tenant_id,key_hash,observed_at DESC,source_version DESC);
-- Protect the original analysis and accepted facts even from accidental ORM updates.
CREATE FUNCTION pix_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable Pix record: %', TG_TABLE_NAME USING ERRCODE='23514';
END $$;
CREATE TRIGGER immutable_pix BEFORE UPDATE OR DELETE ON pix_transaction FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE TRIGGER immutable_analysis BEFORE UPDATE OR DELETE ON risk_analysis FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE TRIGGER immutable_bank_event BEFORE UPDATE OR DELETE ON bank_event FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE TRIGGER immutable_challenge BEFORE UPDATE OR DELETE ON bank_challenge FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE TRIGGER immutable_bank_link BEFORE UPDATE OR DELETE ON bank_operation_link FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE TRIGGER immutable_account BEFORE UPDATE OR DELETE ON pix_account FOR EACH ROW EXECUTE FUNCTION pix_reject_mutation();
CREATE FUNCTION pix_guard_claim() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'permanent Pix reservation' USING ERRCODE='23514';
    END IF;
    IF OLD.state='completed' OR NEW.tenant_id<>OLD.tenant_id OR NEW.external_id<>OLD.external_id
        OR NEW.payload_hash<>OLD.payload_hash OR NEW.fencing_token<OLD.fencing_token THEN
        RAISE EXCEPTION 'immutable claim identity/result' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_analysis_claim BEFORE UPDATE OR DELETE ON analysis_claim FOR EACH ROW EXECUTE FUNCTION pix_guard_claim();
CREATE FUNCTION pix_guard_initial() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'permanent initial check' USING ERRCODE='23514';
    END IF;
    IF OLD.state='completed' OR (NEW.tenant_id,NEW.branch,NEW.account,NEW.document)<>
       (OLD.tenant_id,OLD.branch,OLD.account,OLD.document) OR NEW.fencing_token<OLD.fencing_token THEN
        RAISE EXCEPTION 'immutable initial identity/result' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_initial BEFORE UPDATE OR DELETE ON source_initial_check FOR EACH ROW EXECUTE FUNCTION pix_guard_initial();
