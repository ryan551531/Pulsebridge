#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="pulsebridge"
APP_DIR="/opt/${APP_NAME}"
SERVICE_USER="pulsebridge"
ENV_FILE="/etc/pulsebridge.env"
SERVICE_FILE="/etc/systemd/system/pulsebridge.service"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash install-lxc.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer supports Debian or Ubuntu LXC containers using apt."
  exit 1
fi

echo "Installing PulseBridge in ${APP_DIR}..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip rsync iputils-ping

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}"
if [[ "$(realpath "${SOURCE_DIR}")" != "$(realpath "${APP_DIR}")" ]]; then
  rsync -a \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude 'logs/' \
    --exclude '*.pyc' \
    "${SOURCE_DIR}/" "${APP_DIR}/"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}/logs"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements-lxc.txt"

if [[ -z "${DASHBOARD_PASSWORD:-}" ]]; then
  while true; do
    read -r -s -p "Create a dashboard password (minimum 12 characters; no spaces): " DASHBOARD_PASSWORD
    echo
    if [[ "${#DASHBOARD_PASSWORD}" -ge 12 && "${DASHBOARD_PASSWORD}" =~ ^[A-Za-z0-9._@%+=,:-]+$ ]]; then
      break
    fi
    echo "Use at least 12 characters from letters, numbers, and ._@%+=,:-"
  done
fi

if [[ "${#DASHBOARD_PASSWORD}" -lt 12 || ! "${DASHBOARD_PASSWORD}" =~ ^[A-Za-z0-9._@%+=,:-]+$ ]]; then
  echo "DASHBOARD_PASSWORD must be at least 12 characters and contain no spaces."
  exit 1
fi

DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-admin}"
if [[ ! "${DASHBOARD_USERNAME}" =~ ^[A-Za-z0-9._-]{3,50}$ ]]; then
  echo "DASHBOARD_USERNAME must contain 3-50 letters, numbers, dots, dashes, or underscores."
  exit 1
fi

(
cd "${APP_DIR}"
PULSEBRIDGE_BOOTSTRAP_USERNAME="${DASHBOARD_USERNAME}" \
PULSEBRIDGE_BOOTSTRAP_PASSWORD="${DASHBOARD_PASSWORD}" \
"${APP_DIR}/.venv/bin/python" - <<'PY'
import os
from auth_store import initialize, list_users, save_user

initialize()
if not list_users():
    save_user({
        "username": os.environ["PULSEBRIDGE_BOOTSTRAP_USERNAME"],
        "password": os.environ["PULSEBRIDGE_BOOTSTRAP_PASSWORD"],
        "role": "admin",
        "active": True,
    })
PY
)
unset DASHBOARD_PASSWORD

umask 077
printf 'DASHBOARD_HOST=0.0.0.0\nDASHBOARD_PORT=8088\n' > "${ENV_FILE}"
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=PulseBridge ERPNext Biometric Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:8088 app:app
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
systemctl daemon-reload
systemctl enable --now pulsebridge.service

sleep 2
if ! systemctl is-active --quiet pulsebridge.service; then
  echo "PulseBridge did not start. Recent service output:"
  journalctl -u pulsebridge.service -n 30 --no-pager
  exit 1
fi

LXC_IP="$(hostname -I | awk '{print $1}')"
echo
echo "PulseBridge is running."
echo "Open: http://${LXC_IP}:8088"
echo "Username: ${DASHBOARD_USERNAME}"
echo "Password: the dashboard password you just created"
echo
echo "Service commands:"
echo "  systemctl status pulsebridge"
echo "  systemctl restart pulsebridge"
echo "  journalctl -u pulsebridge -f"
