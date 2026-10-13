#!/bin/sh
# Idempotently provision the pr-batch role and database on shared-infra.
# Runs with the shared PostgreSQL admin account; touches no other database.
set -eu

psql -v ON_ERROR_STOP=1 \
    -v role="$BATCH_DB_USER" \
    -v password="$BATCH_DB_PASSWORD" \
    -v db="$BATCH_DB_NAME" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'role')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role')\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'role', :'password')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')\gexec
SQL
