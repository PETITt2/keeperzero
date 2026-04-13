"""
Helpers de validation d'environnement pour KEEPER-ZERO.
"""
from __future__ import annotations

import os
import re
from typing import Optional


_PK_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_private_key(raw: str) -> Optional[str]:
    """
    Normalise une clé privée hex en format 0x... (64 hex chars).
    Retourne None si invalide.
    """
    if not raw:
        return None
    pk = raw.strip()
    if pk.lower().startswith("0x"):
        pk = pk[2:]
    if not _PK_HEX_RE.fullmatch(pk):
        return None
    return "0x" + pk


def is_placeholder_rpc(url: str) -> bool:
    if not url:
        return True
    bad_tokens = ["VOTRE_CLE", "TA_CLE", "YOUR_KEY", "YOUR-KEY"]
    upper = url.upper()
    return any(tok in upper for tok in bad_tokens)


def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)
