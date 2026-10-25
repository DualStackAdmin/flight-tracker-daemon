#!/usr/bin/env bash
# Installer for Ubuntu 22.04/24.04. Installs the app IN PLACE (wherever you cloned it).
# Usage (from the project directory):  sudo bash deploy/install.sh
# Optional: run the service as a specific user:  sudo APP_USER=myuser bash deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Please run with sudo or as root"; exit 1; fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"          # e.g. /home/ubuntu/flight-tracker-daemon
APP_USER="${APP_USER:-$(stat -c %U "$APP_DIR")}"     # owner of the directory
[[ "$APP_USER" == "root" ]] && APP_USER=ubuntu
id -u "$APP_USER" &>/dev/null || { echo "User not found: $APP_USER"; exit 1; }

echo "==> Directory: $APP_DIR   User: $APP_USER"

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

mkdir -p "$APP_DIR/data"
[[ -f "$APP_DIR/config.yaml" ]] || cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
[[ -f "$APP_DIR/.env" ]] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

echo "==> Virtual environment"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> systemd service"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#$APP_USER#g" \
    "$APP_DIR/deploy/flight-tracker.service" > /etc/systemd/system/flight-tracker.service
systemctl daemon-reload
systemctl enable flight-tracker >/dev/null

if grep -qE '^(TRAVELPAYOUTS_TOKEN|TELEGRAM_BOT_TOKEN)=\s*$' "$APP_DIR/.env"; then
  echo
  echo "!! Tokens are still empty:"
  echo "   nano $APP_DIR/.env          # add your tokens"
  echo "   nano $APP_DIR/config.yaml   # pick your routes"
  echo "   sudo systemctl start flight-tracker"
else
  systemctl restart flight-tracker
  echo "==> Started. Logs: journalctl -u flight-tracker -f"
fi
