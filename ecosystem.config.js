// PM2 process definition for the PII Redaction Pipeline production server.
// Start with:  pm2 start ecosystem.config.js
// See wiki/deployment.md and wiki/feature-updates/phase-1/plan-pm2-implementation.md.
module.exports = {
  apps: [
    {
      name: "pii-redaction",
      script: "serve.py",
      interpreter: "./pii_env/Scripts/python.exe",
      cwd: __dirname,
      watch: false, // MUST stay false — this app writes/rewrites files as part of
      // normal operation (uploads, temp artifacts, redacted output) while a run is
      // in progress. If PM2 watched the working tree, that routine file activity
      // would trigger a restart mid-run and kill an in-progress redaction partway
      // through a client's document.
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
      instances: 1, // NOT cluster mode — this app binds a single fixed port and
      // does file-path-based temp/output handling plus per-run MySQL writes, so
      // a second concurrent instance would race the first on both.
      out_file: "./logs/pm2-out.log",
      error_file: "./logs/pm2-error.log",
      time: true,
    },
  ],
};
