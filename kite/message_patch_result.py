from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessagePatchResult:
    ok: bool
    retryable: bool = False
    retry_after_seconds: float = 0.0

    @classmethod
    def success(cls) -> MessagePatchResult:
        return cls(ok=True)

    @classmethod
    def failure(cls) -> MessagePatchResult:
        return cls(ok=False)

    @classmethod
    def retry_later(cls, retry_after_seconds: float) -> MessagePatchResult:
        return cls(
            ok=False,
            retryable=True,
            retry_after_seconds=max(float(retry_after_seconds), 0.0),
        )
