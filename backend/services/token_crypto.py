"""AES-CBC encryption for stored session tokens at rest.

Uses a machine-derived key (hostname + fixed salt) so tokens are encrypted
on disk but don't need a user-supplied password.
"""
import base64
import hashlib
import socket

_KEY_CACHE: bytes | None = None


class CookieDecryptError(ValueError):
    """Raised when a stored ciphertext cannot be decrypted (corrupt or foreign)."""


def _derive_key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    raw = socket.gethostname() + "::vodrip::token::v1"
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

    Raises CookieDecryptError on any decryption failure — callers must not
    treat undecryptable ciphertext as a cookie value.
    """
    if ciphertext is None:
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken

        key = base64.urlsafe_b64encode(_derive_key())
        f = Fernet(key)
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CookieDecryptError("invalid token ciphertext") from exc
    except Exception as exc:
        raise CookieDecryptError(f"decryption failed: {exc}") from exc
