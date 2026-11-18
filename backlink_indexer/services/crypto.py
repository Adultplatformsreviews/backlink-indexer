"""Encrypt operator secrets at rest. The key never goes into the database."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    raw = (os.environ.get("ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise EncryptionError("ENCRYPTION_KEY is not set.")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_text(value: str) -> str:
    if value is None:
        value = ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, EncryptionError, ValueError) as exc:
        raise EncryptionError("Could not decrypt a stored secret.") from exc
