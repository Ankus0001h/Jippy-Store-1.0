#!/usr/bin/env bash
# ========================================================
# 🚀 JIPPY STORE - AWS EC2 FLASK APP SETUP SCRIPT (PORT 3001)
# ========================================================

set -e

echo "=== [1/4] Updating system packages ==="
sudo apt-get update -y
sudo apt-get upgrade -y

echo "=== [2/4] Installing Python 3, Pip, Venv & Build Tools ==="
sudo apt-get install -y python3 python3-pip python3-venv build-essential curl git

echo "=== [3/4] Installing Node.js & PM2 (Process Manager) ==="
if ! command -v node &> /dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

sudo npm install -g pm2

echo "=== [4/4] Setting up Python Virtual Environment & Dependencies ==="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "========================================================"
echo "✅ Setup Completed Successfully!"
echo ""
echo "To start Flask App on Port 3001 with PM2:"
echo "   pm2 start ecosystem.config.js"
echo "   pm2 save"
echo "   pm2 startup"
echo ""
echo "To test in development mode directly:"
echo "   source .venv/bin/activate"
echo "   python app.py"
echo "========================================================"
