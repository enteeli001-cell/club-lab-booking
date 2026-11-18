"""Fernet-encrypted recoverable copy of staff passwords for superadmin viewing.

Plaintext is never written to logs. The key lives in data/pw_view_key (mode 600)
under the app's DATA_DIR (pytest uses a temporary directory).
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.secretstore import write_secret_file


def _key_path() -> Path:
    from flask import current_app, has_app_context
    from app.paths import DATA_DIR

    if has_app_context():
        return Path(current_app.config["DATA_DIR"]) / "pw_view_key"
    return DATA_DIR / "pw_view_key"


def _fernet() -> Fernet:
    path = _key_path()
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip().encode("ascii")
        return Fernet(raw)
    raw = Fernet.generate_key()
    write_secret_file(path, raw.decode("ascii"))
    return Fernet(raw)


def encrypt_password(password: str) -> str | None:
    if not password:
        return None
    return _fernet().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None
