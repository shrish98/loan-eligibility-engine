#!/usr/bin/env bash
# Saves the RDS endpoint into .env and creates the schema (only if it isn't there yet).
# psql runs inside the postgres Docker image, so nothing needs installing locally.
source "$(dirname "$0")/lib.sh"

host="$(stack_output DatabaseEndpoint)"
[ -n "$host" ] && [ "$host" != "None" ] || { echo "Stack has no DatabaseEndpoint yet; deploy first." >&2; exit 1; }
set_env DB_HOST "$host"
set_env DB_SSL require

psql_rds() {
  docker run --rm -i -e PGPASSWORD="$DB_PASSWORD" postgres:16-alpine \
    psql "host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER sslmode=require" "$@"
}

if [ "$(psql_rds -tAc "SELECT to_regclass('public.upload_batches') IS NOT NULL")" = "t" ]; then
  echo "Schema already present on $DB_HOST"
else
  psql_rds -v ON_ERROR_STOP=1 -q -f - < "$REPO_ROOT/db/01_schema.sql"
  echo "Schema created on $DB_HOST"
fi
