-- First-start initialization of the postgres container (empty data volume only).
-- Runs as POSTGRES_USER (admindb) in POSTGRES_DB (datachatdb). Mirrors the roles that
-- scripts/setup_datachatdb.py creates on a host installation, then applies db/schema.sql.

\set ON_ERROR_STOP on
\set app_password `cat /run/secrets/datachat_db_password`

CREATE ROLE datachat_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD :'app_password';
CREATE ROLE datachat_executive NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE "user-a" NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT IN ROLE datachat_executive;
CREATE ROLE "user-b" NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT IN ROLE datachat_executive;
CREATE ROLE "user-c" NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT IN ROLE datachat_executive;
-- The app may only become an executive (SET ROLE); it never inherits their privileges.
GRANT "user-a", "user-b", "user-c" TO datachat_app WITH INHERIT FALSE, SET TRUE;

REVOKE ALL ON DATABASE datachatdb FROM PUBLIC;
GRANT CONNECT ON DATABASE datachatdb TO datachat_app;

\i /datachat/schema.sql
