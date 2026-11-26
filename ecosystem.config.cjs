// PM2 process file. Exactly one process: the job queue and browser
// coordination live in memory, so never use cluster mode or more instances.
module.exports = {
  apps: [
    {
      name: "dreamina-proxy",
      cwd: __dirname,
      script: ".venv/bin/uvicorn",
      args: [
        "--factory", "dreamina.app:create_app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--workers", "1",
        // cloudflared connects over loopback; the app reads CF-Connecting-IP itself.
        "--no-proxy-headers",
        "--timeout-graceful-shutdown", "30",
      ].join(" "),
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      kill_timeout: 40000,
      env: {
        PYTHONUNBUFFERED: "1",
        // Point at a production env file outside the checkout if you prefer:
        // DREAMINA_ENV_FILE: "/etc/dreamina/dreamina.env",
      },
    },
  ],
};
