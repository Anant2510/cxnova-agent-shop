"""Convert agent state to plain JSON-safe Python (numpy, pandas, dates, Decimals, NaN ...).

Applied to every agent's final state so results can always be validated, checkpointed by
LangGraph (msgpack), returned by the API (strict JSON) and embedded in the console.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import enum
import math
from pathlib import PurePath
from typing import Any

try:  # optional heavy deps
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None
try:
    import pandas as _pd
except Exception:  # pragma: no cover
    _pd = None


def _float(v: float) -> float | None:
    return None if (math.isnan(v) or math.isinf(v)) else float(v)


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    if _depth > 60:
        return str(obj)
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if _np is not None and isinstance(obj, _np.generic):
        if isinstance(obj, _np.bool_):
            return bool(obj)
        if isinstance(obj, _np.integer):
            return int(obj)
        if isinstance(obj, _np.floating):
            return _float(float(obj))
        return to_jsonable(obj.item(), _depth + 1)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return _float(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v, _depth + 1) for v in obj]
    if _np is not None and isinstance(obj, _np.ndarray):
        return [to_jsonable(v, _depth + 1) for v in obj.tolist()]
    if _pd is not None:
        if obj is _pd.NaT:
            return None
        if isinstance(obj, _pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, _pd.DataFrame):
            return to_jsonable(obj.to_dict(orient="records"), _depth + 1)
        if isinstance(obj, _pd.Series):
            return to_jsonable(obj.tolist(), _depth + 1)
        try:
            if _pd.isna(obj):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, decimal.Decimal):
        return _float(float(obj))
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value, _depth + 1)
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode()
    if hasattr(obj, "model_dump"):
        return to_jsonable(obj.model_dump(), _depth + 1)
    return str(obj)
