#!/usr/bin/env bash
# Stage 1: drop and recreate the shop database so order ids stay in seed order.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DB_NAME="support"
TOOLBOX_ROLE="toolbox"

if [[ "${1:-}" == "phoenix" ]]; then
  export PHOENIX_HOST=127.0.0.1
  export PHOENIX_PORT=6006
  export PHOENIX_WORKING_DIR="${HOME}/.phoenix"
  exec "${ROOT}/.venv/bin/phoenix" serve --host 127.0.0.1 --port 6006
fi

if [[ "${1:-}" == "judge" ]]; then
  exec "${ROOT}/.venv/bin/python" -m guards.judge
fi

if [[ "${1:-}" == "masker" ]]; then
  exec "${ROOT}/.venv/bin/python" -m guards.masker
fi

if [[ "${1:-}" != "reset" ]]; then
  echo "usage: ./run.sh reset | ./run.sh phoenix | ./run.sh judge | ./run.sh masker" >&2
  exit 1
fi

if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_NAME}"
fi

psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${TOOLBOX_ROLE}') THEN
    CREATE ROLE ${TOOLBOX_ROLE} LOGIN;
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${DB_NAME} TO ${TOOLBOX_ROLE};
SQL

psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS actions_log, customer_orders, users CASCADE;"
psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 -f "${ROOT}/db/seed.sql"
psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 <<SQL
GRANT USAGE ON SCHEMA public TO ${TOOLBOX_ROLE};
GRANT SELECT ON users, customer_orders TO ${TOOLBOX_ROLE};
GRANT SELECT, INSERT ON actions_log TO ${TOOLBOX_ROLE};
GRANT USAGE, SELECT ON SEQUENCE actions_log_id_seq TO ${TOOLBOX_ROLE};
SQL

echo "reset ${DB_NAME}: tables recreated from db/seed.sql"
