"""
Експлуатаційні задачі ЦінаБот (pm2 запускає за cron, див. ecosystem.config.js):

    python ops.py backup         # бекап бази + ротація + копія на BACKUP_REMOTE
    python ops.py healthcheck    # живучість бота/API → пінг моніторингу, алерт адміну
"""
import sys

from app.logs import setup_logging

if __name__ == "__main__":
    setup_logging()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backup":
        from app.ops.backup import run
    elif cmd == "healthcheck":
        from app.ops.healthcheck import run
    else:
        print(__doc__)
        sys.exit(2)
    sys.exit(run())
