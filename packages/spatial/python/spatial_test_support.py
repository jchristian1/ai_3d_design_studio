"""Test support: load the shared language-neutral spatial corpus.

JSON cannot encode NaN or Infinity, so the corpus uses string sentinels which
each language decodes into its own non-finite values. Both representations
therefore exercise identical inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).resolve().parents[1] / "spatial-cases.json"


def load_spatial_cases() -> dict[str, Any]:
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def decode_value(value: Any) -> Any:
    """Decode corpus sentinels into real non-finite values."""
    if value == "__NAN__":
        return float("nan")
    if value == "__INF__":
        return float("inf")
    if value == "__NEG_INF__":
        return float("-inf")
    return value
