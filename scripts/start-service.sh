#!/bin/sh
set -eu

infer_role() {
  service_name="$(printf '%s' "${RAILWAY_SERVICE_NAME:-}" | tr '[:upper:]' '[:lower:]')"
  case "${service_name}" in
    *beat*)
      printf '%s\n' "beat"
      ;;
    *worker*)
      printf '%s\n' "worker"
      ;;
    *)
      printf '%s\n' "web"
      ;;
  esac
}

wait_for_table() {
  table_name="$1"
  echo "Waiting for table ${table_name}..."
  until python manage.py shell -c "from django.db import connection; tables = connection.introspection.table_names(); raise SystemExit(0 if \"${table_name}\" in tables else 1)" >/dev/null 2>&1; do
    sleep 2
  done
  echo "Table ${table_name} is ready."
}

SERVICE_ROLE="${SERVICE_ROLE:-$(infer_role)}"
CELERY_LOGLEVEL="${CELERY_LOGLEVEL:-info}"

echo "Starting service role: ${SERVICE_ROLE}"
sh ./scripts/wait-for-db.sh

case "${SERVICE_ROLE}" in
  web)
    python manage.py migrate --noinput
    python manage.py sync_daily_brief_schedule
    python manage.py sync_trading_jobs_schedule
    exec gunicorn investing_project.wsgi:application --bind "0.0.0.0:${PORT:-8080}" --access-logfile - --error-logfile -
    ;;
  worker)
    exec celery -A investing_project worker --loglevel="${CELERY_LOGLEVEL}" --concurrency="${CELERY_WORKER_CONCURRENCY:-2}"
    ;;
  beat)
    if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
      python manage.py migrate --noinput
    else
      wait_for_table "django_celery_beat_periodictask"
    fi
    python manage.py sync_daily_brief_schedule
    python manage.py sync_trading_jobs_schedule
    exec celery -A investing_project beat --loglevel="${CELERY_LOGLEVEL}" --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  *)
    echo "Unknown SERVICE_ROLE: ${SERVICE_ROLE}" >&2
    exit 1
    ;;
esac
