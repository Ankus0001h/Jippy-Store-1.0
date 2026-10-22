module.exports = {
  apps: [
    {
      name: "jippy-store-flask",
      script: "./.venv/bin/gunicorn",
      args: "-w 4 --threads 4 --worker-class gthread -b 0.0.0.0:3001 --keep-alive 65 --timeout 120 --backlog 2048 --max-requests 2500 --max-requests-jitter 250 app:app",
      interpreter: "none",
      autorestart: true,
      watch: false,
      env: {
        PORT: 3001,
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
