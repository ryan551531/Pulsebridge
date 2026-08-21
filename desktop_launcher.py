"""PulseBridge Windows desktop entry point. Developed by Ryan Brown."""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request


def run_sync_mode() -> bool:
    if "--sync-once" in sys.argv:
        import erpnext_sync
        erpnext_sync.main()
        return True
    if "--sync-service" in sys.argv:
        import erpnext_sync
        erpnext_sync.infinite_loop()
        return True
    return False


def wait_until_ready(url: str) -> None:
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("PulseBridge could not start its local dashboard.")


def main() -> None:
    if run_sync_mode():
        return

    from waitress import serve
    import webview
    from app import app

    host = "127.0.0.1"
    port = int(os.getenv("DASHBOARD_PORT", "8088"))
    url = f"http://{host}:{port}"
    server = threading.Thread(
        target=serve,
        kwargs={"app": app, "host": host, "port": port, "threads": 8},
        name="pulsebridge-desktop-server",
        daemon=True,
    )
    server.start()
    wait_until_ready(url)
    webview.create_window("PulseBridge · ERPNext Biometric Sync", url, width=1440, height=900, min_size=(1050, 700))
    webview.start()


if __name__ == "__main__":
    main()
