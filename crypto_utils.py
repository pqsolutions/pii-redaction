"""
AES encrypt/decrypt compatible with the legacy .NET helper:

    private string Decrypt(string cipherText) { ... Rfc2898DeriveBytes(EncryptionKey, salt) ... }
    private string Encrypt(string clearText)  { ... }

Key derivation: PBKDF2-HMAC-SHA1(password="MAKV2SPBNI99212", salt="Ivan Medvedev", iterations=1000)
  -> first 32 bytes = AES-256 key, next 16 bytes = IV (single continuous derived stream,
     matching .NET's Rfc2898DeriveBytes.GetBytes(32) followed by GetBytes(16)).
Cipher: AES-CBC, PKCS7 padding, plaintext encoded as UTF-16LE (.NET Encoding.Unicode).
"""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

_ENCRYPTION_KEY = "MAKV2SPBNI99212"
_SALT = bytes([0x49, 0x76, 0x61, 0x6e, 0x20, 0x4d, 0x65, 0x64, 0x76, 0x65, 0x64, 0x65, 0x76])  # "Ivan Medvedev"
_ITERATIONS = 1000


def _derive_key_iv() -> tuple[bytes, bytes]:
    derived = hashlib.pbkdf2_hmac("sha1", _ENCRYPTION_KEY.encode("utf-8"), _SALT, _ITERATIONS, dklen=48)
    return derived[:32], derived[32:48]


def decrypt(cipher_text: str) -> str:
    """Decrypt a base64 AES-CBC blob produced by the .NET Encrypt() helper."""
    key, iv = _derive_key_iv()
    cipher_bytes = base64.b64decode(cipher_text)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    clear_bytes = unpadder.update(padded) + unpadder.finalize()
    return clear_bytes.decode("utf-16-le")


def encrypt(clear_text: str) -> str:
    """Encrypt a string using the same scheme as the .NET Encrypt() helper."""
    key, iv = _derive_key_iv()
    clear_bytes = clear_text.encode("utf-16-le")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(clear_bytes) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher_bytes = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(cipher_bytes).decode("ascii")


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt raw bytes (e.g. an original image kept for redaction rehydration)
    using the same app-wide AES key as encrypt()/decrypt(), but with a random
    IV per call (prepended to the output) instead of the fixed derived IV —
    unlike the .NET-compatible text scheme above, this format has no external
    compatibility constraint to satisfy."""
    key, _ = _derive_key_iv()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher_bytes = encryptor.update(padded) + encryptor.finalize()
    return iv + cipher_bytes


def decrypt_bytes(blob: bytes) -> bytes:
    """Decrypt bytes produced by encrypt_bytes()."""
    key, _ = _derive_key_iv()
    iv, cipher_bytes = blob[:16], blob[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()
