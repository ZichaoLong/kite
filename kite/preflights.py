"""Reason-coded preflight checks for in-flight-work-sensitive operations.

Ported from FOCUS's ``runtime_admin_controller.py`` discipline
(docs/research/focus-assets-map.md §1 REWORK row): operations whose blast
radius covers in-flight work must say *why* they refuse or warn, in a
structured ``{allowed, reason_code, reason_text}`` form — never a bare
stringly-typed branch buried in a command handler.

Covered here:

- ``/new``: denied while the bound session has an active prompt (fail-closed:
  a rebind would orphan the in-flight prompt's card/approval visibility;
  mvp-scope aligned item 7).
- ``/detach``: always allowed (it only pauses the Feishu push), but an active
  prompt earns an informational note — the work keeps running upstream.
- ``kitectl service stop|restart``: destructive-op preview. Verified busy /
  pending-interaction state makes the operation force-only; an UNVERIFIABLE
  live state (kap unreachable) is also force-only — it is never silently
  available (FOCUS ``*_FORCE_ONLY_BY_RUNTIME_UNVERIFIED``).

Only normalized adapter types are consumed; kap wire knowledge stays in the
adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from kite.adapters.kap_server import PromptQueueState, SessionSummary

# Reason codes (stable identifiers for logs/tests; the text is for users).
NEW_DENIED_BY_ACTIVE_PROMPT = "new_denied_by_active_prompt"
DETACH_NOTE_ACTIVE_PROMPT = "detach_note_active_prompt"

NEW_ACTIVE_PROMPT_REASON_TEXT = "当前有执行中的 prompt，请先 /abort 或等待完成。"
DETACH_ACTIVE_PROMPT_NOTE = "执行中的 prompt 仍在继续，推送已暂停。"


@dataclass(frozen=True, slots=True)
class ReasonedCheck:
    """The outcome of a preflight: allow (optionally with a note) or deny
    with a machine-readable reason code plus user-facing reason text."""

    allowed: bool
    reason_code: str = ""
    reason_text: str = ""
    note: str = ""

    @classmethod
    def allow(cls, *, note: str = "", reason_code: str = "") -> "ReasonedCheck":
        return cls(allowed=True, reason_code=reason_code, note=note)

    @classmethod
    def deny(cls, reason_code: str, reason_text: str) -> "ReasonedCheck":
        return cls(
            allowed=False,
            reason_code=str(reason_code or "").strip(),
            reason_text=str(reason_text or "").strip(),
        )


def check_new(queue: PromptQueueState) -> ReasonedCheck:
    """`/new` rebinds the chat to a fresh session. With an active prompt on
    the old session the rebind would strand its execution card, terminal
    result and approval routing — refuse (fail-closed)."""
    if queue.active_prompt_id:
        return ReasonedCheck.deny(
            NEW_DENIED_BY_ACTIVE_PROMPT,
            NEW_ACTIVE_PROMPT_REASON_TEXT,
        )
    return ReasonedCheck.allow()


def check_detach(queue: PromptQueueState) -> ReasonedCheck:
    """`/detach` only flips a local push flag, so it is never denied; an
    active prompt merely earns the note that the work continues unseen."""
    if queue.active_prompt_id:
        return ReasonedCheck.allow(
            reason_code=DETACH_NOTE_ACTIVE_PROMPT,
            note=DETACH_ACTIVE_PROMPT_NOTE,
        )
    return ReasonedCheck.allow()


@dataclass(frozen=True, slots=True)
class ServiceStopPreview:
    """Live-state probe for `kitectl service stop|restart`.

    ``verifiable=False`` means kap could not be queried at all; the operation
    is then force-only (never silently available). ``kited_running`` is
    observability for the preview message (from runtime_status.json).
    """

    verifiable: bool
    busy_sessions: int = 0
    pending_interactions: int = 0
    kited_running: Optional[bool] = None

    @property
    def force_only(self) -> bool:
        if not self.verifiable:
            return True
        return self.busy_sessions > 0 or self.pending_interactions > 0

    def preview_text(self, verb: str) -> str:
        """One-line preview; ``verb`` is e.g. "restarting" / "stopping"."""
        if not self.verifiable:
            return (
                f"cannot verify live state (kap-server unreachable); "
                f"{verb} may kill in-flight prompts"
            )
        return (
            f"{self.busy_sessions} session(s) busy, "
            f"{self.pending_interactions} pending interaction(s); "
            f"{verb} kills in-flight prompts"
        )


def preview_service_stop(
    sessions: Optional[Sequence[SessionSummary]],
    *,
    kited_running: Optional[bool],
) -> ServiceStopPreview:
    """Build the preview from a kap session list; ``None`` = unverifiable."""
    if sessions is None:
        return ServiceStopPreview(verifiable=False, kited_running=kited_running)
    return ServiceStopPreview(
        verifiable=True,
        busy_sessions=sum(1 for session in sessions if session.busy),
        pending_interactions=sum(1 for session in sessions if session.pending_interaction),
        kited_running=kited_running,
    )
