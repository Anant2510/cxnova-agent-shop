"""One way out to the network for every connector (the model, reference sources, the repository).

HTTPS is verified against certifi's CA bundle when certifi is installed, else the system's. A python.org build of
Python on macOS has no system bundle until its "Install Certificates" step is run, so certifi is a dependency.
"""
from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def urlopen(req: urllib.request.Request | str, timeout: float = 30.0) -> Any:
    url = req if isinstance(req, str) else req.full_url
    if url.lower().startswith("https://"):
        return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
    return urllib.request.urlopen(req, timeout=timeout)
