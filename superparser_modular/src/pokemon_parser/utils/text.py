from __future__ import annotations

import re
import unicodedata
from typing import Optional


def clean_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_text(text: object) -> str:
    raw = clean_text(text).lower()
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_price_eur(text: object) -> Optional[float]:
    raw = clean_text(text)
    if not raw:
        return None

    match = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d{1,4}[.,]\d{2}|\d{1,4},-)", raw)
    if not match:
        return None

    token = match.group(1)
    if token.endswith(",-"):
        token = token[:-2]

    if "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(",", ".")

    try:
        value = float(token)
    except ValueError:
        return None

    return value if value > 0 else None
