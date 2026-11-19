"""Envelope encryption helper for `project_secrets`, CI webhooks, and OTel headers (`SEC-be-1`).

Uses `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256) with a 32-byte key derived
from `Settings.secrets_encryption_key` (falling back to `Settings.artifact_url_secret` in
`MultiFernet` order for rotation compatibility). Fails closed when neither key is set.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import ValidationError

from fleetforge.config import Settings, get_settings


class SecretKeyNotConfiguredError(ValueError):
    """Raised when attempting to encrypt or decrypt secrets with no key configured."""


def _fernet_for(settings: Settings | None = None) -> MultiFernet:
    cfg = settings
    if cfg is None:
        try:
            cfg = get_settings()
        except ValidationError:
            cfg = None
    candidates: list[str] = []
    if cfg is not None:
        for raw in (cfg.secrets_encryption_key, cfg.artifact_url_secret):
            if raw and raw not in candidates:
                candidates.append(raw)
    if not candidates:
        raise SecretKeyNotConfiguredError(
            "secrets encryption key is not configured: set SECRETS_ENCRYPTION_KEY"
        )
    return MultiFernet(
        [Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest())) for k in candidates]
    )


def encrypt_secret(plaintext: str, settings: Settings | None = None) -> str:
    """Encrypt a plaintext string and return the ASCII Fernet token."""
    return _fernet_for(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, settings: Settings | None = None) -> str:
    """Decrypt a Fernet token back to its UTF-8 plaintext string."""
    try:
        return _fernet_for(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("invalid secret ciphertext") from exc
