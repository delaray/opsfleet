# datachatdb — PostgreSQL report library

## Purpose

`datachatdb` is the durable relational store for the OpsFleet data-analysis chat
assistant. It holds everything the assistant must remember beyond a single process:

- **Saved reports** generated from validated BigQuery aggregates, each owned by one
  executive and bound to the product scope (`scope_hash`) they were generated under.
- **Conversations** that reports and deletion requests belong to.
- **Deletion operations**: frozen, expiring approval requests for the human-in-the-loop
  LangGraph deletion workflow (`src/report_workflow.py`).
- **An append-only audit log** of saves and deletion decisions.

Analytical data itself stays in BigQuery. `datachatdb` contains only generated report
text, aggregate provenance, and workflow state.

The application code is `src/report_store.py`; the schema is `db/schema.sql`;
provisioning is `scripts/setup_datachatdb.py`. All of it replaced the earlier
`.local/opsfleet.sqlite3` file.

## Databases

| Database          | Owner     | Use |
|-------------------|-----------|-----|
| `datachatdb`      | `admindb` | Application data (CLI `--chat` / `--report`). |
| `datachatdb_test` | `admindb` | Identical schema; disposable. Unit tests and `examples/report_demo.py` truncate it freely. |

Both databases revoke all privileges from `PUBLIC`; only `admindb` (owner) and
`datachat_app` may connect. Tables live in the schema `datachat`.

## Roles

| Role                 | Login | Key attributes | Purpose |
|----------------------|-------|----------------|---------|
| `admindb`            | yes (password `postgres`) | `CREATEROLE`, `BYPASSRLS`, not superuser | Database/schema owner and administrator. Applies `db/schema.sql`, reads all rows for support/audit, and holds `ADMIN` on the roles below so it can onboard executives without the installation superuser. **Change the password outside local development.** |
| `datachat_app`       | yes (generated password in `.env`) | `NOINHERIT`, no table privileges | The only role the application connects as. It can do nothing until it switches to an executive role. |
| `datachat_executive` | no    | group role | Holds every table privilege; executives inherit from it. |
| `user-a`, `user-b`, `user-c` | no | members of `datachat_executive` | One role per executive. Each name matches the `--user` identity and the grants in `config/grants.executives.json`. `datachat_app` is granted these roles `WITH INHERIT FALSE, SET TRUE`, so it may only *become* them. |

The installation superuser (`PG_*` in `.env`) is used only by
`scripts/setup_datachatdb.py` to create these roles and databases. Nothing at
runtime uses it.

### How authorization is enforced

1. Each `ReportStore` transaction begins with `SET LOCAL ROLE "<owner>"`. An unknown
   identity, or a role that isn't a `datachat_executive` member (e.g. `datachat_app` itself),
   raises `PolicyDenied`.
2. **Row-level security** (enabled *and forced*) on every table applies the policy
   `owner_id = current_user` for both reading (`USING`) and writing (`WITH CHECK`). An
   executive cannot see, insert, or change another executive's rows, even if the
   application passes the wrong owner.
3. **Command/column grants**: there is no `DELETE` on any table; `report_audit` is
   `SELECT`/`INSERT` only (append-only); `reports` allows `UPDATE` only of
   `deleted_at`, `version` (soft delete); `deletion_operations` allows `UPDATE` only of `status`.
4. The application's own checks remain: conversation ownership, current scope-hash match
   for reading content, frozen-target versions, and expiry.

Deletion confirmation locks the operation row and each target report
(`SELECT … FOR UPDATE`) so concurrent decisions on the same reports serialize; a
stale target aborts the whole operation.

## Tables (schema `datachat`)

### `conversations`
| Column     | Type | Notes |
|------------|------|-------|
| `id`       | `uuid` PK | Conversation ID shown by the CLI (`--conversation-id`). |
| `owner_id` | `text` | Executive role name. `UNIQUE (id, owner_id)` is the FK target. |

### `reports`
| Column            | Type | Notes |
|-------------------|------|-------|
| `id`              | `uuid` PK | |
| `owner_id`        | `text` | Executive role name; RLS key. |
| `conversation_id` | `uuid` | FK `(conversation_id, owner_id)` → `conversations`. |
| `title`, `body`   | `text` | Generated Markdown report. |
| `sources_json`    | `json` | Aggregate evidence provenance. `json` (not `jsonb`) preserves the exact text. |
| `scope_hash`      | `text` | Fingerprint of the product grants at creation time. |
| `version`         | `integer` ≥ 1 | Incremented on soft delete; used for stale-target detection. |
| `created_at`, `deleted_at` | `double precision` | Epoch seconds; `deleted_at IS NULL` means live. |

Index `report_owner (owner_id, deleted_at)`.

### `deletion_operations`
| Column            | Type | Notes |
|-------------------|------|-------|
| `id`              | `uuid` PK | Confirmation token (`DELETE <id>` / `/cancel <id>`). |
| `owner_id`, `conversation_id` | | FK → `conversations`. |
| `status`          | `text` | `pending`, `confirmed`, `cancelled`, `expired`, `stale` (CHECK). |
| `targets_json`    | `jsonb` | Frozen `[{id, title, version}]`. |
| `created_at`, `expires_at` | `double precision` | Epoch seconds; lifetime 1–3600 s (default 900). |

### `report_audit`
| Column            | Type | Notes |
|-------------------|------|-------|
| `id`              | `bigint` identity PK | Ordering. |
| `owner_id`        | `text` | |
| `operation_id`    | `uuid` | Null for `report_saved`. |
| `action`          | `text` | `report_saved`, `deletion_requested`, `deletion_confirmed`, `deletion_cancelled`, `deletion_expired`, `deletion_stale`. |
| `report_ids_json` | `jsonb` | Affected report IDs. |
| `occurred_at`     | `double precision` | Epoch seconds. |

Index `report_audit_owner (owner_id, id)`.

## Configuration

`scripts/setup_datachatdb.py` appends these to `.env`. A container needs only these, not
the installation `PG_*` credentials:

```
DATACHAT_DB_HOST, DATACHAT_DB_PORT
DATACHAT_DB_NAME=datachatdb        DATACHAT_TEST_DB_NAME=datachatdb_test
DATACHAT_DB_USER=datachat_app      DATACHAT_DB_PASSWORD=<generated>
DATACHAT_DB_ADMIN_USER=admindb     DATACHAT_DB_ADMIN_PASSWORD=postgres
```

`src.report_store.datachat_conninfo()` builds the connection string from them.
`main.py --reports-dsn` overrides it.

## Operations

```bash
uv run python -m scripts.setup_datachatdb   # idempotent: (re)create roles, DBs, schema, grants, RLS
```

Adding an executive (e.g. `user-d`) as `admindb`. Also add their grants to the grant file:

```sql
CREATE ROLE "user-d" NOLOGIN INHERIT;
GRANT datachat_executive TO "user-d";
GRANT "user-d" TO datachat_app WITH INHERIT FALSE, SET TRUE;
```

Inspecting as an executive would see it:

```sql
BEGIN; SET LOCAL ROLE "user-a"; SELECT id, title FROM datachat.reports; COMMIT;
```

For Docker, run `db/schema.sql` as `admindb` against `datachatdb` after creating the
roles (the role statements are in `scripts/setup_datachatdb.py`).

## Limitations

- `admindb` and the installation superuser can alter audit rows; the audit log is not
  tamper-proof against database administrators.
- Executive roles mirror the trusted CLI `--user` selection; they are not end-user
  authentication.
- Timestamps are epoch seconds (`double precision`) so tests can inject a clock.
