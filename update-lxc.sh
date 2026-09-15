#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/pulsebridge"
SERVICE_USER="pulsebridge"
REPOSITORY_ARCHIVE="https://github.com/ryan551531/Pulsebridge/archive/refs/heads/main.tar.gz"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this updater as root inside the PulseBridge LXC."
  exit 1
fi

if [[ ! -d "${APP_DIR}" || ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  echo "PulseBridge is not installed in ${APP_DIR}. Use the Proxmox installer for a fresh installation."
  exit 1
fi

for command in curl tar rsync; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates tar rsync
    break
  fi
done

UPDATE_DIR="$(mktemp -d /tmp/pulsebridge-update.XXXXXX)"
cleanup() {
  if [[ -n "${UPDATE_DIR:-}" && "${UPDATE_DIR}" == /tmp/pulsebridge-update.* ]]; then
    rm -rf -- "${UPDATE_DIR}"
  fi
}
trap cleanup EXIT

echo "Downloading the latest PulseBridge main branch..."
curl -fsSL "${REPOSITORY_ARCHIVE}" -o "${UPDATE_DIR}/pulsebridge.tar.gz"
tar -xzf "${UPDATE_DIR}/pulsebridge.tar.gz" -C "${UPDATE_DIR}"
SOURCE_DIR="$(find "${UPDATE_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'Pulsebridge-*' -print -quit)"
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/app.py" ]]; then
  echo "The downloaded PulseBridge archive is invalid. Existing installation was not changed."
  exit 1
fi

echo "Stopping PulseBridge and installing the update..."
systemctl stop pulsebridge.service
rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude 'logs/' \
  --exclude 'local_config.py' \
  --exclude '.continuous-sync-enabled' \
  "${SOURCE_DIR}/" "${APP_DIR}/"

"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements-lxc.txt"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}/data" "${APP_DIR}/logs"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
chmod 755 "${APP_DIR}"
systemctl restart pulsebridge.service
sleep 3

if ! systemctl is-active --quiet pulsebridge.service; then
  echo "PulseBridge did not restart. Recent service output:"
  journalctl -u pulsebridge.service -n 40 --no-pager
  exit 1
fi

echo "PulseBridge update completed successfully."
echo "Configuration, accounts, logs, and continuous-sync settings were preserved."
