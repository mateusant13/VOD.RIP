"""AES-CBC encryption for OAuth tokens at rest.

Uses a machine-derived key (hostname + fixed salt) so tokens are encrypted
on disk but don't need a user-supplied password.
"""
import base64
import hashlib
import socket

_KEY_CACHE: bytes | None = None


def _derive_key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    raw = socket.gethostname() + "::vodrip::oauth::v1"
    _KEY_CACHE = hashlib.sha256(raw.encode()).digest()
    return _KEY_CACHE


def encrypt_token(plaintext: str | None) -> str | None:
    """Return base64-encoded AES-CBC ciphertext, or None if input is None."""
    if plaintext is None:
        return None
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(_derive_key())
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str | None) -> str | None:
    """Return decrypted plaintext, or None if input is None.

    Backward compatible: if decryption fails, the input is returned as-is
    so old unencrypted tokens still load correctly.
    """
    if ciphertext is None:
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken

        key = base64.urlsafe_b64encode(_derive_key())
        f = Fernet(key)
        return f.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        return ciphertext
