"""Encryption at rest for the few secrets the shop keeps (a customer's model credential).

The key comes from CAF_SECRET_KEY: either a Fernet key or any passphrase, which is stretched into one. Without it,
inline credentials are refused and a policy has to name an environment variable on the server instead
(``api_key_env``), which keeps the secret out of the database altogether.
"""

from __future__ import annotations

import base64
import hashlib
import os

PREFIX = "enc:v1:"


def _fernet():
    from cryptography.fernet import Fernet

    raw = os.environ.get("CAF_SECRET_KEY", "")
    if not raw:
        return None
    try:
        return Fernet(raw.encode())
    except Exception:
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def available() -> bool:
    return _fernet() is not None


def encrypt(value: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("CAF_SECRET_KEY is not set on this server: store the credential in an environment variable and "
                           "name it in the policy as api_key_env instead")
    return PREFIX + f.encrypt(value.encode()).decode()


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def decrypt(value: str) -> str:
    if not is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        raise RuntimeError("CAF_SECRET_KEY is not set: this credential cannot be decrypted")
    return f.decrypt(value[len(PREFIX):].encode()).decode()
