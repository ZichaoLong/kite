"""Sender display-name resolution with TTL caching (group-chat UX).

open_ids are unreadable; group-facing notices (approval/question routing
hints) should name people. Names are resolved lazily through the Feishu
contact API and cached; lookup failure never blocks anything — the fallback
is a shortened open_id. This is a disposable read-through cache: no state
axis, nothing to rebuild after a restart (kite-design §4).
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class IdentityNames:
    """open_id -> display name cache.

    ``fetcher`` does one upstream lookup (``FeishuTransport.fetch_user_name``)
    and returns None on failure. Positive results cache for ``ttl_seconds``,
    failures negative-cache for ``negative_ttl_seconds`` (transient contact
    API errors must not stampede). ``name_of`` never raises.
    """

    def __init__(
        self,
        fetcher: Callable[[str], Optional[str]],
        *,
        ttl_seconds: float = 21600.0,
        negative_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = float(ttl_seconds)
        self._negative_ttl = float(negative_ttl_seconds)
        self._clock = clock
        # open_id -> (expires_at, name or None when negative-cached)
        self._cache: dict[str, tuple[float, Optional[str]]] = {}

    def name_of(self, open_id: str) -> str:
        """The display name, or a short fallback when unresolvable."""
        normalized = str(open_id or "").strip()
        if not normalized:
            return "未知用户"
        now = self._clock()
        cached = self._cache.get(normalized)
        if cached is not None and cached[0] > now:
            return cached[1] if cached[1] is not None else self._fallback(normalized)
        try:
            name = self._fetcher(normalized)
        except Exception:  # noqa: BLE001 - name lookup must never break a flow
            name = None
        if isinstance(name, str) and name.strip():
            resolved = name.strip()
            self._cache[normalized] = (now + self._ttl, resolved)
            return resolved
        self._cache[normalized] = (now + self._negative_ttl, None)
        return self._fallback(normalized)

    @staticmethod
    def _fallback(open_id: str) -> str:
        return open_id[:13] + "…" if len(open_id) > 13 else open_id
