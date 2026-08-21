#!/usr/bin/env bash
set -Eeuo pipefail

# PulseBridge Proxmox VE host installer. Run on the Proxmox host, not in an LXC.

APP_NAME="PulseBridge"
TEMP_DIR=""
GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; RESET='\033[0m'

info() { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }
die() { printf "${RED}✗ %s${RESET}\n" "$*" >&2; exit 1; }
cleanup() { [[ -z "${TEMP_DIR}" || ! -d "${TEMP_DIR}" ]] || rm -rf -- "${TEMP_DIR}"; }
trap cleanup EXIT

prompt_default() {
  local label="$1" default="$2" value
  read -r -p "${label} [${default}]: " value
  printf '%s' "${value:-${default}}"
}

require_number() {
  local label="$1" value="$2" minimum="$3"
  [[ "${value}" =~ ^[0-9]+$ ]] || die "${label} must be a whole number."
  (( value >= minimum )) || die "${label} must be at least ${minimum}."
}

[[ "${EUID}" -eq 0 ]] || die "Run this as root in the Proxmox VE host shell."
for command in pct pveam pvesh pvesm; do
  command -v "${command}" >/dev/null 2>&1 || die "${command} was not found. Run this on a Proxmox VE host."
done

clear
printf '%b\n' "${GREEN}PulseBridge · Proxmox LXC Installer${RESET}"
printf '%s\n\n' "Creates a private Debian LXC and installs the ERPNext biometric bridge."

DEFAULT_CTID="$(pvesh get /cluster/nextid 2>/dev/null || true)"
[[ "${DEFAULT_CTID}" =~ ^[0-9]+$ ]] || DEFAULT_CTID="200"

CTID="$(prompt_default "Container ID" "${DEFAULT_CTID}")"
HOSTNAME="$(prompt_default "Hostname" "pulsebridge")"
CORES="$(prompt_default "CPU cores" "2")"
MEMORY="$(prompt_default "Memory in MB" "2048")"
DISK_GB="$(prompt_default "Disk size in GB" "12")"
BRIDGE="$(prompt_default "Network bridge" "vmbr0")"
TEMPLATE_STORAGE="$(prompt_default "Template storage" "local")"
ROOTFS_STORAGE="$(prompt_default "Container storage" "local-lvm")"

