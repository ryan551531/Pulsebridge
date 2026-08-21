"""Local authentication and branding storage for PulseBridge."""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from config_store import ROOT

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "pulsebridge-auth.db"
SECRET_PATH = DATA_DIR / ".session-secret"
DEFAULT_BRANDING = {
    "app_name": "PulseBridge",
    "logo_data": "",
    "footer_text": "Developed by Ryan Brown",
    "theme_preset": "pulse",
    "theme_mode": "light",
}


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize() -> None:
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        db.executemany("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", DEFAULT_BRANDING.items())
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def session_secret() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SECRET_PATH.exists():
        SECRET_PATH.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            os.chmod(SECRET_PATH, 0o600)
        except OSError:
            pass
    return SECRET_PATH.read_text(encoding="utf-8").strip()


def branding() -> dict[str, str]:
    with _connect() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    values = dict(DEFAULT_BRANDING)
    values.update({row["key"]: row["value"] for row in rows})
    return values


def save_branding(values: dict[str, str]) -> dict[str, str]:
    current = branding()
    theme_preset = str(values.get("theme_preset", current["theme_preset"])).strip().lower()
    theme_mode = str(values.get("theme_mode", current["theme_mode"])).strip().lower()
    if theme_preset not in {"pulse", "logo", "ocean", "royal", "sunset"}:
        raise ValueError("Choose a valid color theme.")
    if theme_mode not in {"light", "dark"}:
        raise ValueError("Choose light or dark appearance.")
    clean = {
        "app_name": str(values.get("app_name", "")).strip()[:60] or DEFAULT_BRANDING["app_name"],
        "footer_text": str(values.get("footer_text", "")).strip()[:160] or DEFAULT_BRANDING["footer_text"],
        "logo_data": str(values.get("logo_data", "")).strip(),
        "theme_preset": theme_preset,
        "theme_mode": theme_mode,
    }
    if clean["logo_data"] and not clean["logo_data"].startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
        raise ValueError("Logo must be a PNG, JPEG, or WebP image.")
    if len(clean["logo_data"]) > 2_100_000:
        raise ValueError("Logo must be smaller than 1.5 MB.")
    with _connect() as db:
        db.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", clean.items())
    return clean


def list_users() -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute("SELECT id, username, role, active, created_at, updated_at FROM users ORDER BY username").fetchall()
    return [dict(row) for row in rows]


def get_user(user_id: int) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT id, username, role, active FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username.strip(),)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return None
    return {key: row[key] for key in ("id", "username", "role", "active")}


def save_user(payload: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "user"))
    active = 1 if payload.get("active", True) else 0
    if len(username) < 3 or len(username) > 50:
        raise ValueError("Username must be between 3 and 50 characters.")
    if role not in {"admin", "user"}:
        raise ValueError("Role must be admin or user.")
    if user_id is None and len(password) < 10:
        raise ValueError("Password must contain at least 10 characters.")
    if password and len(password) < 10:
        raise ValueError("Password must contain at least 10 characters.")
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    try:
        with _connect() as db:
            if user_id is None:
                cursor = db.execute(
                    "INSERT INTO users(username, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, generate_password_hash(password), role, active, now, now),
                )
                user_id = int(cursor.lastrowid)
            elif password:
                db.execute("UPDATE users SET username=?, password_hash=?, role=?, active=?, updated_at=? WHERE id=?",
                           (username, generate_password_hash(password), role, active, now, user_id))
            else:
                db.execute("UPDATE users SET username=?, role=?, active=?, updated_at=? WHERE id=?",
                           (username, role, active, now, user_id))
    except sqlite3.IntegrityError as exc:
        raise ValueError("That username is already in use.") from exc
    user = get_user(int(user_id))
    if not user:
        raise ValueError("User was not found.")
    return user


def delete_user(user_id: int) -> None:
    with _connect() as db:
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def export_auth_data() -> dict[str, Any]:
    """Export accounts and branding settings without exposing plaintext passwords."""
    with _connect() as db:
        users = db.execute(
            "SELECT id, username, password_hash, role, active, created_at, updated_at FROM users ORDER BY id"
        ).fetchall()
        settings = db.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {
        "users": [dict(row) for row in users],
        "settings": {row["key"]: row["value"] for row in settings},
    }


def restore_auth_data(payload: dict[str, Any]) -> None:
    """Replace local accounts and branding from a validated backup payload."""
    if not isinstance(payload, dict):
        raise ValueError("The backup account data is invalid.")
    raw_users = payload.get("users")
    raw_settings = payload.get("settings")
    if not isinstance(raw_users, list) or not isinstance(raw_settings, dict):
        raise ValueError("The backup account data is incomplete.")

    users: list[tuple[Any, ...]] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for item in raw_users:
        if not isinstance(item, dict):
            raise ValueError("The backup contains an invalid user account.")
        user_id = int(item.get("id"))
        username = str(item.get("username") or "").strip()
        password_hash = str(item.get("password_hash") or "")
        role = str(item.get("role") or "")
        active = 1 if item.get("active") else 0
        created_at = str(item.get("created_at") or "")
        updated_at = str(item.get("updated_at") or "")
        if user_id <= 0 or user_id in seen_ids or username.lower() in seen_names:
            raise ValueError("The backup contains duplicate user accounts.")
        if not 3 <= len(username) <= 50 or role not in {"admin", "user"}:
            raise ValueError("The backup contains an invalid user account.")
        if not 20 <= len(password_hash) <= 1024 or not created_at or not updated_at:
            raise ValueError("The backup contains invalid account credentials.")
        seen_ids.add(user_id)
        seen_names.add(username.lower())
        users.append((user_id, username, password_hash, role, active, created_at, updated_at))

    settings = dict(DEFAULT_BRANDING)
    for key in DEFAULT_BRANDING:
        if key in raw_settings:
            settings[key] = str(raw_settings[key])
    if settings["theme_preset"] not in {"pulse", "logo", "ocean", "royal", "sunset"}:
        raise ValueError("The backup contains an invalid color theme.")
    if settings["theme_mode"] not in {"light", "dark"}:
        raise ValueError("The backup contains an invalid appearance setting.")
    if settings["logo_data"] and not settings["logo_data"].startswith(
        ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")
    ):
        raise ValueError("The backup contains an invalid logo.")
    if len(settings["logo_data"]) > 2_100_000:
        raise ValueError("The backup logo is too large.")

    with _connect() as db:
        db.execute("DELETE FROM users")
        db.execute("DELETE FROM settings")
        if users:
            db.executemany(
                "INSERT INTO users(id, username, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                users,
            )
        db.executemany("INSERT INTO settings(key, value) VALUES (?, ?)", settings.items())
