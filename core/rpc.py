
"""
Helpers pour gerer plusieurs RPCs et basculer en cas de panne.
"""
from __future__ import annotations

from typing import List, Optional


class RpcPool:
    def __init__(self, urls: List[str]):
        cleaned = [u.strip() for u in urls if u and u.strip()]
        self._urls = cleaned
        self._idx = 0

    def has_any(self) -> bool:
        return len(self._urls) > 0

    def current(self) -> Optional[str]:
        if not self._urls:
            return None
        return self._urls[self._idx]

    def next(self) -> Optional[str]:
        if not self._urls:
            return None
        self._idx = (self._idx + 1) % len(self._urls)
        return self._urls[self._idx]

    def all(self) -> List[str]:
        return list(self._urls)