require_number "Container ID" "${CTID}" 100
require_number "CPU cores" "${CORES}" 1
require_number "Memory" "${MEMORY}" 512
require_number "Disk size" "${DISK_GB}" 4
[[ "${HOSTNAME}" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] || die "Hostname contains unsupported characters."
pct status "${CTID}" >/dev/null 2>&1 && die "Container ${CTID} already exists."

STORAGES="$(pvesm status 2>/dev/null | awk 'NR > 1 {print $1}')"
grep -Fxq "${TEMPLATE_STORAGE}" <<<"${STORAGES}" || die "Template storage '${TEMPLATE_STORAGE}' does not exist."
grep -Fxq "${ROOTFS_STORAGE}" <<<"${STORAGES}" || die "Container storage '${ROOTFS_STORAGE}' does not exist."

read -r -p "Use DHCP for the LXC network? [Y/n]: " USE_DHCP
if [[ "${USE_DHCP:-Y}" =~ ^[Nn]$ ]]; then
  read -r -p "Static address with CIDR (example 192.168.0.40/24): " STATIC_IP
  read -r -p "Gateway (example 192.168.0.1): " GATEWAY
  [[ "${STATIC_IP}" == */* ]] || die "Enter the static address with its CIDR prefix."
  [[ -n "${GATEWAY}" ]] || die "A gateway is required."
  NET0="name=eth0,bridge=${BRIDGE},ip=${STATIC_IP},gw=${GATEWAY}"
else
  NET0="name=eth0,bridge=${BRIDGE},ip=dhcp"
fi

while true; do
  read -r -s -p "Create the PulseBridge administrator password (12+ characters): " DASHBOARD_PASSWORD; echo
  read -r -s -p "Confirm the administrator password: " PASSWORD_CONFIRM; echo
  if [[ "${#DASHBOARD_PASSWORD}" -ge 12 && "${DASHBOARD_PASSWORD}" == "${PASSWORD_CONFIRM}" && "${DASHBOARD_PASSWORD}" =~ ^[A-Za-z0-9._@%+=,:-]+$ ]]; then break; fi
  warn "Passwords must match and use 12+ characters without spaces."
done

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [[ ! -f "${SOURCE_DIR}/app.py" ]]; then
  REPO_URL="${PULSEBRIDGE_REPO_URL:-https://github.com/ryan551531/Pulsebridge.git}"
  [[ "${REPO_URL}" =~ ^https:// ]] || die "Use an HTTPS repository URL."
  REPO_TOKEN="${PULSEBRIDGE_REPO_TOKEN:-}"

  if ! command -v git >/dev/null 2>&1; then
    info "Installing Git on the Proxmox host"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates
  fi
  TEMP_DIR="$(mktemp -d -t pulsebridge-pve-XXXXXX)"
  info "Downloading the PulseBridge repository"
  if [[ -n "${REPO_TOKEN}" ]]; then
    REPO_USERNAME="${PULSEBRIDGE_REPO_USERNAME:-x-access-token}"
    TOKEN_FILE="${TEMP_DIR}/repo-token"; ASKPASS_FILE="${TEMP_DIR}/askpass.sh"
    printf '%s' "${REPO_TOKEN}" > "${TOKEN_FILE}"; chmod 600 "${TOKEN_FILE}"
    cat > "${ASKPASS_FILE}" <<EOF
#!/usr/bin/env bash
case "\$1" in
  *Username*) printf '%s' '${REPO_USERNAME}' ;;
  *) cat '${TOKEN_FILE}' ;;
esac
EOF
    chmod 700 "${ASKPASS_FILE}"
    GIT_ASKPASS="${ASKPASS_FILE}" GIT_TERMINAL_PROMPT=0 git clone --depth 1 "${REPO_URL}" "${TEMP_DIR}/source"
  else
    GIT_TERMINAL_PROMPT=0 git clone --depth 1 "${REPO_URL}" "${TEMP_DIR}/source"
  fi
  SOURCE_DIR="${TEMP_DIR}/source"
  unset REPO_TOKEN PULSEBRIDGE_REPO_TOKEN
fi
[[ -f "${SOURCE_DIR}/app.py" && -f "${SOURCE_DIR}/install-lxc.sh" ]] || die "The repository is not a complete PulseBridge installation."

info "Updating the Proxmox template catalogue"
pveam update >/dev/null
TEMPLATE_NAME="$(pveam available --section system | awk '/debian-12-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' | sort -V | tail -n 1)"
[[ -n "${TEMPLATE_NAME}" ]] || die "No Debian 12 LXC template was found."
TEMPLATE_REF="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_NAME}"
if ! pveam list "${TEMPLATE_STORAGE}" 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "${TEMPLATE_REF}"; then
  info "Downloading ${TEMPLATE_NAME}"
  pveam download "${TEMPLATE_STORAGE}" "${TEMPLATE_NAME}"
else
  info "Using the downloaded Debian template"
fi

info "Creating unprivileged LXC ${CTID}"
pct create "${CTID}" "${TEMPLATE_REF}" \
  --hostname "${HOSTNAME}" --ostype debian --arch amd64 \
  --cores "${CORES}" --memory "${MEMORY}" --swap 512 \
  --rootfs "${ROOTFS_STORAGE}:${DISK_GB}" --net0 "${NET0}" \
  --unprivileged 1 --onboot 1 --start 1 --tags pulsebridge --timezone host

info "Waiting for the LXC network"
NETWORK_READY=0
for _ in $(seq 1 60); do
  if pct exec "${CTID}" --keep-env 0 -- bash -lc 'getent hosts deb.debian.org >/dev/null 2>&1'; then NETWORK_READY=1; break; fi
  sleep 2
done
(( NETWORK_READY == 1 )) || die "LXC ${CTID} was created but has no working network."

[[ -n "${TEMP_DIR}" ]] || TEMP_DIR="$(mktemp -d -t pulsebridge-pve-XXXXXX)"
PACKAGE_FILE="${TEMP_DIR}/pulsebridge.tar.gz"; PASSWORD_FILE="${TEMP_DIR}/dashboard-password"
tar -C "${SOURCE_DIR}" \
  --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
  --exclude='build' --exclude='dist' --exclude='data' --exclude='logs' \
  --exclude='local_config.py' -czf "${PACKAGE_FILE}" .
printf '%s' "${DASHBOARD_PASSWORD}" > "${PASSWORD_FILE}"; chmod 600 "${PASSWORD_FILE}"
unset DASHBOARD_PASSWORD PASSWORD_CONFIRM

info "Transferring PulseBridge into the LXC"
pct push "${CTID}" "${PACKAGE_FILE}" /root/pulsebridge.tar.gz
pct push "${CTID}" "${PASSWORD_FILE}" /root/.pulsebridge-password --perms 600

info "Installing PulseBridge inside the LXC"
pct exec "${CTID}" --keep-env 0 -- bash -lc '
  set -Eeuo pipefail
  install -d /root/pulsebridge-installer
  tar -xzf /root/pulsebridge.tar.gz -C /root/pulsebridge-installer
  cd /root/pulsebridge-installer
  DASHBOARD_USERNAME="admin" DASHBOARD_PASSWORD="$(cat /root/.pulsebridge-password)" bash install-lxc.sh
  rm -f /root/.pulsebridge-password /root/pulsebridge.tar.gz
'

LXC_IP="$(pct exec "${CTID}" --keep-env 0 -- hostname -I | awk '{print $1}')"
echo
printf '%b\n' "${GREEN}${APP_NAME} installation completed.${RESET}"
echo "Container: ${CTID} (${HOSTNAME})"
echo "Address:   http://${LXC_IP}:8088"
echo "Username:  admin"
echo "Start at boot: enabled"
echo "Next: open the address and configure ERPNext and the biometric devices."
