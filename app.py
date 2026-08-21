"""Self-hosted web control panel for the ERPNext biometric sync service."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

from config_store import ROOT, load_config, save_config
from auth_store import (authenticate, branding, delete_user, export_auth_data, get_user,
                        initialize as initialize_auth, list_users, restore_auth_data,
                        save_branding, save_user, session_secret)
from backup_store import MAX_BACKUP_SIZE, create_backup, open_backup

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
app = Flask(
    __name__,
    template_folder=str(BUNDLE_ROOT / "templates"),
    static_folder=str(BUNDLE_ROOT / "static"),
)
app.config["JSON_SORT_KEYS"] = False
initialize_auth()
app.secret_key = session_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
app.config["DEVELOPER"] = "Ryan Brown"
DEVELOPER_NAME = "Ryan Brown"
LOG_DIR = ROOT / "logs"
SYNC_SCRIPT = ROOT / "erpnext_sync.py"
AUTH_TOKEN_RE = re.compile(r"(?i)(authorization[^\n]{0,80}?token\s+)[^\s'\"]+")
EMPLOYEE_CACHE_TTL = 300
employee_cache: dict[str, Any] = {"expires": 0.0, "value": None}
employee_cache_lock = threading.Lock()
erp_health_cache: dict[str, Any] = {"expires": 0.0, "value": {"connected": None, "checked_at": None}}
erp_health_lock = threading.Lock()
DEVICE_PING_INTERVAL_SECONDS = 600
DEVICE_USER_SCAN_TTL_SECONDS = 600
device_user_scan_cache: dict[str, dict[str, Any]] = {}
device_user_scan_lock = threading.Lock()


def _hidden_subprocess_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


class ServiceManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._mode: str | None = None
        self._lock = threading.Lock()
        self._started_at: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            if not running and self._process is not None:
                self._process = None
                self._mode = None
            return {
                "running": running,
                "pid": self._process.pid if running else None,
                "mode": self._mode if running else None,
                "started_at": self._started_at if running else None,
            }

    def start(self, once: bool = False, device_ids: list[str] | None = None) -> dict[str, Any]:
        if not getattr(sys, "frozen", False) and not SYNC_SCRIPT.exists():
            raise RuntimeError("The sync engine is missing from this installation.")
        if not (ROOT / "local_config.py").exists():
            raise RuntimeError("Save the connection settings before starting the service.")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("The sync service is already running.")
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--sync-once" if once else "--sync-service"]
            else:
                command = [sys.executable, "-u", str(SYNC_SCRIPT)]
                if once:
                    command = [sys.executable, "-u", "-c", "import erpnext_sync; erpnext_sync.main()"]
            LOG_DIR.mkdir(exist_ok=True)
            console_path = LOG_DIR / "service-console.log"
            _rotate_console_log(console_path)
            output = console_path.open("a", encoding="utf-8")
            sync_env = {**os.environ, **({"PULSEBRIDGE_RANGE_SYNC": "1"} if once else {})}
            if device_ids:
                sync_env["PULSEBRIDGE_DEVICE_IDS"] = json.dumps(device_ids)
            self._process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                env=sync_env,
                **_hidden_subprocess_options(),
            )
            output.close()
            self._mode = "device" if device_ids else ("once" if once else "continuous")
            self._started_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            return self.status_unlocked()

    def status_unlocked(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        return {
            "running": running,
            "pid": self._process.pid if running else None,
            "mode": self._mode if running else None,
            "started_at": self._started_at if running else None,
        }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._process = None
                self._mode = None
                return self.status_unlocked()
            self._process.terminate()
            try:
                self._process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            self._process = None
            self._mode = None
            self._started_at = None
            return self.status_unlocked()


service = ServiceManager()


def _erpnext_health(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with erp_health_lock:
        if not force and now < float(erp_health_cache.get("expires", 0)):
            return dict(erp_health_cache["value"])
    private = load_config(include_secret=True)
    checked_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    result = {"connected": False, "checked_at": checked_at, "message": "ERPNext is not configured."}
    if private.get("erpnext_url") and private.get("erpnext_api_key") and private.get("erpnext_api_secret"):
        try:
            response = requests.get(
                f"{private['erpnext_url']}/api/method/frappe.auth.get_logged_user",
                headers={"Authorization": f"token {private['erpnext_api_key']}:{private['erpnext_api_secret']}"},
                timeout=6,
            )
            response.raise_for_status()
            result = {"connected": True, "checked_at": checked_at, "message": "Connected to ERPNext."}
        except requests.RequestException:
            result = {"connected": False, "checked_at": checked_at, "message": "ERPNext is not reachable."}
    with erp_health_lock:
        erp_health_cache["value"] = result
        erp_health_cache["expires"] = now + 60
    return dict(result)


class DeviceConnectivityMonitor:
    """Test configured ZKTeco TCP endpoints every ten minutes."""

    def __init__(self, interval: int = DEVICE_PING_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _ping(device: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        device_id = str(device.get("device_id", ""))
        ip = str(device.get("ip", ""))
        checked_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        port = int(device.get("port") or 4370)
        try:
            with socket.create_connection((ip, port), timeout=4):
                connected = True
        except (OSError, ValueError):
            connected = False
        return device_id, {"connected": connected, "checked_at": checked_at, "port": port}

    def refresh(self) -> None:
        devices = load_config(include_secret=False).get("devices", [])
        if not devices:
            with self._lock:
                self._results = {}
            return
        with ThreadPoolExecutor(max_workers=min(8, len(devices))) as executor:
            results = dict(executor.map(self._ping, devices))
        with self._lock:
            self._results = results

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._results.items()}

    def _run(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:
                app.logger.exception("Device connectivity check failed.")
            time.sleep(self._interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="device-connectivity-monitor", daemon=True)
        self._thread.start()


connectivity_monitor = DeviceConnectivityMonitor()
if os.getenv("PULSEBRIDGE_DISABLE_MONITOR") != "1":
    connectivity_monitor.start()


TRUSTED_ADMIN_IPS = {
    item.strip() for item in os.getenv("PULSEBRIDGE_ADMIN_IPS", "127.0.0.1,::1").split(",") if item.strip()
}


def _current_user() -> dict[str, Any] | None:
    # Only use the socket peer address. Forwarded headers are intentionally not trusted.
    if request.remote_addr in TRUSTED_ADMIN_IPS:
        return {"id": None, "username": "Owner", "role": "admin", "active": 1, "trusted_ip": True}
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_user(int(user_id))
    if not user or not user.get("active"):
        session.clear()
        return None
    user["trusted_ip"] = False
    return user


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = os.urandom(24).hex()
    return str(session["csrf_token"])


@app.before_request
def require_auth() -> Response | None:
    public_endpoints = {"login", "static"}
    if request.endpoint in public_endpoints:
        return None
    g.user = _current_user()
    if not g.user:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "Sign in required."}), 401
        return redirect(url_for("login", next=request.path))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or supplied != _csrf_token():
            return jsonify({"ok": False, "message": "Security token expired. Refresh the page and try again."}), 403
    return None


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


def _json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


def _read_status() -> dict[str, Any]:
    path = LOG_DIR / "status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _rotate_console_log(path: Path, max_bytes: int = 5_000_000, backup_count: int = 5) -> None:
    """Bound captured subprocess output so repeated tracebacks cannot fill the drive."""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        oldest = path.with_name(f"{path.name}.{backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        app.logger.exception("Unable to rotate the sync console log.")


def _tail_rotated(path: Path, limit: int = 600) -> list[str]:
    """Read a longer history across the current activity log and its rotations."""
    rotated: list[tuple[int, Path]] = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        try:
            rotated.append((int(candidate.suffix[1:]), candidate))
        except (TypeError, ValueError):
            continue
    ordered = [item for _, item in sorted(rotated, reverse=True)] + [path]
    lines: list[str] = []
    for item in ordered:
        lines.extend(_tail(item, limit))
    return lines[-limit:]


def _tail(path: Path, limit: int = 120) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = list(deque(handle, maxlen=limit))
    except OSError:
        return []
    return [AUTH_TOKEN_RE.sub(r"\1<redacted>", line.rstrip()) for line in lines]


def _correction_summary(config: dict[str, Any]) -> dict[str, Any]:
    pending_path = LOG_DIR / "pending_single_punches.json"
    try:
        raw_pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        raw_pending = {}

    pending = []
    boundaries = config.get("shift_boundaries", {})
    grace_by_shift = config.get("single_punch_grace_by_shift", {})
    correction_enabled = config.get("auto_correct_single_punches", True)
    correction_by_device = config.get("single_punch_correction_by_device", {})
    for record in raw_pending.values() if isinstance(raw_pending, dict) else []:
        if not isinstance(record, dict):
            continue
        shift = str(record.get("shift_name") or "")
        missing = "OUT" if record.get("in_time") and not record.get("out_time") else "IN"
        pending.append({
            "employee_id": str(record.get("employee_field_value") or ""),
            "device_id": str(record.get("device_id") or ""),
            "shift": shift,
            "assignment_date": record.get("assignment_date"),
            "real_punch": record.get("in_time") or record.get("out_time"),
            "missing": missing,
            "scheduled_time": (boundaries.get(shift) or {}).get("end" if missing == "OUT" else "start"),
            "grace_minutes": int(grace_by_shift.get(shift, 120)),
            "correction_enabled": bool(correction_enabled and correction_by_device.get(str(record.get("device_id") or ""), True)),
        })

    shift_status = []
    private = load_config(include_secret=True)
    shift_names = sorted(set(boundaries))
    if private.get("erpnext_url") and private.get("erpnext_api_secret") and shift_names:
        try:
            response = requests.get(
                f"{private['erpnext_url']}/api/resource/Shift Type",
                headers={
                    "Authorization": f"token {private['erpnext_api_key']}:{private['erpnext_api_secret']}",
                    "Accept": "application/json",
                },
                params={
                    "fields": json.dumps(["name", "enable_auto_attendance"]),
                    "filters": json.dumps([["name", "in", shift_names]]),
                    "limit_page_length": 100,
                },
                timeout=12,
            )
            response.raise_for_status()
            shift_status = response.json().get("data", [])
        except (requests.RequestException, ValueError, TypeError):
            shift_status = []

    return {
        "enabled": correction_enabled,
        "locations": [
            {
                "device_id": str(device.get("device_id") or ""),
                "ip": str(device.get("ip") or ""),
                "enabled": bool(correction_by_device.get(str(device.get("device_id") or ""), True)),
            }
            for device in config.get("devices", [])
        ],
        "pending": sorted(pending, key=lambda item: (item.get("assignment_date") or "", item["employee_id"])),
        "history": _tail(LOG_DIR / "automated_corrections.log", 100),
        "shift_status": shift_status,
    }


def _enrich_devices(config: dict[str, Any]) -> None:
    shifts: dict[str, str] = {}
    for mapping in config.get("shift_type_device_mapping", []):
        name = mapping.get("shift_type_name", "")
        if isinstance(name, list):
            name = ", ".join(name)
        for device_id in mapping.get("related_device_id", []):
            shifts[str(device_id)] = str(name)
    for device in config.get("devices", []):
        device["shift"] = shifts.get(str(device.get("device_id")), "")


def _activity_summary(config: dict[str, Any], status_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for device in config.get("devices", []):
        device_id = device.get("device_id", "")
        success_file = LOG_DIR / f"attendance_success_log_{device_id}.log"
        failed_file = LOG_DIR / f"attendance_failed_log_{device_id}.log"
        success_lines = _tail(success_file, 5000)
        failed_lines = _tail(failed_file, 5000)
        rows.append({
            "device_id": device_id,
            "ip": device.get("ip", ""),
            "shift": device.get("shift", ""),
            "last_pull": status_data.get(f"{device_id}_pull_timestamp"),
            "last_push": status_data.get(f"{device_id}_push_timestamp"),
            "success_count": sum(1 for line in success_lines if "\tIGNORED:" not in line),
            "failure_count": sum(1 for line in failed_lines if "\tACTIONABLE\t" in line),
        })
    return rows


def _normal_location(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _employee_summary(config: dict[str, Any]) -> dict[str, Any]:
    """Return active ERPNext employee totals, cached to avoid frequent API calls."""
    now = time.monotonic()
    with employee_cache_lock:
        cached = employee_cache.get("value")
        if cached is not None and now < float(employee_cache.get("expires", 0)):
            return cached

    private_config = load_config(include_secret=True)
    if not private_config.get("erpnext_url") or not private_config.get("erpnext_api_secret"):
        return {"available": False, "total": None, "locations": [], "message": "ERPNext is not configured."}

    try:
        response = requests.get(
            f"{private_config['erpnext_url']}/api/resource/Employee",
            headers={
                "Authorization": f"token {private_config['erpnext_api_key']}:{private_config['erpnext_api_secret']}",
                "Accept": "application/json",
            },
            params={
                "fields": json.dumps(["name", "branch"]),
                "filters": json.dumps([["status", "=", "Active"]]),
                "limit_page_length": 10000,
            },
            timeout=15,
        )
        response.raise_for_status()
        employees = response.json().get("data", [])
        counts: dict[str, int] = {}
        for employee in employees:
            location = str(employee.get("branch") or "Unassigned").strip()
            counts[location] = counts.get(location, 0) + 1

        locations = [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: item[0].lower())
        ]
        location_lookup = {_normal_location(item["name"]): item["count"] for item in locations}

        def count_for_device(device_id: str) -> int | None:
            normalized = _normal_location(device_id)
            if normalized in location_lookup:
                return location_lookup[normalized]
            matches = [
                count for location, count in location_lookup.items()
                if normalized and (normalized in location or location in normalized)
            ]
            return matches[0] if len(matches) == 1 else None

        result = {
            "available": True,
            "total": len(employees),
            "locations": locations,
            "device_counts": {
                str(device.get("device_id", "")): count_for_device(str(device.get("device_id", "")))
                for device in config.get("devices", [])
            },
            "message": None,
        }
    except (requests.RequestException, ValueError, TypeError):
        result = {
            "available": False,
            "total": None,
            "locations": [],
            "device_counts": {},
            "message": "Employee totals are unavailable. Check ERPNext connection and Employee read permission.",
        }

    with employee_cache_lock:
        employee_cache["value"] = result
        employee_cache["expires"] = now + EMPLOYEE_CACHE_TTL
    return result


def _erp_headers(config: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"token {config['erpnext_api_key']}:{config['erpnext_api_secret']}",
        "Accept": "application/json",
    }


def _normalize_person_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _erp_list(config: dict[str, Any], doctype: str, fields: list[str], filters: list | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "fields": json.dumps(fields),
        "limit_page_length": 10000,
    }
    if filters:
        params["filters"] = json.dumps(filters)
    response = requests.get(
        f"{config['erpnext_url']}/api/resource/{doctype}",
        headers=_erp_headers(config),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def _find_device(config: dict[str, Any], device_id: str) -> dict[str, Any] | None:
    return next(
        (device for device in config.get("devices", []) if str(device.get("device_id")) == str(device_id)),
        None,
    )


def _scan_device_users(config: dict[str, Any], device_id: str) -> dict[str, Any]:
    """Read safe identity/enrollment metadata directly from one ZKTeco clock."""
    from zk import ZK

    device = _find_device(config, device_id)
    if not device:
        raise ValueError("Choose a configured biometric device.")

    connection = None
    try:
        connection = ZK(
            str(device.get("ip", "")),
            port=int(device.get("port", 4370)),
            timeout=20,
            ommit_ping=True,
        ).connect()
        users = connection.get_users() or []
        # Only the template UID and count are retained. Biometric template
        # bytes never leave this function and are never returned to the UI.
        fingerprint_counts: dict[int, int] = {}
        for template in connection.get_templates() or []:
            if int(getattr(template, "valid", 1)):
                uid = int(getattr(template, "uid"))
                fingerprint_counts[uid] = fingerprint_counts.get(uid, 0) + 1
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

    employees = _erp_list(
        config,
        "Employee",
        ["name", "employee_name", "attendance_device_id", "status", "branch", "company"],
    )
    by_device_id = {
        str(employee.get("attendance_device_id") or "").strip(): employee
        for employee in employees
        if str(employee.get("attendance_device_id") or "").strip()
    }
    by_name: dict[str, list[dict[str, Any]]] = {}
    for employee in employees:
        key = _normalize_person_name(employee.get("employee_name", ""))
        if key:
            by_name.setdefault(key, []).append(employee)

    rows = []
    for user in users:
        user_id = str(getattr(user, "user_id", "") or "").strip()
        raw_name = str(getattr(user, "name", "") or "").strip()
        name = re.sub(r"[._]+", " ", raw_name).strip()
        exact = by_device_id.get(user_id)
        possible = [] if exact else by_name.get(_normalize_person_name(name), [])
        fingerprint_count = fingerprint_counts.get(int(getattr(user, "uid")), 0)
        rows.append({
            "uid": int(getattr(user, "uid")),
            "user_id": user_id,
            "name": name,
            "fingerprint_count": fingerprint_count,
            "has_fingerprint": fingerprint_count > 0,
            "erp_employee": exact,
            "possible_erp_match": possible[0] if len(possible) == 1 else None,
            "eligible": bool(user_id and name and fingerprint_count > 0 and not exact and not possible),
        })

    companies = _erp_list(config, "Company", ["name"], [["is_group", "=", 0]])
    genders = _erp_list(config, "Gender", ["name"])
    branches = _erp_list(config, "Branch", ["name"])
    branch_names = [str(item.get("name") or "") for item in branches]
    normalized_device = _normal_location(device_id)
    branch_matches = [
        branch for branch in branch_names
        if normalized_device and (normalized_device in _normal_location(branch) or _normal_location(branch) in normalized_device)
    ]
    default_branch = branch_matches[0] if len(branch_matches) == 1 else ""
    result = {
        "device": {"device_id": device_id, "ip": str(device.get("ip", ""))},
        "scanned_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "users": sorted(rows, key=lambda row: (row["name"].lower(), row["user_id"])),
        "summary": {
            "total": len(rows),
            "with_fingerprint": sum(1 for row in rows if row["has_fingerprint"]),
            "in_erp": sum(1 for row in rows if row["erp_employee"]),
            "eligible": sum(1 for row in rows if row["eligible"]),
            "possible_matches": sum(1 for row in rows if row["possible_erp_match"]),
        },
        "options": {
            "companies": [str(item.get("name") or "") for item in companies],
            "genders": [str(item.get("name") or "") for item in genders],
            "branches": branch_names,
            "default_company": str(companies[0].get("name") or "") if len(companies) == 1 else "",
            "default_branch": default_branch,
            "default_joining_date": date.today().isoformat(),
        },
    }
    with device_user_scan_lock:
        device_user_scan_cache[device_id] = {"expires": time.monotonic() + DEVICE_USER_SCAN_TTL_SECONDS, "value": result}
    return result


def _split_employee_name(value: str) -> dict[str, str]:
    parts = [part for part in re.split(r"[\s._]+", str(value or "").strip()) if part]
    if not parts:
        raise ValueError("The device user has no name.")
    result = {"first_name": parts[0]}
    if len(parts) == 2:
        result["last_name"] = parts[1]
    elif len(parts) > 2:
        result["middle_name"] = " ".join(parts[1:-1])
        result["last_name"] = parts[-1]
    return result


def _sync_device_clock(device: dict[str, Any]) -> dict[str, Any]:
    """Set one terminal clock from this PC and return verification details."""
    from zk import ZK

    device_id = str(device.get("device_id", ""))
    ip = str(device.get("ip", ""))
    connection = None
    try:
        connection = ZK(
            ip,
            port=int(device.get("port", 4370)),
            timeout=12,
            ommit_ping=True,
        ).connect()
        before = connection.get_time()
        target = datetime.now().replace(microsecond=0)
        connection.set_time(target)
        after = connection.get_time()
        return {
            "device_id": device_id,
            "ip": ip,
            "ok": True,
            "before": before.isoformat(sep=" ") if before else None,
            "pc_time": target.isoformat(sep=" "),
            "after": after.isoformat(sep=" ") if after else None,
            "message": f"{device_id} synchronized successfully.",
        }
    except Exception:
        return {
            "device_id": device_id,
            "ip": ip,
            "ok": False,
            "before": None,
            "pc_time": datetime.now().replace(microsecond=0).isoformat(sep=" "),
            "after": None,
            "message": f"Could not synchronize {device_id} at {ip}.",
        }
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.remote_addr in TRUSTED_ADMIN_IPS:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = authenticate(request.form.get("username", ""), request.form.get("password", ""))
        if user:
            session.clear()
            session["user_id"] = user["id"]
            _csrf_token()
            return redirect(url_for("index"))
        error = "Incorrect username or password."
    return render_template("login.html", branding=branding(), error=error)


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True, "message": "Signed out."})


def _require_admin():
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"ok": False, "message": "Administrator access required."}), 403
    return None


@app.get("/api/admin/users")
def api_admin_users():
    denied = _require_admin()
    return denied or jsonify({"ok": True, "users": list_users()})


@app.post("/api/admin/users")
def api_admin_create_user():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"ok": True, "message": "User created.", "user": save_user(request.get_json(force=True) or {})})
    except ValueError as exc:
        return _json_error(str(exc))


@app.put("/api/admin/users/<int:user_id>")
def api_admin_update_user(user_id: int):
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"ok": True, "message": "User updated.", "user": save_user(request.get_json(force=True) or {}, user_id)})
    except ValueError as exc:
        return _json_error(str(exc))


@app.delete("/api/admin/users/<int:user_id>")
def api_admin_delete_user(user_id: int):
    denied = _require_admin()
    if denied:
        return denied
    if g.user.get("id") == user_id:
        return _json_error("You cannot delete your own signed-in account.")
    delete_user(user_id)
    return jsonify({"ok": True, "message": "User deleted."})


@app.post("/api/admin/branding")
def api_admin_branding():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"ok": True, "message": "Branding saved.", "branding": save_branding(request.get_json(force=True) or {})})
    except ValueError as exc:
        return _json_error(str(exc))


@app.post("/api/admin/backup/export")
def api_admin_backup_export():
    denied = _require_admin()
    if denied:
        return denied
    payload = request.get_json(force=True) or {}
    try:
        content = create_backup(str(payload.get("password") or ""))
    except ValueError as exc:
        return _json_error(str(exc))
    filename = f"pulsebridge-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pulsebackup"
    response = Response(content, mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/admin/backup/restore")
def api_admin_backup_restore():
    denied = _require_admin()
    if denied:
        return denied
    uploaded = request.files.get("backup")
    if not uploaded or not uploaded.filename:
        return _json_error("Choose a PulseBridge backup file.")
    content = uploaded.stream.read(MAX_BACKUP_SIZE + 1)
    try:
        restored = open_backup(content, str(request.form.get("password") or ""))
    except ValueError as exc:
        return _json_error(str(exc))

    previous_config = load_config(include_secret=True)
    previous_auth = export_auth_data()
    service_before = service.status()
    if service_before.get("running"):
        service.stop()
    try:
        save_config(restored["configuration"])
        restore_auth_data(restored["authentication"])
    except (TypeError, ValueError, OSError) as exc:
        app.logger.exception("PulseBridge backup restore failed; rolling back local setup.")
        try:
            save_config(previous_config)
            restore_auth_data(previous_auth)
        except Exception:
            app.logger.exception("PulseBridge backup rollback failed.")
        if service_before.get("running") and service_before.get("mode") == "continuous":
            try:
                service.start()
            except Exception:
                app.logger.exception("Continuous sync could not restart after backup rollback.")
        return _json_error(f"Restore failed: {str(exc)}", 400)

    with employee_cache_lock:
        employee_cache["expires"] = 0.0
    with erp_health_lock:
        erp_health_cache["expires"] = 0.0
    with device_user_scan_lock:
        device_user_scan_cache.clear()
    restarted = False
    if service_before.get("running") and service_before.get("mode") == "continuous":
        try:
            service.start()
            restarted = True
        except Exception:
            app.logger.exception("Continuous sync could not restart after backup restore.")
    return jsonify({
        "ok": True,
        "message": "PulseBridge setup restored successfully. Refreshing the dashboard…",
        "created_at": restored.get("created_at"),
        "service_restarted": restarted,
    })


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    if request.args.get("refresh") == "1":
        with employee_cache_lock:
            employee_cache["expires"] = 0.0
        with erp_health_lock:
            erp_health_cache["expires"] = 0.0
    config = load_config(include_secret=False)
    _enrich_devices(config)
    status_data = _read_status()
    employees = _employee_summary(config)
    devices = _activity_summary(config, status_data)
    connectivity = connectivity_monitor.snapshot()
    for device in devices:
        device["employee_count"] = employees.get("device_counts", {}).get(device["device_id"])
        device["connectivity"] = connectivity.get(device["device_id"], {
            "connected": None,
            "checked_at": None,
        })
    return jsonify({
        "ok": True,
        "configured": (ROOT / "local_config.py").exists(),
        "config": config,
        "service": service.status(),
        "status": status_data,
        "devices": devices,
        "employees": employees,
        "last_cycle": status_data.get("mission_accomplished_timestamp"),
        "erpnext": _erpnext_health(),
        "developer": DEVELOPER_NAME,
        "auth": {"user": g.user, "csrf_token": _csrf_token()},
        "branding": branding(),
    })


@app.get("/api/sync-progress")
def api_sync_progress():
    """Lightweight status polling while a synchronization is active."""
    status_data = _read_status()
    return jsonify({
        "ok": True,
        "sync_progress": status_data.get("sync_progress") or {},
        "last_cycle": status_data.get("mission_accomplished_timestamp"),
        "service": service.status(),
    })


@app.post("/api/config")
def api_config():
    try:
        payload = request.get_json(force=True)
        result = save_config(payload or {})
        _enrich_devices(result)
        return jsonify({"ok": True, "message": "Configuration saved.", "config": result})
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))
    except OSError:
        return _json_error("The configuration file could not be written.", 500)


@app.post("/api/service/<action>")
def api_service(action: str):
    try:
        if action == "start":
            state = service.start(once=False)
            message = "Continuous sync started."
        elif action == "run-once":
            state = service.start(once=True)
            message = "A sync cycle was started."
        elif action == "run-device":
            payload = request.get_json(silent=True) or {}
            requested_ids = payload.get("device_ids")
            if not isinstance(requested_ids, list):
                requested_ids = [payload.get("device_id")]
            requested_ids = {str(value or "").strip() for value in requested_ids if str(value or "").strip()}
            configured_ids = [
                str(item.get("device_id") or "")
                for item in load_config(include_secret=False).get("devices", [])
            ]
            selected_ids = [device_id for device_id in configured_ids if device_id in requested_ids]
            if not selected_ids:
                return _json_error("Select at least one saved device to synchronize.", 400)
            state = service.start(once=True, device_ids=selected_ids)
            message = f"Attendance sync started for {len(selected_ids)} selected location{'s' if len(selected_ids) != 1 else ''}."
        elif action == "stop":
            state = service.stop()
            message = "Sync service stopped."
        else:
            return _json_error("Unknown service action.", 404)
        return jsonify({"ok": True, "message": message, "service": state})
    except RuntimeError as exc:
        return _json_error(str(exc), 409)


@app.post("/api/test/erpnext")
def test_erpnext():
    config = load_config(include_secret=True)
    if not config.get("erpnext_url"):
        return _json_error("Save ERPNext settings first.")
    try:
        response = requests.get(
            f"{config['erpnext_url']}/api/method/frappe.auth.get_logged_user",
            headers={
                "Authorization": f"token {config['erpnext_api_key']}:{config['erpnext_api_secret']}",
                "Accept": "application/json",
            },
            timeout=12,
        )
        response.raise_for_status()
        user = response.json().get("message", "authenticated user")
        with erp_health_lock:
            erp_health_cache["expires"] = 0.0
        return jsonify({"ok": True, "message": f"Connected to ERPNext as {user}."})
    except requests.RequestException as exc:
        code = exc.response.status_code if exc.response is not None else None
        detail = f" (HTTP {code})" if code else ""
        return _json_error(f"ERPNext connection failed{detail}.", 502)


@app.post("/api/test/device")
def test_device():
    payload = request.get_json(force=True) or {}
    ip = str(payload.get("ip", "")).strip()
    try:
        parsed_ip = ipaddress.ip_address(ip)
        if not (parsed_ip.is_private or parsed_ip.is_loopback):
            return _json_error("Device tests are limited to private network addresses.")
        from zk import ZK

        connection = ZK(ip, port=4370, timeout=20, ommit_ping=True).connect()
        try:
            name = connection.get_device_name() or "biometric device"
        finally:
            connection.disconnect()
        return jsonify({"ok": True, "message": f"Connected to {name} at {ip}."})
    except ValueError:
        return _json_error("Enter a valid private device IP address.")
    except Exception:
        return _json_error(f"Could not connect to the biometric device at {ip}.", 502)


@app.post("/api/devices/time-sync")
def sync_device_time():
    payload = request.get_json(silent=True) or {}
    requested_id = str(payload.get("device_id") or "").strip()
    config = load_config(include_secret=False)
    devices = config.get("devices", [])
    if requested_id:
        devices = [item for item in devices if str(item.get("device_id")) == requested_id]
        if not devices:
            return _json_error("The requested device is not configured.", 404)
    if not devices:
        return _json_error("No biometric devices are configured.")

    with ThreadPoolExecutor(max_workers=min(6, len(devices))) as executor:
        results = list(executor.map(_sync_device_clock, devices))
    succeeded = sum(1 for item in results if item["ok"])
    message = f"Synchronized {succeeded} of {len(results)} device clocks from this PC."
    return jsonify({
        "ok": succeeded > 0,
        "message": message,
        "succeeded": succeeded,
        "total": len(results),
        "results": results,
    }), 200 if succeeded > 0 else 502


@app.get("/api/device-users/scan")
def api_scan_device_users():
    denied = _require_admin()
    if denied:
        return denied
    device_id = str(request.args.get("device_id") or "").strip()
    if not device_id:
        return _json_error("Choose a biometric device to scan.")
    config = load_config(include_secret=True)
    try:
        result = _scan_device_users(config, device_id)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return _json_error(str(exc))
    except requests.RequestException:
        return _json_error("The device was scanned, but ERPNext employee data could not be loaded.", 502)
    except Exception:
        app.logger.exception("Device user scan failed for %s", device_id)
        return _json_error(f"Could not read users from {device_id}.", 502)


@app.post("/api/device-users/name")
def api_update_device_user_name():
    denied = _require_admin()
    if denied:
        return denied
    payload = request.get_json(force=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    name = re.sub(r"\s+", " ", str(payload.get("name") or "").strip())
    if not device_id or not user_id or not name:
        return _json_error("Device, user ID, and name are required.")
    if len(name.encode("utf-8")) > 24:
        return _json_error("ZKTeco names must be 24 bytes or fewer. Shorten the name and try again.")
    config = load_config(include_secret=False)
    device = _find_device(config, device_id)
    if not device:
        return _json_error("The selected device is not configured.", 404)

    from zk import ZK
    connection = None
    try:
        connection = ZK(
            str(device.get("ip", "")),
            port=int(device.get("port", 4370)),
            timeout=20,
            ommit_ping=True,
        ).connect()
        user = next((item for item in (connection.get_users() or []) if str(item.user_id) == user_id), None)
        if not user:
            return _json_error("That user ID is no longer present on the device.", 404)
        connection.set_user(
            uid=user.uid,
            name=name,
            privilege=user.privilege,
            password=user.password,
            group_id=user.group_id,
            user_id=user.user_id,
            card=user.card,
        )
    except Exception:
        app.logger.exception("Unable to update device user name for %s on %s", user_id, device_id)
        return _json_error("The name could not be saved to the biometric device.", 502)
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass
    with device_user_scan_lock:
        device_user_scan_cache.pop(device_id, None)
    return jsonify({"ok": True, "message": f"Updated device user {user_id} to {name}.", "name": name})


@app.post("/api/device-users/import")
def api_import_device_users():
    denied = _require_admin()
    if denied:
        return denied
    payload = request.get_json(force=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    requested = payload.get("users") or []
    if not device_id or not isinstance(requested, list) or not requested:
        return _json_error("Select at least one scanned device user.")
    if len(requested) > 100:
        return _json_error("Import no more than 100 users at a time.")

    with device_user_scan_lock:
        cached = device_user_scan_cache.get(device_id)
        scan = cached.get("value") if cached and time.monotonic() < float(cached.get("expires", 0)) else None
    if not scan:
        return _json_error("The device scan expired. Scan the device again before importing.", 409)

    config = load_config(include_secret=True)
    if not _find_device(config, device_id):
        return _json_error("The scanned device is no longer configured.", 409)
    available = {str(row["user_id"]): row for row in scan.get("users", []) if row.get("eligible")}
    options = scan.get("options", {})
    company = str(payload.get("company") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    joining_date = str(payload.get("date_of_joining") or "").strip()
    if company not in options.get("companies", []):
        return _json_error("Choose a valid ERPNext company.")
    if branch and branch not in options.get("branches", []):
        return _json_error("Choose a valid ERPNext branch.")
    try:
        joining = date.fromisoformat(joining_date)
    except ValueError:
        return _json_error("Choose a valid date of joining.")

    try:
        existing_employees = _erp_list(
            config,
            "Employee",
            ["name", "employee_name", "attendance_device_id"],
        )
        employee_number_rows = _erp_list(config, "Employee", ["employee_number"])
    except requests.RequestException:
        return _json_error("ERPNext employees could not be checked before import.", 502)
    existing_ids = {
        str(item.get("attendance_device_id") or "").strip(): item
        for item in existing_employees
        if str(item.get("attendance_device_id") or "").strip()
    }
    existing_names = {
        _normalize_person_name(str(item.get("employee_name") or ""))
        for item in existing_employees
        if _normalize_person_name(str(item.get("employee_name") or ""))
    }
    existing_employee_numbers = {
        str(item.get("employee_number") or "").strip()
        for item in employee_number_rows
        if str(item.get("employee_number") or "").strip()
    }

    results = []
    created = 0
    for item in requested:
        user_id = str(item.get("user_id") or "").strip()
        scanned = available.get(user_id)
        if not scanned:
            results.append({"user_id": user_id, "ok": False, "message": "Not eligible or no longer in the scan."})
            continue
        if user_id in existing_ids:
            results.append({"user_id": user_id, "name": scanned["name"], "ok": False, "message": "Already exists in ERPNext."})
            continue
        edited_name = re.sub(r"\s+", " ", str(item.get("name") or scanned["name"]).strip())
        employee_number = str(item.get("employee_number") or user_id).strip()
        if not edited_name:
            results.append({"user_id": user_id, "ok": False, "message": "Enter an employee name."})
            continue
        if _normalize_person_name(edited_name) in existing_names:
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "That employee name already exists in ERPNext. Review the existing employee instead."})
            continue
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,140}", employee_number):
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "Enter a valid ERP employee ID."})
            continue
        if employee_number in existing_employee_numbers:
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "That ERP employee ID is already in use."})
            continue
        gender = str(item.get("gender") or "").strip()
        birth_date = str(item.get("date_of_birth") or "").strip()
        if gender not in options.get("genders", []):
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "Choose a valid gender."})
            continue
        try:
            born = date.fromisoformat(birth_date)
            if born >= date.today() or born > joining:
                raise ValueError
        except ValueError:
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "Enter a valid date of birth."})
            continue

        employee_payload: dict[str, Any] = {
            **_split_employee_name(edited_name),
            "employee_number": employee_number,
            "attendance_device_id": user_id,
            "company": company,
            "gender": gender,
            "date_of_birth": birth_date,
            "date_of_joining": joining_date,
            # New clock users remain inactive until HR completes and verifies
            # their profile in ERPNext.
            "status": "Inactive",
        }
        if branch:
            employee_payload["branch"] = branch
        try:
            response = requests.post(
                f"{config['erpnext_url']}/api/resource/Employee",
                headers={**_erp_headers(config), "Content-Type": "application/json"},
                json=employee_payload,
                timeout=30,
            )
            if response.status_code not in (200, 201):
                try:
                    error_data = response.json()
                    message = str(error_data.get("message") or error_data.get("exception") or f"ERPNext rejected the employee (HTTP {response.status_code}).")
                except ValueError:
                    message = f"ERPNext rejected the employee (HTTP {response.status_code})."
                results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": message[:240]})
                continue
            employee = response.json().get("data", {})
            created += 1
            existing_ids[user_id] = employee
            existing_employee_numbers.add(employee_number)
            existing_names.add(_normalize_person_name(edited_name))
            results.append({
                "user_id": user_id,
                "name": edited_name,
                "ok": True,
                "employee": employee.get("name"),
                "message": "Created inactive in ERPNext for HR completion.",
            })
        except requests.RequestException:
            results.append({"user_id": user_id, "name": edited_name, "ok": False, "message": "ERPNext could not be reached."})

    with employee_cache_lock:
        employee_cache["expires"] = 0.0
    with device_user_scan_lock:
        device_user_scan_cache.pop(device_id, None)
    return jsonify({
        "ok": True,
        "message": f"Created {created} of {len(requested)} selected users as inactive ERPNext employees.",
        "created": created,
        "total": len(requested),
        "results": results,
    })


@app.get("/api/logs")
def api_logs():
    kind = request.args.get("kind", "activity")
    allowed = {
        "activity": LOG_DIR / "logs.log",
        "errors": LOG_DIR / "error.log",
        "console": LOG_DIR / "service-console.log",
    }
    if kind == "history":
        return jsonify({"ok": True, "kind": kind, "lines": _tail_rotated(LOG_DIR / "logs.log")})
    if kind not in allowed:
        return _json_error("Unknown log type.", 404)
    return jsonify({"ok": True, "kind": kind, "lines": _tail(allowed[kind])})


@app.get("/api/corrections")
def api_corrections():
    config = load_config(include_secret=False)
    return jsonify({"ok": True, **_correction_summary(config)})


@app.post("/api/corrections/settings")
def api_correction_settings():
    payload = request.get_json(force=True) or {}
    config = load_config(include_secret=False)
    known_devices = {str(item.get("device_id") or "") for item in config.get("devices", [])}
    submitted = payload.get("locations") or {}
    if not isinstance(submitted, dict):
        return _json_error("Location correction settings must be an object.")
    config["auto_correct_single_punches"] = bool(payload.get("enabled", True))
    config["single_punch_correction_by_device"] = {
        device_id: bool(submitted.get(device_id, True)) for device_id in known_devices
    }
    try:
        service_before = service.status()
        saved = save_config(config)
        restarted = False
        if service_before.get("running") and service_before.get("mode") == "continuous":
            service.stop()
            service.start(once=False)
            restarted = True
        return jsonify({
            "ok": True,
            "message": "Automatic correction settings saved." + (" Continuous sync was restarted." if restarted else ""),
            **_correction_summary(saved),
        })
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))


@app.post("/api/corrections/run-range")
def api_run_correction_range():
    payload = request.get_json(force=True) or {}
    try:
        date_from = datetime.strptime(str(payload.get("from") or ""), "%Y-%m-%d").date()
        date_to = datetime.strptime(str(payload.get("to") or ""), "%Y-%m-%d").date()
    except ValueError:
        return _json_error("Choose valid From and To dates.")
    if date_to < date_from:
        return _json_error("Correction To date cannot be earlier than the From date.")
    if (date_to - date_from).days > 366:
        return _json_error("Correction range cannot exceed 366 days.")
    if service.status().get("running"):
        return _json_error("Wait for synchronization to finish or stop continuous sync before correcting a date range.", 409)
    config = load_config(include_secret=False)
    if not config.get("auto_correct_single_punches", True):
        return _json_error("Automatic correction is turned off overall.")
    enabled_by_device = config.get("single_punch_correction_by_device", {})
    known = {str(item.get("device_id") or "") for item in config.get("devices", [])}
    requested = {str(item) for item in (payload.get("locations") or [])}
    selected = sorted(device_id for device_id in known if device_id in requested and enabled_by_device.get(device_id, True))
    if not selected:
        return _json_error("Select at least one enabled location.")
    command = [sys.executable, str(SYNC_SCRIPT), "--correct-range", date_from.isoformat(), date_to.isoformat(), json.dumps(selected)]
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600, check=False, **_hidden_subprocess_options())
        if completed.returncode != 0:
            app.logger.error("Correction range process failed: %s", completed.stderr[-2000:])
            return _json_error("Correction could not finish. Review Activity logs for details.", 502)
        result_line = next((line for line in reversed(completed.stdout.splitlines()) if line.strip().startswith("[")), "[]")
        corrections = json.loads(result_line)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        app.logger.exception("Correction range process failed.")
        return _json_error("Correction could not finish. Review Activity logs for details.", 502)
    summary = _correction_summary(load_config(include_secret=False))
    return jsonify({
        "ok": True,
        "message": f"Date range processed. {len(corrections)} missing punch{'es' if len(corrections) != 1 else ''} created.",
        "created": len(corrections),
        **summary,
    })


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8088"))
    app.run(host=host, port=port, debug=False)
