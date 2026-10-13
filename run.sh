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
#   ./run.sh migrate    довести схему бази до актуальної (start і dev роблять це самі)
#   ./run.sh backup     бекап бази зараз (за розкладом — щодня 03:15, pm2 cinabot-backup)
#   ./run.sh logrotate  один раз: ротація логів pm2 (pm2-logrotate, 10 МБ × 14)
#   ./run.sh audit      відомі CVE у залежностях (pip-audit по requirements.txt)
#   ./run.sh lock       перегенерувати requirements.txt (з хешами) з requirements.in
#   ./run.sh botkit-pull [гілка]  оновити спільний пакет botkit/ з GitHub willy99/botkit (git subtree)
set -euo pipefail
cd "$(dirname "$0")"

PY=./venv/bin/python
APPS="cinabot cinabot-admin cinabot-backup cinabot-health"

check() {
  [[ -x "$PY" ]] || { echo "Немає venv: python3.14 -m venv venv && venv/bin/pip install --require-hashes -r requirements.txt"; exit 1; }
  [[ -f .env ]]   || { echo "Немає .env: cp .env.example .env і заповнити BOT_TOKEN"; exit 1; }
  grep -q '^ADMIN_STORAGE_SECRET=.\+' .env || echo "⚠️  ADMIN_STORAGE_SECRET порожній у .env — сесії адмінки злетять після рестарту"
  mkdir -p logs
}

case "${1:-start}" in
  start|restart)
    check
    command -v pm2 >/dev/null || { echo "Немає pm2: npm i -g pm2"; exit 1; }
    "$PY" -m app.database.migrate          # один раз, до старту процесів (вони лише перевіряють схему)
    pm2 startOrRestart ecosystem.config.js --update-env
    pm2 save >/dev/null
    pm2 describe pm2-logrotate >/dev/null 2>&1 || echo "⚠️  Логи pm2 не ротуються: ./run.sh logrotate (один раз)"
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
    "$PY" -m app.database.migrate
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
  migrate)
    check
    "$PY" -m app.database.migrate
    ;;
  backup)
    check
    "$PY" ops.py backup
    ;;
  logrotate)
    command -v pm2 >/dev/null || { echo "Немає pm2"; exit 1; }
    pm2 install pm2-logrotate
    pm2 set pm2-logrotate:max_size 10M
    pm2 set pm2-logrotate:retain 14
    pm2 set pm2-logrotate:compress true
    ;;
  audit)
    [[ -x "$PY" ]] || { echo "Немає venv"; exit 1; }
    ./venv/bin/pip-audit -r requirements.txt --require-hashes --disable-pip
    ;;
  botkit-pull)
    # Спільний пакет (див. botkit/README.md). Робоче дерево має бути закомічене.
    git diff --quiet && git diff --cached --quiet || { echo "Спершу закоміть зміни (git subtree вимагає чисте дерево)"; exit 1; }
    # Звідки: GitHub за замовчуванням; BOTKIT_REPO=~/work/botkit ./run.sh botkit-pull — локальна копія до пушу
    repo="${BOTKIT_REPO:-https://github.com/willy99/botkit.git}"
    git subtree pull --prefix=botkit "$repo" "${2:-main}" --squash -m "botkit: оновити з $repo (${2:-main})"
    echo "Далі: ./run.sh test"
    ;;
  lock)
    command -v uv >/dev/null || { echo "Немає uv: brew install uv"; exit 1; }
    uv pip compile requirements.in --generate-hashes --python-version 3.14 -o requirements.txt
    echo "Далі: venv/bin/pip install --require-hashes -r requirements.txt && ./run.sh test && ./run.sh audit"
    ;;
  *)
    sed -n '2,17p' "$0"; exit 1 ;;
esac
