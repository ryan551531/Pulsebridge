"""Validated configuration storage for the biometric sync dashboard."""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


if getattr(sys, "frozen", False):
    ROOT = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "PulseBridge"
    ROOT.mkdir(parents=True, exist_ok=True)
    legacy_config = Path(sys.executable).resolve().parent / "local_config.py"
    if legacy_config.exists() and not (ROOT / "local_config.py").exists():
        shutil.copy2(legacy_config, ROOT / "local_config.py")
else:
    ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "local_config.py"
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


DEFAULT_CONFIG: dict[str, Any] = {
    "erpnext_api_key": "",
    "erpnext_api_secret": "",
    "erpnext_url": "",
    "erpnext_version": 15,
    "pull_frequency": 15,
    "import_start_date": None,
    "import_end_date": None,
    "devices": [],
    "shift_type_device_mapping": [],
    "allowed_exceptions": [1, 2, 3],
    "device_default_shift": {},
    "shift_logic": {},
    "shift_boundaries": {},
    "auto_shift_detection_enabled": False,
    "auto_shift_excluded_ips": [],
    "auto_shift_max_distance_minutes": 240,
    "auto_shift_allowed_by_device": {},
    "auto_create_shift_assignment": True,
    "auto_created_shift_assignment_prefix": "AUTO-SYNC",
    "sync_device_time_with_pc": True,
    "auto_correct_single_punches": True,
    "single_punch_correction_by_device": {},
    "single_punch_grace_by_shift": {
        "10-6 Workday": 60,
        "2-10 Workday": 60,
        "6-2 Workday": 60,
        "8-5 Workday": 120,
    },
}


def _load_module() -> Any | None:
    if not CONFIG_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("dashboard_local_config", CONFIG_PATH)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_config(include_secret: bool = False) -> dict[str, Any]:
    module = _load_module()
    result = dict(DEFAULT_CONFIG)
    if module:
        mapping = {
            "erpnext_api_key": "ERPNEXT_API_KEY",
            "erpnext_api_secret": "ERPNEXT_API_SECRET",
            "erpnext_url": "ERPNEXT_URL",
            "erpnext_version": "ERPNEXT_VERSION",
            "pull_frequency": "PULL_FREQUENCY",
            "import_start_date": "IMPORT_START_DATE",
            "import_end_date": "IMPORT_END_DATE",
            "devices": "devices",
            "shift_type_device_mapping": "shift_type_device_mapping",
            "allowed_exceptions": "allowed_exceptions",
            "device_default_shift": "DEVICE_DEFAULT_SHIFT",
            "shift_logic": "SHIFT_LOGIC",
            "shift_boundaries": "SHIFT_BOUNDARIES",
            "auto_shift_detection_enabled": "AUTO_SHIFT_DETECTION_ENABLED",
            "auto_shift_excluded_ips": "AUTO_SHIFT_EXCLUDED_IPS",
            "auto_shift_max_distance_minutes": "AUTO_SHIFT_MAX_DISTANCE_MINUTES",
            "auto_shift_allowed_by_device": "AUTO_SHIFT_ALLOWED_BY_DEVICE",
            "auto_create_shift_assignment": "AUTO_CREATE_SHIFT_ASSIGNMENT",
            "auto_created_shift_assignment_prefix": "AUTO_CREATED_SHIFT_ASSIGNMENT_PREFIX",
            "sync_device_time_with_pc": "SYNC_DEVICE_TIME_WITH_PC",
            "auto_correct_single_punches": "AUTO_CORRECT_SINGLE_PUNCHES",
            "single_punch_correction_by_device": "SINGLE_PUNCH_CORRECTION_BY_DEVICE",
            "single_punch_grace_by_shift": "SINGLE_PUNCH_GRACE_BY_SHIFT",
        }
        for target, source in mapping.items():
            if hasattr(module, source):
                result[target] = getattr(module, source)
    if not include_secret:
        result["erpnext_api_secret"] = ""
        result["has_api_secret"] = bool(getattr(module, "ERPNEXT_API_SECRET", "")) if module else False
    return result


