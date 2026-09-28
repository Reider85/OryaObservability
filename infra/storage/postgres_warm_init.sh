#!/bin/bash
# Grants for the warm tier, run by the postgres entrypoint from
# /docker-entrypoint-initdb.d/init.sh, i.e. immediately after 01-schema.sql.
#
# The schema itself is applied by the 01-schema.sql mount; this script only
# creates the least-privilege application role and grants it access.
#
# Notes on why this looks the way it does:
#   * No `createdb` — POSTGRES_DB already created warm_store.
#   * No `CREATE USER IF NOT EXISTS` — PostgreSQL has no such form, and the
#     entrypoint runs psql with ON_ERROR_STOP=1, so a syntax error here aborts
#     initdb and leaves the warm tier with no schema at all. A DO block is the
#     supported way to make role creation idempotent.
#   * No -h/-p — during initdb the server listens on a unix socket only.
#     -U/-d come from POSTGRES_USER/POSTGRES_DB because the entrypoint does not
#     export PGUSER here, and its default of "postgres" does not exist in this
#     stack (the superuser is named by POSTGRES_USER).

set -e

PSQL=(psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-warm}" -d "${POSTGRES_DB:-warm_store}")

echo "Creating warm_user role (if absent)..."
"${PSQL[@]}" <<'EOSQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'warm_user') THEN
        CREATE ROLE warm_user LOGIN PASSWORD 'warm_password';
    END IF;
END
$$;
EOSQL

echo "Granting privileges on warm_store to warm_user..."
"${PSQL[@]}" <<'EOSQL'
GRANT CONNECT ON DATABASE warm_store TO warm_user;
GRANT USAGE ON SCHEMA public TO warm_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO warm_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO warm_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO warm_user;

-- Keep future tables (e.g. traces_warm added by PC22) reachable as well.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO warm_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO warm_user;
EOSQL

echo "Verifying warm tables..."
"${PSQL[@]}" -c "
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;
"

echo "PostgreSQL warm storage initialization completed successfully"
