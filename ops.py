"""
Експлуатаційні задачі ЦінаБот (pm2 запускає за cron, див. ecosystem.config.js):

    python ops.py backup         # бекап бази + ротація + копія на BACKUP_REMOTE
    python ops.py healthcheck    # живучість бота/API → пінг моніторингу, алерт адміну
"""
import sys

from botkit.logs import setup_logging

if __name__ == "__main__":
    setup_logging()
    from app.database.session import close_engine          # також налаштовує контекст botkit
    from botkit.ops import backup, healthcheck

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backup":
        sys.exit(backup.run())
    if cmd == "healthcheck":
        sys.exit(healthcheck.run(close_engine))
    print(__doc__)
    sys.exit(2)