def _valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_config(data: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    clean = dict(DEFAULT_CONFIG)
    clean["erpnext_api_key"] = str(data.get("erpnext_api_key", "")).strip()
    supplied_secret = str(data.get("erpnext_api_secret", "")).strip()
    clean["erpnext_api_secret"] = supplied_secret or existing.get("erpnext_api_secret", "")
    clean["erpnext_url"] = str(data.get("erpnext_url", "")).strip().rstrip("/")
    if not _valid_url(clean["erpnext_url"]):
        raise ValueError("ERPNext URL must start with http:// or https:// and include a host.")
    if not clean["erpnext_api_key"]:
        raise ValueError("ERPNext API key is required.")
    if not clean["erpnext_api_secret"]:
        raise ValueError("ERPNext API secret is required.")

    clean["erpnext_version"] = int(data.get("erpnext_version", 15))
    if not 12 <= clean["erpnext_version"] <= 99:
        raise ValueError("ERPNext version must be 12 or newer.")
    clean["pull_frequency"] = int(data.get("pull_frequency", 15))
    if not 1 <= clean["pull_frequency"] <= 1440:
        raise ValueError("Pull frequency must be between 1 and 1440 minutes.")

    import_date = str(data.get("import_start_date") or "").replace("-", "").strip()
    if import_date and not re.fullmatch(r"\d{8}", import_date):
        raise ValueError("Import start date must be a valid YYYY-MM-DD date.")
    clean["import_start_date"] = import_date or None
    import_end_date = str(data.get("import_end_date") or "").replace("-", "").strip()
    if import_end_date and not re.fullmatch(r"\d{8}", import_end_date):
        raise ValueError("Import end date must be a valid YYYY-MM-DD date.")
    if import_date and import_end_date and import_end_date < import_date:
        raise ValueError("Import To date cannot be earlier than the From date.")
    clean["import_end_date"] = import_end_date or None

    devices = data.get("devices") or []
    if not isinstance(devices, list):
        raise ValueError("Devices must be a list.")
    seen: set[str] = set()
    clean_devices: list[dict[str, Any]] = []
    shift_map: dict[str, list[str]] = {}
    for item in devices:
        device_id = str(item.get("device_id", "")).strip()
        ip = str(item.get("ip", "")).strip()
        if not DEVICE_ID_RE.fullmatch(device_id):
            raise ValueError(f"Device ID '{device_id}' may contain only letters, numbers, - and _.")
        if device_id in seen:
            raise ValueError(f"Device ID '{device_id}' is duplicated.")
        try:
            parsed_ip = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ValueError(f"Device '{device_id}' has an invalid IP address.") from exc
        if not (parsed_ip.is_private or parsed_ip.is_loopback):
            raise ValueError(f"Device '{device_id}' must use a private network IP address.")
        seen.add(device_id)
        direction = str(item.get("punch_direction") or "AUTO").upper()
        if direction not in {"IN", "OUT", "AUTO", "NONE"}:
            raise ValueError(f"Device '{device_id}' has an invalid punch direction.")
        shift = str(item.get("shift", "")).strip()
        if shift:
            shift_map.setdefault(shift, []).append(device_id)
        clean_devices.append({
            "device_id": device_id,
            "ip": ip,
            "punch_direction": None if direction == "NONE" else direction,
            "clear_from_device_on_fetch": bool(item.get("clear_from_device_on_fetch", False)),
            "latitude": float(item.get("latitude") or 0),
            "longitude": float(item.get("longitude") or 0),
        })
    clean["devices"] = clean_devices
    clean["allowed_exceptions"] = [1, 2, 3]

    for key in (
        "device_default_shift", "shift_logic", "shift_boundaries", "auto_shift_allowed_by_device",
        "single_punch_grace_by_shift", "single_punch_correction_by_device"
    ):
        value = data.get(key, existing.get(key, DEFAULT_CONFIG[key]))
        clean[key] = value if isinstance(value, dict) else DEFAULT_CONFIG[key]
    clean_grace: dict[str, int] = {}
    for shift_name, minutes in clean["single_punch_grace_by_shift"].items():
        parsed_minutes = int(minutes)
        if not 0 <= parsed_minutes <= 1440:
            raise ValueError(f"Single-punch grace for '{shift_name}' must be between 0 and 1440 minutes.")
        clean_grace[str(shift_name)] = parsed_minutes
    clean["single_punch_grace_by_shift"] = clean_grace
    ip_to_device = {str(item["ip"]): str(item["device_id"]) for item in clean_devices}
    for ip, shifts in clean["auto_shift_allowed_by_device"].items():
        device_id = ip_to_device.get(str(ip))
        if not device_id or not isinstance(shifts, list):
            continue
        for shift in shifts:
            clean_shift = str(shift).strip()
            if clean_shift and device_id not in shift_map.setdefault(clean_shift, []):
                shift_map[clean_shift].append(device_id)
    clean["shift_type_device_mapping"] = [
        {"shift_type_name": shift, "related_device_id": ids}
        for shift, ids in shift_map.items()
    ]
    clean["auto_shift_detection_enabled"] = bool(data.get(
        "auto_shift_detection_enabled", existing.get("auto_shift_detection_enabled", False)
    ))
    clean["auto_shift_excluded_ips"] = list(data.get(
        "auto_shift_excluded_ips", existing.get("auto_shift_excluded_ips", [])
    ) or [])
    clean["auto_shift_max_distance_minutes"] = int(data.get(
        "auto_shift_max_distance_minutes", existing.get("auto_shift_max_distance_minutes", 240)
    ))
    clean["auto_create_shift_assignment"] = bool(data.get(
        "auto_create_shift_assignment", existing.get("auto_create_shift_assignment", True)
    ))
    clean["auto_created_shift_assignment_prefix"] = str(data.get(
        "auto_created_shift_assignment_prefix", existing.get("auto_created_shift_assignment_prefix", "AUTO-SYNC")
    ))
    clean["sync_device_time_with_pc"] = bool(data.get(
        "sync_device_time_with_pc", existing.get("sync_device_time_with_pc", True)
    ))
    clean["auto_correct_single_punches"] = bool(data.get(
        "auto_correct_single_punches", existing.get("auto_correct_single_punches", True)
    ))
    known_device_ids = {item["device_id"] for item in clean_devices}
    clean["single_punch_correction_by_device"] = {
        str(device_id): bool(enabled)
        for device_id, enabled in clean["single_punch_correction_by_device"].items()
        if str(device_id) in known_device_ids
    }
    return clean


def save_config(data: dict[str, Any]) -> dict[str, Any]:
    existing = load_config(include_secret=True)
    clean = validate_config(data, existing)
    assignments = [
        ("ERPNEXT_API_KEY", clean["erpnext_api_key"]),
        ("ERPNEXT_API_SECRET", clean["erpnext_api_secret"]),
        ("ERPNEXT_URL", clean["erpnext_url"]),
        ("ERPNEXT_VERSION", clean["erpnext_version"]),
        ("PULL_FREQUENCY", clean["pull_frequency"]),
        ("LOGS_DIRECTORY", "logs"),
        ("IMPORT_START_DATE", clean["import_start_date"]),
        ("IMPORT_END_DATE", clean["import_end_date"]),
        ("devices", clean["devices"]),
        ("shift_type_device_mapping", clean["shift_type_device_mapping"]),
        ("allowed_exceptions", clean["allowed_exceptions"]),
        ("DEVICE_DEFAULT_SHIFT", clean["device_default_shift"]),
        ("SHIFT_LOGIC", clean["shift_logic"]),
        ("SHIFT_BOUNDARIES", clean["shift_boundaries"]),
        ("AUTO_SHIFT_DETECTION_ENABLED", clean["auto_shift_detection_enabled"]),
        ("AUTO_SHIFT_EXCLUDED_IPS", clean["auto_shift_excluded_ips"]),
        ("AUTO_SHIFT_MAX_DISTANCE_MINUTES", clean["auto_shift_max_distance_minutes"]),
        ("AUTO_SHIFT_ALLOWED_BY_DEVICE", clean["auto_shift_allowed_by_device"]),
        ("AUTO_CREATE_SHIFT_ASSIGNMENT", clean["auto_create_shift_assignment"]),
        ("AUTO_CREATED_SHIFT_ASSIGNMENT_PREFIX", clean["auto_created_shift_assignment_prefix"]),
        ("SYNC_DEVICE_TIME_WITH_PC", clean["sync_device_time_with_pc"]),
        ("AUTO_CORRECT_SINGLE_PUNCHES", clean["auto_correct_single_punches"]),
        ("SINGLE_PUNCH_CORRECTION_BY_DEVICE", clean["single_punch_correction_by_device"]),
        ("SINGLE_PUNCH_GRACE_BY_SHIFT", clean["single_punch_grace_by_shift"]),
    ]
    content = "# Generated by the ERPNext Biometric web dashboard.\n"
    content += "# Keep this file private; it contains your ERPNext API secret.\n\n"
    content += "\n".join(f"{name} = {value!r}" for name, value in assignments) + "\n"
    temp_path = CONFIG_PATH.with_suffix(".py.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, CONFIG_PATH)
    return load_config(include_secret=False)
