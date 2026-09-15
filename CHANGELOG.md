# Changelog

All notable PulseBridge changes are recorded here.

## 2026-09-15

### Fixed

- Continuous synchronization now resumes automatically after the PulseBridge
  service, LXC, or host is restarted.
- Added a watchdog that restarts the continuous sync process within 30 seconds
  if it exits unexpectedly.
- Choosing **Stop service** still disables automatic restart intentionally.

## 2026-08-21

### Improved

- Cached ERPNext Employee and Shift Assignment lookups during each sync cycle
  for substantially faster attendance uploads.
- Reused ERPNext HTTP connections while preserving roster validation, shift
  correction, duplicate protection, and parallel device synchronization.
