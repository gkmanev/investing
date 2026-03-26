#!/bin/sh
set -eu

if [ -n "${PGHOST:-}" ]; then
  DB_HOST="${PGHOST}"
  DB_PORT="${PGPORT:-5432}"
  DB_USER="${PGUSER:-postgres}"
  DB_NAME="${PGDATABASE:-postgres}"
elif [ -n "${POSTGRES_HOST:-}" ]; then
  DB_HOST="${POSTGRES_HOST}"
  DB_PORT="${POSTGRES_PORT:-5432}"
  DB_USER="${POSTGRES_USER:-investing}"
  DB_NAME="${POSTGRES_DB:-investing}"
elif [ -n "${DATABASE_URL:-}" ]; then
  DB_HOST="$(python -c 'import os; from urllib.parse import urlparse; print(urlparse(os.environ["DATABASE_URL"]).hostname or "")')"
  DB_PORT="$(python -c 'import os; from urllib.parse import urlparse; print(urlparse(os.environ["DATABASE_URL"]).port or 5432)')"
  DB_USER="$(python -c 'import os; from urllib.parse import urlparse, unquote; print(unquote(urlparse(os.environ["DATABASE_URL"]).username or "postgres"))')"
  DB_NAME="$(python -c 'import os; from urllib.parse import urlparse; print((urlparse(os.environ["DATABASE_URL"]).path or "/postgres").lstrip("/"))')"
else
  DB_HOST="db"
  DB_PORT="5432"
  DB_USER="investing"
  DB_NAME="investing"
fi

echo "Waiting for Postgres at ${DB_HOST}:${DB_PORT}..."
until pg_isready -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; do
  sleep 1
done

echo "Postgres is ready."
