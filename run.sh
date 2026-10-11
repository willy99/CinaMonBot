#!/usr/bin/env bash
# Керування ЦінаБот: бот (bot.py) + веб-адмінка (admin.py) через pm2.
#
#   ./run.sh            запустити або перезапустити обидва
#   ./run.sh stop       зупинити обидва
#   ./run.sh status     стан процесів
#   ./run.sh logs       хвіст логів обох (Ctrl+C щоб вийти)
#   ./run.sh dev        обидва у цьому терміналі без pm2 (Ctrl+C зупиняє обох)
#   ./run.sh admin-user логін [telegram_id]   створити адміна дашборду
#   ./run.sh test [cov]    прогнати всі тести (cov — з покриттям); перед кожним деплоєм!
set -euo pipefail
cd "$(dirname "$0")"

PY=./venv/bin/python
APPS="cinabot cinabot-admin"

check() {
  [[ -x "$PY" ]] || { echo "Немає venv: python3 -m venv venv && venv/bin/pip install -r requirements.txt"; exit 1; }
  [[ -f .env ]]   || { echo "Немає .env: cp .env.example .env і заповнити BOT_TOKEN"; exit 1; }
  grep -q '^ADMIN_STORAGE_SECRET=.\+' .env || echo "⚠️  ADMIN_STORAGE_SECRET порожній у .env — сесії адмінки злетять після рестарту"
  mkdir -p logs
}

case "${1:-start}" in
  start|restart)
    check
    command -v pm2 >/dev/null || { echo "Немає pm2: npm i -g pm2"; exit 1; }
    pm2 startOrRestart ecosystem.config.js --update-env
    pm2 save >/dev/null
    pm2 status $APPS
    port=$(grep -E '^ADMIN_PORT=' .env | cut -d= -f2 | tr -d ' ' || true)
    echo "Адмінка: http://127.0.0.1:${port:-8088}"
    ;;
  stop)
    pm2 stop $APPS
    ;;
  status)
    pm2 status $APPS
    ;;
  logs)
    pm2 logs $APPS --lines 50
    ;;
  dev)
    check
    trap 'kill 0' EXIT INT TERM
    "$PY" bot.py   2>&1 | sed -u 's/^/[bot]   /' &
    "$PY" admin.py 2>&1 | sed -u 's/^/[admin] /' &
    wait
    ;;
  admin-user)
    check
    shift
    [[ $# -ge 1 ]] || { echo "Використання: ./run.sh admin-user <логін> [telegram_id]"; exit 1; }
    if [[ $# -ge 2 ]]; then "$PY" -m app.admin.cli create-admin "$1" --telegram-id "$2"; else "$PY" -m app.admin.cli create-admin "$1"; fi
    ;;
  test)
    [[ -x "$PY" ]] || { echo "Немає venv"; exit 1; }
    if [[ "${2:-}" == "cov" ]]; then
      "$PY" -m pytest -q --cov=app --cov=bot --cov=admin --cov-report=term-missing:skip-covered
    else
      "$PY" -m pytest -q
    fi
    ;;
  *)
    sed -n '2,11p' "$0"; exit 1 ;;
esac
