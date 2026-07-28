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
- ``/archive``: stricter than ``/new``/``/switch`` — denied on an active
  prompt AND on queued prompts (upstream archive drains agents and silently
  cancels the queue; mvp-scope aligned item 15).
- ``/detach``: always allowed (it only pauses the Feishu push), but an active
  prompt earns an informational note — the work keeps running upstream.
- ``kitectl service stop|restart``: destructive-op preview. Verified busy /
  pending-interaction state makes the operation force-only; an UNVERIFIABLE
  live state (kap unreachable) is also force-only — it is never silently
  available (FOCUS ``*_FORCE_ONLY_BY_RUNTIME_UNVERIFIED``).
- all-mode group exclusivity (group-chat contract §2, fail-closed §4.6):
  ``/group-mode all`` and ``/switch``/``/new`` rebinds verify the all-mode
  group's session is not bound to any other attached chat and deny with a
  remediation text otherwise (FOCUS thread-access-policy port). The rule
  applies in both directions (§3.8): any other chat rebinding into a
  session an all-mode group already occupies is denied the same way.

Only normalized adapter types are consumed; kap wire knowledge stays in the
adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from kite.adapters.kap_server import PromptQueueState, SessionSummary
from kite.stores.binding_store import BindingStore
from kite.stores.group_config_store import GROUP_MODE_ALL, GroupConfigStore

# Reason codes (stable identifiers for logs/tests; the text is for users).
NEW_DENIED_BY_ACTIVE_PROMPT = "new_denied_by_active_prompt"
ARCHIVE_DENIED_BY_ACTIVE_PROMPT = "archive_denied_by_active_prompt"
ARCHIVE_DENIED_BY_QUEUED_PROMPT = "archive_denied_by_queued_prompt"
DETACH_NOTE_ACTIVE_PROMPT = "detach_note_active_prompt"
GROUP_ALL_MODE_SESSION_SHARED = "group_all_mode_session_shared"
GROUP_ALL_MODE_SESSION_OCCUPIED = "group_all_mode_session_occupied"

NEW_ACTIVE_PROMPT_REASON_TEXT = "当前有执行中的 prompt，请先 /abort 或等待完成。"
ARCHIVE_QUEUED_PROMPT_REASON_TEXT = (
    "当前有排队中的 prompt，归档会静默取消排队中的 prompt；请先 /abort 或等待完成。"
)
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


