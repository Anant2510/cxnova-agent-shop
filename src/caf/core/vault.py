"""Encryption at rest for the few secrets this layer keeps: a customer's model credential and the credentials of the
systems a customer connects.

The key comes from CAF_SECRET_KEY: either a Fernet key or any passphrase, which is stretched into one. Without it,
inline credentials are refused and a policy has to name an environment variable on the server instead
(``api_key_env``), which keeps the secret out of the database altogether.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any

PREFIX = "enc:v1:"
MASK = "***"   # what a stored secret reads as wherever a policy is shown; saved back, it means "keep what is stored"

# a setting whose name says it is a credential, and a value that carries one (a URL with user:password@host)
_SECRET_NAME = re.compile(r"(secret|token|password|passwd|api_?key|credentials|private_key|^pat$|^key$)", re.I)
_NOT_A_SECRET = ("_env", "_url", "_uri", "_endpoint", "_path", "_id", "_file", "_map", "_type")   # token_url, credentials_file ...
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^@/\s]+@", re.I)


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
        raise RuntimeError("CAF_SECRET_KEY is not set on this server, so a credential cannot be stored. Set it (any long "
                           "passphrase) and restart, or keep the credential in an environment variable on the server and "
                           "name that variable instead (api_key_env, <setting>_env)")
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


# ---- the settings of a connection: which of them are secrets, how they are kept and how they are read back -----------

def is_secret(name: str, value: Any = None) -> bool:
    """True for a setting that is kept encrypted and never shown: its name says credential (api_key, client_secret,
    management_token, password, pat, credentials_json ...) or its value is a URL that carries a password. A name that
    ends in `_env`, `_url`, `_id` and the like names where a secret is or what it is for, and is not one."""
    if isinstance(value, str) and _URL_WITH_PASSWORD.match(value):
        return True
    return bool(_SECRET_NAME.search(str(name))) and not str(name).lower().endswith(_NOT_A_SECRET)


def seal(config: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """The settings as they are stored: every secret encrypted. A secret that arrives as the mask keeps the value
    stored before (the settings were read, edited and saved back); with nothing stored before, that is refused."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        if value == MASK:   # whatever was shown as the mask comes back as the mask: a credential by name, or a URL with a password in it
            if not (previous or {}).get(key):
                raise ValueError(f"`{key}` is the mask ({MASK}) and nothing is stored for it yet: give the value itself")
            value = previous[key]
        elif is_secret(key, value) and value not in (None, "") and not is_encrypted(value if isinstance(value, str) else None):
            value = encrypt(value if isinstance(value, str) else json.dumps(value))
        out[key] = value
    return out


def mask(config: dict[str, Any]) -> dict[str, Any]:
    """The settings as they are shown: every secret, and anything stored encrypted, replaced by the mask."""
    return {k: (MASK if v not in (None, "") and (is_secret(k, v) or is_encrypted(v if isinstance(v, str) else None)) else v)
            for k, v in config.items()}


def unlock(config: dict[str, Any]) -> dict[str, Any]:
    """The settings as a connector reads them: secrets decrypted, and `<name>_env` replaced by the value of that
    environment variable on this server under `<name>`. Raises RuntimeError when a secret cannot be read here."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        if key.endswith("_env") and isinstance(value, str) and value.strip():
            found = os.environ.get(value.strip(), "")
            if not found:
                raise RuntimeError(f"this server has no environment variable {value.strip()} (named by `{key}`)")
            out[key[:-4]] = found
        elif isinstance(value, str) and is_encrypted(value):
            try:
                out[key] = decrypt(value)
            except RuntimeError:
                raise
            except Exception as exc:   # a key that is not the one this was encrypted with
                raise RuntimeError(f"`{key}` was encrypted with a different CAF_SECRET_KEY than this server has") from exc
        else:
            out[key] = value
    return out
