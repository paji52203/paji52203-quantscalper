module.exports = {
  apps: [
    {
      name: "quantscalper",
      script: "start.py",
      interpreter: "/root/quantscalper/.venv/bin/python3",
      cwd: "/root/quantscalper",

      // Restart policy
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 5000,       // wait 5s before restart
      min_uptime: "10s",         // must run 10s to count as stable

      // Logging
      out_file: "/root/quantscalper/logs/out.log",
      error_file: "/root/quantscalper/logs/err.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs: true,

      // Environment variables — set secrets in /root/quantscalper/.env
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: "/root/quantscalper",
      },

      // Load .env file automatically
      env_file: "/root/quantscalper/.env",
    }
  ]
};
