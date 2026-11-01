"""Provision datachatdb: roles, databases, schema, row-level security and .env credentials.

Run once (idempotent) with: uv run python -m scripts.setup_datachatdb

The installation superuser credentials (PG_HOST/PG_PORT/PG_USER/PG_PWD in .env) are used
only here, to create roles and databases. Everything afterwards uses the DATACHAT_* role
credentials this script writes to .env, so a containerized deployment needs only those.
"""

import secrets
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql

ENV_PATH = Path('.env')
SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'db' / 'schema.sql'
DATABASES = ('datachatdb', 'datachatdb_test')
ADMIN_ROLE, ADMIN_PASSWORD = 'admindb', 'postgres'
APP_ROLE, EXECUTIVE_GROUP = 'datachat_app', 'datachat_executive'
EXECUTIVES = ('user-a', 'user-b', 'user-c')


def ensure_role(conn, name, attributes, password=None):
    exists = conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
    verb = 'ALTER' if exists else 'CREATE'
    statement = sql.SQL(f'{verb} ROLE {{}} WITH {attributes}').format(sql.Identifier(name))
    if password is not None:
        statement += sql.SQL(' PASSWORD {}').format(sql.Literal(password))
    conn.execute(statement)


def ensure_env(values: dict[str, str]):
    current = dotenv_values(ENV_PATH)
    missing = {key: value for key, value in values.items() if key not in current}
    if missing:
        with ENV_PATH.open('a', encoding='utf-8') as env:
            env.write('\n# datachatdb (created by scripts/setup_datachatdb.py)\n')
            env.writelines(f'{key}={value}\n' for key, value in missing.items())


def main():
    env = dotenv_values(ENV_PATH)
    host, port = env.get('PG_HOST') or 'localhost', env.get('PG_PORT') or '5432'
    app_password = env.get('DATACHAT_DB_PASSWORD') or secrets.token_hex(24)
    superuser = dict(host=host, port=port, user=env['PG_USER'], password=env['PG_PWD'], dbname='postgres')

    with psycopg.connect(**superuser, autocommit=True) as conn:
        ensure_role(conn, ADMIN_ROLE, 'LOGIN NOSUPERUSER NOCREATEDB CREATEROLE BYPASSRLS', ADMIN_PASSWORD)
        ensure_role(conn, APP_ROLE, 'LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT', app_password)
        ensure_role(conn, EXECUTIVE_GROUP, 'NOLOGIN NOSUPERUSER NOBYPASSRLS')
        for executive in EXECUTIVES:
            ensure_role(conn, executive, 'NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT')
            conn.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(EXECUTIVE_GROUP), sql.Identifier(executive)))
            # The app may only *become* an executive (SET ROLE); it never inherits their privileges.
            conn.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE, SET TRUE').format(
                sql.Identifier(executive), sql.Identifier(APP_ROLE)))
        # admindb can onboard future executives without the installation superuser.
        for role in (EXECUTIVE_GROUP, APP_ROLE, *EXECUTIVES):
            conn.execute(sql.SQL('GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE').format(
                sql.Identifier(role), sql.Identifier(ADMIN_ROLE)))
        for database in DATABASES:
            if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,)).fetchone():
                conn.execute(sql.SQL('CREATE DATABASE {} OWNER {} ENCODING {}').format(
                    sql.Identifier(database), sql.Identifier(ADMIN_ROLE), sql.Literal('UTF8')))
            conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
            conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(database), sql.Identifier(APP_ROLE)))

    schema = SCHEMA_PATH.read_text(encoding='utf-8')
    for database in DATABASES:
        with psycopg.connect(host=host, port=port, user=ADMIN_ROLE, password=ADMIN_PASSWORD,
                             dbname=database, autocommit=True) as conn:
            conn.execute(schema)

    ensure_env({
        'DATACHAT_DB_HOST': host,
        'DATACHAT_DB_PORT': port,
        'DATACHAT_DB_NAME': DATABASES[0],
        'DATACHAT_TEST_DB_NAME': DATABASES[1],
        'DATACHAT_DB_USER': APP_ROLE,
        'DATACHAT_DB_PASSWORD': app_password,
        'DATACHAT_DB_ADMIN_USER': ADMIN_ROLE,
        'DATACHAT_DB_ADMIN_PASSWORD': ADMIN_PASSWORD,
    })
    print(f'datachatdb ready: databases {", ".join(DATABASES)}; roles {ADMIN_ROLE}, {APP_ROLE}, '
          f'{EXECUTIVE_GROUP}, {", ".join(EXECUTIVES)}.')


if __name__ == '__main__':
    main()
