module.exports = {
  apps: [
    {
      name: "jippy-store-flask",
      script: "gunicorn",
      args: "-w 3 -b 0.0.0.0:3001 app:app --timeout 120",
      interpreter: "none",
      autorestart: true,
      watch: false,
      env: {
        PORT: 3001,
        NODE_ENV: "production"
      }
    }
  ]
};
