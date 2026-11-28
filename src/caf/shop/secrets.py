"""Encryption at rest for the few secrets the shop keeps. The implementation is `caf.core.vault` (the connectors read a
customer's stored credentials through it too); this name stays for the code that imports it from here."""

from __future__ import annotations

from ..core.vault import MASK, PREFIX, _fernet, available, decrypt, encrypt, is_encrypted, is_secret, mask, seal, unlock

__all__ = ["MASK", "PREFIX", "_fernet", "available", "decrypt", "encrypt", "is_encrypted", "is_secret", "mask", "seal", "unlock"]
