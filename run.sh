#!/usr/bin/env bash
# Start, stop, and inspect the support desk. Phoenix stays up when the others stop.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DB_NAME="support"
TOOLBOX_ROLE="toolbox"
RUN_DIR="${ROOT}/.run"
mkdir -p "${RUN_DIR}"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

postgres_ok() {
  pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 \
    && psql -d "${DB_NAME}" -tAc "SELECT 1" 2>/dev/null | grep -q 1
}

toolbox_ok() {
  curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:5000/api/toolset" \
    || curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:5000/"
}

judge_ok() {
  curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:10002/.well-known/agent.json"
}

masker_ok() {
  curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:10003/.well-known/agent.json"
}

phoenix_ok() {
  curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:6006/v1/projects"
}

web_ok() {
  local body
  body="$(curl -sf --max-time 3 "http://127.0.0.1:8000/health" || true)"
  [[ "${body}" == *'"status":"ok"'* || "${body}" == *'"status": "ok"'* ]]
}

wait_until() {
  local name="$1"
  local tries="$2"
  shift 2
  local i
  for ((i = 1; i <= tries; i++)); do
    if "$@"; then
      echo "${name}: up"
      return 0
    fi
    sleep 1
  done
  echo "${name}: did not become ready" >&2
  return 1
}

ensure() {
  local name="$1"
  local check="$2"
  shift 2
  if "${check}"; then
    echo "${name}: already up"
    return 0
  fi
  echo "starting ${name}"
  "$@"
  wait_until "${name}" 60 "${check}"
}

start_postgres() {
  if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo "postgres is up, but database ${DB_NAME} is missing. Run ./run.sh reset" >&2
    return 1
  fi
  brew services start postgresql@16
}

start_toolbox() {
  if [[ -z "${TOOLBOX_DB_PASSWORD:-}" ]]; then
    echo "TOOLBOX_DB_PASSWORD is not set. Put it in .env" >&2
    return 1
  fi
  nohup npx --yes @toolbox-sdk/server \
    --config "${ROOT}/mcp_toolbox/tools.yaml" \
    --enable-api \
    --address 127.0.0.1 \
    --port 5000 \
    >>"${RUN_DIR}/toolbox.log" 2>&1 &
  echo $! >"${RUN_DIR}/toolbox.pid"
}

start_phoenix() {
  export PHOENIX_HOST=127.0.0.1
  export PHOENIX_PORT=6006
  export PHOENIX_WORKING_DIR="${HOME}/.phoenix"
  nohup "${ROOT}/.venv/bin/phoenix" serve --host 127.0.0.1 --port 6006 \
    >>"${RUN_DIR}/phoenix.log" 2>&1 &
  echo $! >"${RUN_DIR}/phoenix.pid"
}

start_judge() {
  nohup "${ROOT}/.venv/bin/python" -m guards.judge \
    >>"${RUN_DIR}/judge.log" 2>&1 &
  echo $! >"${RUN_DIR}/judge.pid"
}

start_masker() {
  nohup "${ROOT}/.venv/bin/python" -m guards.masker \
    >>"${RUN_DIR}/masker.log" 2>&1 &
  echo $! >"${RUN_DIR}/masker.pid"
}

start_web() {
  nohup "${ROOT}/.venv/bin/python" -m support.web \
    >>"${RUN_DIR}/web.log" 2>&1 &
  echo $! >"${RUN_DIR}/web.pid"
}

cmd_status() {
  local failed=0
  local name check
  for pair in \
    "postgres postgres_ok" \
    "toolbox toolbox_ok" \
    "judge judge_ok" \
    "masker masker_ok" \
    "phoenix phoenix_ok" \
    "web web_ok"
  do
    name="${pair%% *}"
    check="${pair##* }"
    if "${check}"; then
      echo "${name}: up"
    else
      echo "${name}: down"
      failed=1
    fi
  done
  return "${failed}"
}

cmd_start() {
  # Phoenix is before the Judge because the Judge refuses to boot until Phoenix answers.
  echo "start order: postgres, toolbox, phoenix, judge, masker, web"
  ensure postgres postgres_ok start_postgres
  ensure toolbox toolbox_ok start_toolbox
  ensure phoenix phoenix_ok start_phoenix
  ensure judge judge_ok start_judge
  ensure masker masker_ok start_masker
  ensure web web_ok start_web
}

stop_port() {
  local name="$1"
  local port="$2"
  local pids
  pids="$(lsof -nP -t -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    echo "${name}: already stopped"
    return 0
  fi
  # shellcheck disable=SC2086
  kill ${pids} 2>/dev/null || true
  echo "${name}: stopped"
}

cmd_stop() {
  stop_port web 8000
  stop_port judge 10002
  stop_port masker 10003
  stop_port toolbox 5000
  echo "phoenix: left running"
  echo "postgres: left running"
}

cmd_reset() {
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
}

usage() {
  echo "usage: ./run.sh start | ./run.sh status | ./run.sh stop | ./run.sh reset | ./run.sh phoenix | ./run.sh judge | ./run.sh masker | ./run.sh web | ./run.sh cli" >&2
  exit 1
}

case "${1:-}" in
  start) cmd_start ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  reset) cmd_reset ;;
  phoenix)
    export PHOENIX_HOST=127.0.0.1
    export PHOENIX_PORT=6006
    export PHOENIX_WORKING_DIR="${HOME}/.phoenix"
    exec "${ROOT}/.venv/bin/phoenix" serve --host 127.0.0.1 --port 6006
    ;;
  judge) exec "${ROOT}/.venv/bin/python" -m guards.judge ;;
  masker) exec "${ROOT}/.venv/bin/python" -m guards.masker ;;
  web) exec "${ROOT}/.venv/bin/python" -m support.web ;;
  cli)
    shift
    exec "${ROOT}/.venv/bin/python" -m support.cli "$@"
    ;;
  *) usage ;;
esac
