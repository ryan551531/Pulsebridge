# Install PulseBridge on Proxmox

## Recommended: helper-style Proxmox installation

Run `proxmox-host-install.sh` in the **Proxmox host shell**. It creates an
unprivileged Debian 12 LXC, configures its resources and network, installs
PulseBridge, enables start-at-boot, and prints the final web address.

The source repository is public, so no GitHub password or access token is
needed. Paste this command into the **Proxmox host shell**:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ryan551531/Pulsebridge/main/proxmox-host-install.sh)"
```

The installer intentionally excludes `local_config.py`, user databases, logs,
and backups. Configure ERPNext after installation or restore an encrypted
PulseBridge backup from Administration.

## Manual installation into an existing Debian/Ubuntu LXC

## 1. Copy the ZIP from the Windows PC

Open PowerShell on the PC and replace `LXC-IP` with the container address:

```powershell
scp "C:\Users\ICT\Documents\Codex\2026-08-12\lo\outputs\erpnext-biometric-web.zip" root@LXC-IP:/root/
```

To carry over the current ERP connection, devices, shifts, and correction settings without re-entering them, securely copy the private configuration separately:

```powershell
scp "C:\Users\ICT\Documents\Codex\2026-08-12\lo\outputs\erpnext-biometric-web\local_config.py" root@LXC-IP:/root/
```

## 2. Install inside the LXC

Connect to the container:

```powershell
ssh root@LXC-IP
```

Then run:

```bash
apt-get update
apt-get install -y unzip
mkdir -p /root/pulsebridge-installer
unzip -o /root/erpnext-biometric-web.zip -d /root/pulsebridge-installer
if [ -f /root/local_config.py ]; then install -m 600 /root/local_config.py /root/pulsebridge-installer/local_config.py; fi
cd /root/pulsebridge-installer
bash install-lxc.sh
```

The installer asks for a dashboard password and prints the final address, normally:

```text
http://LXC-IP:8088
```

The initial browser login username is `admin`. Use the password created during installation.

## 3. Configure and start attendance synchronization

If you copied `local_config.py` separately, your current ERPNext and device settings will already be present. Otherwise, open **Configuration**, enter the ERPNext settings and devices, and save. The deployment ZIP intentionally excludes `local_config.py` so credentials are never embedded in a shareable archive.

After saving, use **Start continuous** on the dashboard. The website itself starts automatically whenever the LXC boots.

## Useful LXC commands

```bash
systemctl status pulsebridge
systemctl restart pulsebridge
journalctl -u pulsebridge -f
```

Keep port `8088` limited to your trusted LAN/VPN. Do not expose it directly to the public internet.
