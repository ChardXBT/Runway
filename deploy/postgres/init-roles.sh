#!/usr/bin/env bash
# Runs once, when the Postgres data directory is first initialised
# (/docker-entrypoint-initdb.d). Creates two login roles (spec 01 section 5):
#
#   RUNWAY_DB_OWNER  owns the schema; `runway db upgrade` connects as it.
#   RUNWAY_DB_APP    the API and worker; may read and write rows, cannot alter
#                    the schema, and has NOBYPASSRLS so row-level security
#                    policies always apply to it.
#
# Passwords come from the container environment (the host's secret store);
# they are passed to psql as variables, never interpolated into SQL text.
set -euo pipefail

: "${RUNWAY_DB_OWNER:?}" "${RUNWAY_DB_OWNER_PASSWORD:?}"
: "${RUNWAY_DB_APP:?}" "${RUNWAY_DB_APP_PASSWORD:?}"
: "${POSTGRES_DB:?}" "${POSTGRES_USER:?}"

psql --no-psqlrc --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set owner="$RUNWAY_DB_OWNER" --set owner_password="$RUNWAY_DB_OWNER_PASSWORD" \
    --set app="$RUNWAY_DB_APP" --set app_password="$RUNWAY_DB_APP_PASSWORD" \
    --set db="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"owner" LOGIN PASSWORD :'owner_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"app" LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"owner", :"app";

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO :"owner";
GRANT USAGE ON SCHEMA public TO :"app";

-- Tables and sequences the owner creates (migrations, or a restore run as the
-- owner) are usable by the app role without further grants.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app";
SQL
