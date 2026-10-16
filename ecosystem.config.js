module.exports = {
  apps: [
    {
      name: "jippy-store-flask",
      script: "./.venv/bin/gunicorn",
      args: "-w 3 --threads 2 --worker-class gthread -b 0.0.0.0:3001 --keep-alive 5 --timeout 120 app:app",
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
