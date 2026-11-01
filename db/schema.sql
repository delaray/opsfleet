-- datachatdb schema: owned report library, deletion approvals and audit.
-- Run as the database owner (admindb) against datachatdb (or datachatdb_test).
-- Idempotent: safe to re-run. Roles are created by scripts/setup_datachatdb.py.
--
-- Authorization model
--   * datachat_app (LOGIN) holds no table privileges of its own. Each transaction runs
--     SET LOCAL ROLE "<executive>", so every statement executes as that executive.
--   * "user-a", "user-b", "user-c" (NOLOGIN) inherit privileges from datachat_executive.
--   * Row-level security limits every row to owner_id = current_user, so one executive
--     can never read, insert or modify another executive's rows, even through an
--     application bug.
--   * Column/command grants make the audit log append-only and report deletion soft-only.

CREATE SCHEMA IF NOT EXISTS datachat AUTHORIZATION admindb;
REVOKE ALL ON SCHEMA datachat FROM PUBLIC;
GRANT USAGE ON SCHEMA datachat TO datachat_executive;

SET search_path = datachat;

CREATE TABLE IF NOT EXISTS conversations (
    id       uuid PRIMARY KEY,
    owner_id text NOT NULL,
    UNIQUE (id, owner_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id              uuid PRIMARY KEY,
    owner_id        text NOT NULL,
    conversation_id uuid NOT NULL,
    title           text NOT NULL,
    body            text NOT NULL,
    sources_json    json NOT NULL,   -- json (not jsonb) preserves the exact provenance text
    scope_hash      text NOT NULL,
    version         integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at      double precision NOT NULL,
    deleted_at      double precision,
    FOREIGN KEY (conversation_id, owner_id) REFERENCES conversations (id, owner_id)
);
CREATE INDEX IF NOT EXISTS report_owner ON reports (owner_id, deleted_at);

CREATE TABLE IF NOT EXISTS deletion_operations (
    id              uuid PRIMARY KEY,
    owner_id        text NOT NULL,
    conversation_id uuid NOT NULL,
    status          text NOT NULL
                    CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired', 'stale')),
    targets_json    jsonb NOT NULL,
    created_at      double precision NOT NULL,
    expires_at      double precision NOT NULL,
    FOREIGN KEY (conversation_id, owner_id) REFERENCES conversations (id, owner_id)
);

CREATE TABLE IF NOT EXISTS report_audit (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id        text NOT NULL,
    operation_id    uuid,
    action          text NOT NULL,
    report_ids_json jsonb NOT NULL,
    occurred_at     double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS report_audit_owner ON report_audit (owner_id, id);

-- Least-privilege grants: no DELETE anywhere, no UPDATE on audit, and only the
-- columns the soft-delete/decision workflow needs to change.
REVOKE ALL ON ALL TABLES IN SCHEMA datachat FROM PUBLIC, datachat_executive;
GRANT SELECT, INSERT ON conversations TO datachat_executive;
GRANT SELECT, INSERT ON reports TO datachat_executive;
GRANT UPDATE (deleted_at, version) ON reports TO datachat_executive;
GRANT SELECT, INSERT ON deletion_operations TO datachat_executive;
GRANT UPDATE (status) ON deletion_operations TO datachat_executive;
GRANT SELECT, INSERT ON report_audit TO datachat_executive;

-- Row-level security: an executive sees and writes only rows they own.
-- FORCE applies the policies to the table owner too; admindb has BYPASSRLS for administration.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['conversations', 'reports', 'deletion_operations', 'report_audit'] LOOP
        EXECUTE format('ALTER TABLE datachat.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE datachat.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS owner_only ON datachat.%I', t);
        EXECUTE format('CREATE POLICY owner_only ON datachat.%I TO datachat_executive '
                       'USING (owner_id = current_user) WITH CHECK (owner_id = current_user)', t);
    END LOOP;
END
$$;
