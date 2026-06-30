"""
AES-256-GCM encryption utilities for sensitive data (email passwords).
Uses ENCRYPTION_KEY from config (must be 32 bytes).
"""
import base64
import logging
import os
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from config import get_settings

logger = logging.getLogger(__name__)


def _get_encryption_key() -> bytes:
    """Get encryption key from settings (hex-encoded 32-byte key)"""
    settings = get_settings()
    key_hex = settings.encryption_key
    if not key_hex:
        raise ValueError("ENCRYPTION_KEY not set in environment variables")
    if len(key_hex) != 64:
        raise ValueError("ENCRYPTION_KEY must be a 64-character hex string (32 bytes)")
    try:
        return bytes.fromhex(key_hex)
    except ValueError as e:
        raise ValueError("ENCRYPTION_KEY must be a valid hexadecimal string") from e


def encrypt_password(plaintext: str) -> str:
    """
    Encrypt a plaintext password using AES-256-GCM.

    Returns:
        base64-encoded string: {nonce_b64}.{encrypted_b64}
    """
    if not plaintext:
        raise ValueError("Cannot encrypt empty string")

    key = _get_encryption_key()
    aesgcm = AESGCM(key)

    # Generate random 12-byte nonce for GCM
    nonce = os.urandom(12)
    # Encrypt — returns ciphertext + tag (16 bytes appended)
    encrypted = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

    # Store as: base64(nonce).base64(encrypted)
    nonce_b64 = base64.b64encode(nonce).decode("utf-8")
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    return f"{nonce_b64}.{encrypted_b64}"


def decrypt_password(ciphertext: str) -> str:
    """
    Decrypt a password encrypted with encrypt_password.

    Args:
        ciphertext: Format: "{nonce_b64}.{encrypted_b64}"

    Returns:
        Decrypted plaintext password
    """
    if not ciphertext:
        raise ValueError("Cannot decrypt empty string")

    key = _get_encryption_key()
    aesgcm = AESGCM(key)

    try:
        parts = ciphertext.split(".")
        if len(parts) != 2:
            raise ValueError("Invalid ciphertext format")

        nonce_b64, encrypted_b64 = parts
        nonce = base64.b64decode(nonce_b64)
        encrypted = base64.b64decode(encrypted_b64)

        decrypted = aesgcm.decrypt(nonce, encrypted, None)
        return decrypted.decode("utf-8")
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise ValueError("Failed to decrypt credentials") from e
