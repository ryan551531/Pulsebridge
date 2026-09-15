# Changelog

All notable PulseBridge changes are recorded here.

## 2026-09-15

### Changed

- Updated the permanent developer credit to **Developed by ryan55** throughout
  the application and repository documentation.
- Reworked the Proxmox host installer with console dialogs, recommended and
  advanced installation modes, and selectable template/container storage
  populated from the host's compatible active storage targets.
- Fresh LXC installations now enable continuous synchronization by default;
  the sync watchdog starts it automatically after configuration is saved.
- Added automatic ZKTeco biometric-device discovery for private network ranges. The
  scanner checks only port 4370, confirms devices through the ZK protocol, and
  leaves every discovered device pending for administrator review.
- Discovery now accepts administrator-friendly address ranges such as
  `192.168.0.1-254` and consistently labels results as biometric devices.

### Fixed

- Continuous synchronization now resumes automatically after the PulseBridge
  service, LXC, or host is restarted.
- Added a watchdog that restarts the continuous sync process within 30 seconds
  if it exits unexpectedly.
- Choosing **Stop service** still disables automatic restart intentionally.
- Added a non-interactive LXC updater that safely preserves runtime data,
  repairs file ownership, refreshes dependencies, and restarts PulseBridge.
- The interactive installer now exits with clear guidance if it is incorrectly
  invoked through a `curl` pipe, preventing script input from being consumed as
  a dashboard password.

## 2026-08-21

### Improved

- Cached ERPNext Employee and Shift Assignment lookups during each sync cycle
  for substantially faster attendance uploads.
- Reused ERPNext HTTP connections while preserving roster validation, shift
  correction, duplicate protection, and parallel device synchronization.
