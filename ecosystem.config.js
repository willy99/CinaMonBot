// pm2: процеси ЦінаБот (бот, адмінка) + задачі за розкладом (бекап, перевірка живучості). Керування: ./run.sh
const path = require("path");
const root = __dirname;
const python = path.join(root, "venv", "bin", "python");

const common = {
  cwd: root,
  interpreter: python,
  kill_timeout: 5000,
  autorestart: true,
  max_restarts: 20,
  restart_delay: 3000,
  env: { PYTHONUNBUFFERED: "1" },
  time: true,
};

module.exports = {
  apps: [
    { ...common, name: "cinabot",       script: "bot.py",   out_file: "logs/bot.out.log",   error_file: "logs/bot.err.log" },
    { ...common, name: "cinabot-admin", script: "admin.py", out_file: "logs/admin.out.log", error_file: "logs/admin.err.log" },
    // Задачі за розкладом: pm2 запускає їх за cron (і один раз при старті), між запусками вони «stopped» — це нормально
    { ...common, name: "cinabot-backup", script: "ops.py", args: "backup", cron_restart: "15 3 * * *", autorestart: false,
      out_file: "logs/ops.out.log", error_file: "logs/ops.err.log" },
    { ...common, name: "cinabot-health", script: "ops.py", args: "healthcheck", cron_restart: "*/5 * * * *", autorestart: false,
      out_file: "logs/ops.out.log", error_file: "logs/ops.err.log" },
  ],
};
