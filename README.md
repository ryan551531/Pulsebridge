# PulseBridge — ERPNext Biometric Sync Web Dashboard

Developed by Ryan Brown.

The Windows one-file build includes Python and all required packages. User settings and logs persist in `%LOCALAPPDATA%\PulseBridge`, allowing the EXE to be moved without losing saved configuration.

This project wraps the existing ERPNext biometric attendance sync engine in a browser-based control panel. It is intended to run on the same server or local network as the biometric terminals.

## What the website provides

- ERPNext connection and API credential management
- Add, remove, edit, and test biometric devices
- Preserve and edit custom automatic shift-detection logic
- Run a sync cycle from the browser
- Continuous sync service controls
- Device status, last pull/push times, success counts, and error counts
- Green/red device connectivity indicators refreshed by server-side ping every 10 minutes
- Workforce tab with active employee totals overall and by ERPNext Branch
- Automatic shift assignment before each inferred punch to prevent new Off-Shift check-ins
- Auditable single-punch correction after a configurable shift grace period
- Corrections tab showing pending unmatched punches, issued corrections, and ERPNext Auto Attendance status
- Master automatic-correction switch plus independent ON/OFF control for every location
- Automatic PC-to-ZKTeco clock synchronization during attendance pulls
- Manual clock synchronization for one device or every configured device
- Recent activity, error, and console logs
- Allowed ERPNext responses such as duplicate punches advance the device checkpoint without inflating the Needs attention total
- Responsive interface for desktop, tablet, and mobile

This working PC copy preserves the existing `local_config.py`. Keep that file private because it contains the live ERPNext API secret; deployment packages should omit it and be configured after installation.

## Single-punch rules

- A lone IN is paired with an automated OUT at the scheduled shift end.
- A lone OUT is paired with an automated IN at the scheduled shift start.
- The correction runs only after the configured grace period, allowing a late real punch to arrive first.
- Every synthetic Employee Checkin uses an `AUTO-CORRECTION` device label and receives an ERPNext comment explaining why it was issued.
- Beechwood is restricted to `10-6 Workday`; the other configured locations use the daytime shifts.
- If a Shift Assignment cannot be confirmed, the real punch is held for retry instead of being sent as Off-Shift.

## Start on Windows

Double-click `START-PULSEBRIDGE.cmd`. It installs the required Python packages the first time, starts the website, and opens it in your default browser.

Alternatively, open PowerShell in this folder and run:

   ```powershell
   .\start-dashboard.ps1
   ```

The local address is `http://127.0.0.1:8088`.

The Windows starter listens on the local network at port 8088. The owner PC at
`192.168.0.32` and localhost receive automatic administrator access. Every
other device is redirected to the sign-in screen; there is no public sign-up.
The owner creates and manages password accounts from **Administration**.

Passwords are stored as one-way hashes in `data/pulsebridge-auth.db`. Session
secrets and the account database are generated locally and are excluded from
the distributable ZIP.

To change the trusted owner address, set it before starting:

```powershell
$env:PULSEBRIDGE_ADMIN_IPS = '192.168.0.32,127.0.0.1,::1'
.\start-dashboard.ps1
```

Then open `http://SERVER-IP:8088`. Devices other than the trusted owner IP will see the PulseBridge sign-in page.

## Start on Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DASHBOARD_HOST='0.0.0.0'
export PULSEBRIDGE_ADMIN_IPS='192.168.0.32,127.0.0.1,::1'
python app.py
```

For production, run it behind your existing HTTPS reverse proxy and process manager. Do not expose port 8088 directly to the public internet.

For a Debian/Ubuntu LXC, use the included `install-lxc.sh`. Complete copy-and-install commands are in `LXC-INSTALL.md`. The installer creates a protected system service that starts automatically with the container.

For a Proxmox Community Scripts-style setup, paste this into the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ryan551531/pulsebridge-private/main/proxmox-host-install.sh)"
```

It creates and configures the LXC automatically. The public repository requires
no GitHub password or access token. Runtime credentials and local databases
remain excluded from Git.

## Important safety notes

- `local_config.py` contains the ERPNext API secret and is excluded from Git.
- The dashboard only accepts private/loopback IP addresses for biometric terminals.
- Clearing attendance records from a terminal is disabled in the web interface.
- Do not copy `.ssh`, shell history, old logs, server virtual environments, or the original `pass` file into this project.
- Rotate the ERPNext API secret if the downloaded server copy has been shared or stored somewhere untrusted.

## License

The original sync engine is from Frappe's Biometric Attendance Sync Tool and is distributed under GPL-3.0. This web adaptation is provided under the same license.
