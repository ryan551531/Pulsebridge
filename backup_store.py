"""Password-encrypted setup backups for PulseBridge."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from auth_store import export_auth_data
from config_store import load_config

MAGIC = b"PULSEBRIDGE-BACKUP\x01"
SALT_SIZE = 16
MAX_BACKUP_SIZE = 10 * 1024 * 1024


def _key(password: str, salt: bytes) -> bytes:
    clean = str(password or "")
    if not 10 <= len(clean) <= 256:
        raise ValueError("Backup password must contain between 10 and 256 characters.")
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    ).derive(clean.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def create_backup(password: str) -> bytes:
    payload = {
        "format": "pulsebridge-backup",
        "version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "configuration": load_config(include_secret=True),
        "authentication": export_auth_data(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    salt = os.urandom(SALT_SIZE)
    return MAGIC + salt + Fernet(_key(password, salt)).encrypt(raw)


def open_backup(content: bytes, password: str) -> dict[str, Any]:
    if not content or len(content) > MAX_BACKUP_SIZE:
        raise ValueError("Choose a PulseBridge backup file smaller than 10 MB.")
    if not content.startswith(MAGIC) or len(content) <= len(MAGIC) + SALT_SIZE:
        raise ValueError("That file is not a valid PulseBridge backup.")
    offset = len(MAGIC)
    salt = content[offset:offset + SALT_SIZE]
    try:
        raw = Fernet(_key(password, salt)).decrypt(content[offset + SALT_SIZE:])
    except InvalidToken as exc:
        raise ValueError("The backup password is incorrect or the file is damaged.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The backup contents are damaged.") from exc
    if not isinstance(payload, dict) or payload.get("format") != "pulsebridge-backup" or payload.get("version") != 1:
        raise ValueError("This PulseBridge backup version is not supported.")
    if not isinstance(payload.get("configuration"), dict) or not isinstance(payload.get("authentication"), dict):
        raise ValueError("The backup is incomplete.")
    return payload
