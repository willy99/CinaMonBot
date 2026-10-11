// pm2: обидва процеси ЦінаБот. Керування через ./run.sh
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
  ],
};