def check_archive(queue: PromptQueueState) -> ReasonedCheck:
    """`/archive` is stricter than `/new` and `/switch` (mvp-scope aligned
    item 15): upstream archive drains agents, so beyond the active prompt a
    QUEUED prompt is silently cancelled too (a `/switch` leaves the queue
    running) — deny on both."""
    if queue.active_prompt_id:
        return ReasonedCheck.deny(
            ARCHIVE_DENIED_BY_ACTIVE_PROMPT,
            NEW_ACTIVE_PROMPT_REASON_TEXT,
        )
    if queue.queued_prompt_ids:
        return ReasonedCheck.deny(
            ARCHIVE_DENIED_BY_QUEUED_PROMPT,
            ARCHIVE_QUEUED_PROMPT_REASON_TEXT,
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


def all_mode_session_exclusive(
    chat_id: str,
    binding_store: BindingStore,
    group_config_store: GroupConfigStore,
    *,
    session_id: Optional[str] = None,
    current_chat_mode: Optional[str] = None,
) -> ReasonedCheck:
    """All-mode group exclusivity probe (group-chat §2, fail-closed §4.6).

    An all-mode group's session may not be bound to any other chat (every
    member message becomes a prompt, so a shared session would leak one
    chat's noise into another). From the all-mode perspective — either the
    chat already is an activated all-mode group, or
    ``current_chat_mode=GROUP_MODE_ALL`` forces that perspective for a
    pending ``/group-mode all`` switch — deny when the session (the chat's
    current binding, or ``session_id`` for a /switch|/new target) is bound
    to ≥1 other ATTACHED chat. Detached chats are inert (they can neither
    prompt nor receive broadcasts), so only attached bindings count — that
    is exactly what makes the ``/detach`` remediation actionable. The
    denial names the other chats plus the remediation; it is never
    silently allowed.
    """
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        return ReasonedCheck.allow()
    mode = str(current_chat_mode or "").strip().lower()
    if not mode:
        config = group_config_store.load(normalized_chat_id)
        if config is not None and config["activated"]:
            mode = config["mode"]
    if mode != GROUP_MODE_ALL:
        # The exclusivity rule only bites for all-mode groups.
        return ReasonedCheck.allow()
    if session_id is None:
        binding = binding_store.load(normalized_chat_id)
        if binding is None:
            # No binding yet: first use creates an exclusive session.
            return ReasonedCheck.allow()
        session_id = binding["session_id"]
    other_chat_ids = sorted(
        candidate
        for candidate, candidate_binding in binding_store.load_all().items()
        if candidate != normalized_chat_id
        and candidate_binding["session_id"] == session_id
        and candidate_binding["attached"]
    )
    if not other_chat_ids:
        return ReasonedCheck.allow()
    return ReasonedCheck.deny(
        GROUP_ALL_MODE_SESSION_SHARED,
        _all_mode_session_shared_text(other_chat_ids),
    )


def _all_mode_session_shared_text(other_chat_ids: Sequence[str]) -> str:
    chats = "、".join(f"`{chat_id}`" for chat_id in other_chat_ids)
    return (
        "all 模式要求本群独占会话，但该会话还绑定着其他聊天"
        f"（{chats}），本次操作被拒绝。"
        "请先在那些聊天中发送 /detach（或将它们 /switch 到其他会话），"
        "或将本群 /switch 到一个未被共享的会话，然后重试。"
    )


def all_mode_session_occupied(
    chat_id: str,
    binding_store: BindingStore,
    group_config_store: GroupConfigStore,
    *,
    session_id: str,
) -> ReasonedCheck:
    """Reverse all-mode exclusivity (group-chat §3.8, fail-closed §4.6).

    The exclusivity rule applies in both directions: any chat (p2p or
    group) rebinding into a session a DIFFERENT chat already occupies as an
    activated, attached all-mode group is denied — otherwise the newcomer
    would start receiving (and sharing) every member message's prompt
    traffic. The denial names the occupying group(s) plus the remediation;
    it is never silently allowed.

    Edge rules, symmetric with the forward probe: a DETACHED occupier is
    inert (it can neither prompt nor receive broadcasts) and lifts the
    denial; the occupying group re-binding its OWN session is not a denial
    case here (that is the forward rule's business); a deactivated or
    corrupt-config record reads as non-all (store fail-closed convention),
    so it never occupies.
    """
    normalized_chat_id = str(chat_id or "").strip()
    normalized_session_id = str(session_id or "").strip()
    if not normalized_chat_id or not normalized_session_id:
        return ReasonedCheck.allow()
    occupier_chat_ids = sorted(
        candidate
        for candidate, candidate_binding in binding_store.load_all().items()
        if candidate != normalized_chat_id
        and candidate_binding["session_id"] == normalized_session_id
        and candidate_binding["attached"]
        and _is_all_mode_group(group_config_store, candidate)
    )
    if not occupier_chat_ids:
        return ReasonedCheck.allow()
    return ReasonedCheck.deny(
        GROUP_ALL_MODE_SESSION_OCCUPIED,
        _all_mode_session_occupied_text(occupier_chat_ids),
    )


def _is_all_mode_group(group_config_store: GroupConfigStore, chat_id: str) -> bool:
    config = group_config_store.load(chat_id)
    return bool(
        config is not None
        and config["activated"]
        and config["mode"] == GROUP_MODE_ALL
    )


def _all_mode_session_occupied_text(occupier_chat_ids: Sequence[str]) -> str:
    chats = "、".join(f"`{chat_id}`" for chat_id in occupier_chat_ids)
    return (
        f"该会话正被 all 模式的群聊（{chats}）独占，本次操作被拒绝。"
        "请先将该群的群聊模式切回 mention_only / assistant"
        "（在该群中发送 /group-mode），或在该群中发送 /detach，然后重试。"
    )


@dataclass(frozen=True, slots=True)
class ServiceStopPreview:
    """Live-state probe for `kitectl service stop|restart`.

    ``verifiable=False`` means kap could not be queried at all; the operation
    is then force-only (never silently available).
    """

    verifiable: bool
    busy_sessions: int = 0
    pending_interactions: int = 0

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
    verified_pending: Optional[int] = None,
) -> ServiceStopPreview:
    """Build the preview from a kap session list; ``None`` = unverifiable.

    ``verified_pending`` is the count from the real pending-approval/question
    lists (upstream's `pending_interaction` session flag can be stale —
    approvals expire server-side but the flag lingers); when omitted, the
    flag count is used as the conservative fallback.
    """
    if sessions is None:
        return ServiceStopPreview(verifiable=False)
    pending = (
        verified_pending
        if verified_pending is not None
        else sum(1 for session in sessions if session.pending_interaction)
    )
    return ServiceStopPreview(
        verifiable=True,
        busy_sessions=sum(1 for session in sessions if session.busy),
        pending_interactions=pending,
    )
