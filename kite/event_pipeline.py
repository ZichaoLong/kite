"""Outbound path: kap durable events -> Feishu cards and pushes.

Implements the outbound half of the MVP contract
(docs/architecture/kite-design.md §5-6, docs/contracts/mvp-scope.md §3-4):

- turn.started creates one single-anchor execution card per attached chat
  bound to the session (prompt attribution comes from kap's prompt FIFO: the
  turn belongs to the active prompt, learned via ``GET .../prompts`` — the
  wire event carries no prompt id); tool.call.* and queue-depth changes patch
  only the anchor-matching card; turn.ended / prompt.aborted send a separate
  terminal card (text persisted in the terminal result store) and freeze the
  execution card. The terminal text fetch is snapshot-authoritative with
  retry-on-empty (the turn-end-vs-final-flush race is normal): an empty read
  is retried via the timer factory before the card falls back to stub text,
  and delivery is deduped per (session, prompt) so a second finalize path
  always skips (never two terminal cards, never stale text over real text).
- approval.requested routes by prompt ownership: a certain owner gets the
  three-button card, other attached chats get a read-only notice, and a
  best-effort/unknown owner gets an explicitly expired card (fail-closed,
  §4.6 — never routed on a guess) AND an upstream resolve-as-rejected
  (audit M2: upstream approvals never expire, so a card-only close-out
  would block the turn forever; the closed id is recorded so replays and
  rebuilds never re-card or re-resolve). approval.resolved from ANY client freezes
  the card. Card clicks are two-phase guarded (pending -> processing ->
  resolved; a mid-flight second click gets a "正在处理中" notice, a click on
  a missing entry gets "已失效或已处理" — never an error, never a
  double-submit) and actor-gated (group-chat §3.3: initiator or admin only;
  a bystander click is a denial toast with no state change and no upstream
  call). An approval unanswered for ``approval_timeout_seconds`` is
  resolved to upstream as rejected and the initiator is notified — never
  auto-approved (§3).
- question.requested renders one option-button card per question item to the
  owner chat (group-chat §3.9); the numbered-reply text pass-through stays as
  the fallback surface (claimed via ``try_handle_interaction_reply`` — in
  groups, only from the initiator or an admin, the same actor rule). Button
  clicks are actor-gated identically to approvals (bystander → denial toast,
  no state change, no upstream call), answer over REST, and freeze the
  clicked card; question.answered/dismissed from ANY client freezes every
  item card. Timeout auto-dismisses and patches the cards closed. An
  unroutable question (unattributable or non-certain ownership) is closed
  out with an expired notice AND an upstream dismiss (audit M2, same
  fail-closed discipline as approvals).
- fail-close sweep: /new /switch unbinds sweep the old session's pending
  approvals/questions routed to that chat, and kited shutdown sweeps all of
  them — responded upstream (approval rejected, question dismissed) and the
  cards patched to expired/closed (locally even when kap is unreachable).
- resync_required / startup recovery rebuilds from REST snapshot + prompts
  and refreshes work state / queue / in-flight cards wholesale; a failed
  rebuild freezes the session's execution cards as "状态未知" with a
  `kitectl session status` hint and never guesses (§4.2-4.3). A successful
  rebuild fires the snapshot-rebuilt hook, which kited uses to re-subscribe
  the session when its ack-listed resync had established no server-side
  subscription (audit M7).
- volatile streaming (docs/contracts/streaming-cards.md): assistant.delta
  frames append into a per-prompt in-memory transcript and schedule
  coalesced patches of the execution card's streamed body through the
  CardPatchDispatcher (Feishu RTT never blocks the loop; renders hop back
  onto it). An offset gap jumps straight to the snapshot-rebuild path
  (never guess the missing text), turn.ended reconciles the authoritative
  text over the deltas (never shrink), and an over-budget or undeliverable
  terminal card falls back to plain text once. Volatile text is
  enhancement, never evidence: with no deltas the durable path alone still
  produces correct cards.
- agent routing (mvp-scope aligned item 13, audit N3-HIGH-1): every
  normalized event carries the emitting ``agent_id``; events of the main
  agent ("" / "main" / missing) take the card pipeline above, while a
  `/btw` side-channel agent's events take a lightweight path — no
  execution card is created or taken over, the main card's stream is never
  touched, error frames end only the side turn's own tracking, and the
  side answer accumulates from its own volatile deltas (keyed by
  (session_id, agent_id)) and is delivered as a plain-text "旁路回复" to
  the initiating chat on turn.ended (a completion note when the stream is
  empty — never a hijack of the main prompt's terminal).

Threading: every mutation runs on the RuntimeLoop. WS callbacks
(``handle_event`` / ``handle_resync_required`` / ``handle_volatile``), timer
callbacks, and kited's startup recovery all hop onto the loop; blocking REST
inside handlers follows the same pattern as the inbound path (app_handler).

Only normalized adapter types are consumed here; kap wire schema knowledge
stays in kite/adapters/kap_server.py. The few REST paths the adapter does not
type (approval/question resolve, dismiss, messages) live in
``KapInteractionOps`` below — same discipline as app_handler.KapSessionOps.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from kite import cards
from kite.adapters.kap_server import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantDelta,
    DurableEvent,
    KapError,
    KapErrorFrame,
    KapEvent,
    KapTransportError,
    PromptAborted,
    PromptSteered,
    QuestionItemView,
    QuestionRequested,
    QuestionResolved,
    ResyncRequest,
    SessionSnapshot,
    SessionWorkChanged,
    ToolCallResult,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    normalize_durable_event,
)
from kite.app_handler import AppHandler, KAP_ERROR_PROMPT_NOT_PENDING, KapSessionOps
from kite.cards import ExecutionCardAnchor
from kite.feishu_transport import CardAction, CardActionResponse, InboundMessage
from kite.message_patch_result import MessagePatchResult
from kite.patch_dispatcher import CardPatchDispatcher
from kite.prompt_ownership import CERTAINTY_CERTAIN, PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import BindingStore, StoredBinding
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.terminal_result_store import TerminalResultRecord, TerminalResultStore
from kite.streaming_transcript import DEFAULT_STREAM_REPLY_CHAR_LIMIT, StreamingTranscript

logger = logging.getLogger("kite.outbound")

# kap business error codes this path depends on (upstream
# packages/kap-server/src/protocol/error-codes.ts).
KAP_ERROR_ALREADY_RESOLVED = 40902
KAP_ERROR_APPROVAL_NOT_FOUND = 40404
KAP_ERROR_QUESTION_NOT_FOUND = 40405
# Upstream quirk: a successful question dismiss replies with code 40909
# (QUESTION_DISMISSED is the *success* envelope, routes/questions.ts).
KAP_ERROR_QUESTION_DISMISSED = 40909

_MAX_TOOL_LINES = 30
# Head line once the cap evicted something (audit L6, FOCUS parity with
# ``_LOG_TRUNCATION_NOTICE``): the cap keeps the TAIL — the newest activity
# is never silently dropped — and this notice says so.
_TOOL_LINES_TRUNCATION_NOTICE = "**[工具调用已截断，仅保留最近部分]**"
# FIFO cap for the terminal-delivery dedup registry (audit L9).
_TERMINAL_DELIVERED_CAP = 1024
_LATEST_ASSISTANT_PAGE_SIZE = 20

# How long a side channel's just-ended marker stays fresh (audit R4-HIGH-1):
# upstream emits turn.ended(failed) and the trailing error frame for one
# failure in the same tick (loopService.ts), so the window only needs to
# outlive reconnect/replay churn — it never spans two real prompts.
_BTW_ENDED_MARK_SECONDS = 60.0

_KAP_UNREACHABLE_TOAST = "无法连接 kap-server，操作未完成，请稍后再试。"


def _is_main_agent(agent_id: Optional[str]) -> bool:
    """The main agent drives the card pipeline; any other id is a `/btw`
    side-channel agent (mvp-scope aligned item 13). An empty/missing agent
    id takes the main path (older frames, global events, defensive)."""
    return agent_id in (None, "", "main")

# Pending-approval click phases (interaction_request_controller discipline):
# a click flips pending -> processing before the REST resolve so a second
# click is a notice, never a double-submit; transport failure rolls back.
_APPROVAL_STATUS_PENDING = "pending"
_APPROVAL_STATUS_PROCESSING = "processing"

# Terminal reconcile defaults (execution_recovery_controller discipline):
# turn.ended routinely wins the race against the final message flush, so an
# empty terminal text is retried a few times before the stub-text fallback.
_DEFAULT_TERMINAL_EMPTY_RETRY_COUNT = 3
_DEFAULT_TERMINAL_EMPTY_RETRY_DELAY_SECONDS = 1.0

# Streaming defaults (streaming-cards contract §3.3/§3.7): per-card minimum
# patch interval 700 ms; the terminal card enforces the utf-8 byte budget
# before falling back to plain text (FOCUS terminal budget discipline).
_DEFAULT_STREAM_PATCH_INTERVAL_SECONDS = 0.7
_DEFAULT_TERMINAL_CARD_BYTE_BUDGET = 26000


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# ---------------------------------------------------------------------------
# REST ops for the interaction slice (paths/payload keys live only here)
# ---------------------------------------------------------------------------


class KapInteractionOps:
    """Typed view of the kap approval/question/messages REST surface.

    Same discipline as app_handler.KapSessionOps: envelope unwrapping and
    KapError/KapTransportError semantics stay in the adapter's KapRestClient;
    only wire paths and payload keys live here on the application side.
    """

    def __init__(self, rest: Any) -> None:
        self._rest = rest

    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        decision: str,
        feedback: str = "",
    ) -> None:
        body: dict[str, Any] = {"decision": decision}
        if feedback:
            body["feedback"] = feedback
        self._rest.call(
            "POST",
            f"/sessions/{_quote(session_id)}/approvals/{_quote(approval_id)}",
            body,
        )

    def answer_question(
        self,
        session_id: str,
        question_id: str,
        answers: Mapping[str, Any],
    ) -> None:
        self._rest.call(
            "POST",
            f"/sessions/{_quote(session_id)}/questions/{_quote(question_id)}",
            {"answers": dict(answers)},
        )

    def dismiss_question(self, session_id: str, question_id: str) -> None:
        try:
            self._rest.call(
                "POST",
                f"/sessions/{_quote(session_id)}/questions/{_quote(question_id)}:dismiss",
            )
        except KapError as exc:
            # The dismiss success envelope carries code 40909 (upstream
            # quirk); anything else is a real error.
            if exc.code != KAP_ERROR_QUESTION_DISMISSED:
                raise

    def latest_assistant_text(self, session_id: str) -> str:
        """The text of the most recent assistant message (terminal card body).

        Durable turn.ended carries no final text; the transcript is the source
        of truth. Returns "" when there is nothing to show.
        """
        data = self._rest.get(
            f"/sessions/{_quote(session_id)}/messages?"
            f"role=assistant&page_size={_LATEST_ASSISTANT_PAGE_SIZE}"
        )
        if not isinstance(data, dict):
            raise KapTransportError("messages: unexpected data shape")
        items = data.get("items")
        if not isinstance(items, list):
            raise KapTransportError("messages: unexpected data shape")
        # Server contract (messageLegacyService.list): items are newest-first,
        # and the role filter is applied AFTER pagination — so the first
        # assistant item with text is the latest assistant message. (Iterating
        # oldest-first here surfaced the PREVIOUS prompt's text on terminal
        # cards, observed live 2026-07-22.)
        for message in items:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            parts = [
                str(part.get("text") or "")
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "\n".join(part for part in parts if part).strip()
            if text:
                return text
        return ""


# ---------------------------------------------------------------------------
# Timer abstraction (real timers in kited; manual timers in tests)
# ---------------------------------------------------------------------------


class TimerHandle:
    """Cancel-able handle returned by the timer factory."""

    def cancel(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _ThreadingTimerHandle(TimerHandle):
    def __init__(self, timer: threading.Timer) -> None:
        self._timer = timer

    def cancel(self) -> None:
        self._timer.cancel()


def _threading_timer_factory(delay_seconds: float, callback: Callable[[], None]) -> TimerHandle:
    timer = threading.Timer(delay_seconds, callback)
    timer.daemon = True
    timer.start()
    return _ThreadingTimerHandle(timer)


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ExecutionCardState:
    """The one current execution card of a chat (design §6 single anchor)."""

    anchor: ExecutionCardAnchor
    session_title: str
    prompt_text: str
    started_at: float  # monotonic
    queue_length: int = 0
    tool_lines: list[str] = field(default_factory=list)
    open_tools: dict[str, int] = field(default_factory=dict)  # tool_call_id -> line index
    # Set once the _MAX_TOOL_LINES cap evicted an oldest line (audit L6):
    # the card then renders a truncation notice above the kept tail.
    tool_lines_truncated: bool = False
    # Streaming throttle state (§3.3): last dispatch + the single trailing
    # timer. The state object's identity doubles as the generation guard for
    # stale timer flushes and stale queued renders (§3.8).
    last_stream_patch_at: float = 0.0
    stream_timer: Optional[TimerHandle] = None
    # One-shot guard for the frozen-card minimal retry (230099 → stripped
    # re-render through the dispatcher, audit R-3).
    frozen_minimal_submitted: bool = False
    # Freeze sequence (audit R-4): bumped on every freeze submit; a result
    # callback carrying an older sequence must not fire its minimal retry —
    # it would clobber the newer freeze's card face.
    frozen_seq: int = 0


@dataclass(slots=True)
class _SessionState:
    """Per-session tracked facts (work state axis + attribution tables)."""

    busy: bool = False
    pending_interaction: Optional[str] = None
    last_turn_reason: Optional[str] = None
    title: Optional[str] = None  # None = not fetched yet
    turn_prompts: dict[int, str] = field(default_factory=dict)  # turn_id -> prompt_id
    active_prompt_id: Optional[str] = None
    queue_depth: int = 0


@dataclass(slots=True)
class _PendingApproval:
    approval_id: str
    session_id: str
    prompt_id: str
    owner_chat_id: str
    card_message_id: str  # "" when the card send failed (timer still runs)
    timer: Optional[TimerHandle]
    resolved: bool = False
    status: str = _APPROVAL_STATUS_PENDING


@dataclass(slots=True)
class _PendingQuestion:
    question_id: str
    session_id: str
    prompt_id: str
    owner_chat_id: str
    items: tuple[QuestionItemView, ...]
    # One option-button card per item ("" for an item whose card send
    # failed); aligned with ``items``.
    card_message_ids: tuple[str, ...]
    timer: Optional[TimerHandle]
    # Set when a button click answered the question (the answered event that
    # closes the entry may lag): repeated clicks get the "已回答" notice and
    # the clicked item's card keeps its answer label when the event patches
    # the rest closed.
    resolved: bool = False
    answered_item_index: Optional[int] = None


@dataclass(slots=True)
class _PendingFeedback:
    """Reject-with-feedback step 2: the next plain text from this user."""

    approval_id: str
    chat_id: str
    operator_open_id: str


@dataclass(slots=True)
class _BtwTurnState:
    """One side-channel (`/btw`) agent's in-flight turn (aligned item 13).

    The lightweight path: no card, no main-stream patches — the answer
    accumulates from the agent's own volatile deltas (offset-less upstream,
    so in arrival order; the synthetic-offset append below can never gap)
    and is delivered as plain text on turn.ended. ``prompt_id`` is the
    FIFO-attributed /btw submission that owns the turn ("" when unknown,
    e.g. submitted before a kited restart).
    """

    turn_id: int
    prompt_id: str = ""
    transcript: StreamingTranscript = field(default_factory=StreamingTranscript)


class _InvalidReply(Exception):
    """The text looked like an interaction reply but did not parse."""


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class EventPipeline:
    """Durable kap events -> application actions (all mutations on the loop)."""

    def __init__(
        self,
        *,
        transport: Any,
        rest: Any,
        binding_store: BindingStore,
        terminal_store: TerminalResultStore,
        ownership: PromptOwnership,
        runtime_loop: RuntimeLoop,
        cursor_store: Optional[EventCursorStore] = None,
        approval_timeout_seconds: int = cards.DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        question_timeout_seconds: int = cards.DEFAULT_QUESTION_TIMEOUT_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], TimerHandle] = _threading_timer_factory,
        monotonic: Callable[[], float] = time.monotonic,
        on_snapshot_rebuilt: Optional[Callable[[str, SessionSnapshot], None]] = None,
        terminal_empty_retry_count: int = _DEFAULT_TERMINAL_EMPTY_RETRY_COUNT,
        terminal_empty_retry_delay_seconds: float = _DEFAULT_TERMINAL_EMPTY_RETRY_DELAY_SECONDS,
        stream_patch_interval_seconds: float = _DEFAULT_STREAM_PATCH_INTERVAL_SECONDS,
        stream_reply_char_limit: int = DEFAULT_STREAM_REPLY_CHAR_LIMIT,
        terminal_card_byte_budget: int = _DEFAULT_TERMINAL_CARD_BYTE_BUDGET,
        patch_dispatcher: Optional[CardPatchDispatcher] = None,
        names: Any = None,
        group_mode_of: Any = None,
    ) -> None:
        self._transport = transport
        self._rest = rest
        self._ops = KapInteractionOps(rest)
        self._session_ops = KapSessionOps(rest)
        self._binding_store = binding_store
        self._terminal_store = terminal_store
        self._ownership = ownership
        self._loop = runtime_loop
        self._cursor_store = cursor_store
        self._approval_timeout = int(approval_timeout_seconds)
        self._question_timeout = int(question_timeout_seconds)
        self._timer_factory = timer_factory
        self._monotonic = monotonic
        self._names = names
        # chat_id -> activated group mode (wired from the group config store;
        # used for mode-aware feedback instructions, audit L17).
        self._group_mode_of = group_mode_of
        self._on_snapshot_rebuilt = on_snapshot_rebuilt
        self._terminal_empty_retry_count = max(int(terminal_empty_retry_count), 0)
        self._terminal_retry_delay = max(float(terminal_empty_retry_delay_seconds), 0.0)
        self._stream_patch_interval = max(float(stream_patch_interval_seconds), 0.0)
        self._stream_reply_char_limit = max(int(stream_reply_char_limit), 0)
        self._terminal_byte_budget = max(int(terminal_card_byte_budget), 0)
        # The coalescing dispatcher moves Feishu patch RTT off the loop
        # (streaming-cards §3.2); renders hop back onto the loop so they
        # always read current state.
        self._dispatcher = (
            patch_dispatcher
            if patch_dispatcher is not None
            else CardPatchDispatcher(
                self._patch_message_result,
                render_invoker=self._loop.call,
                timer_factory=timer_factory,
            )
        )

        self._cards: dict[str, _ExecutionCardState] = {}  # chat_id -> current card
        self._sessions: dict[str, _SessionState] = {}
        self._approvals: dict[str, _PendingApproval] = {}
        self._questions: dict[str, _PendingQuestion] = {}
        self._pending_feedback: dict[tuple[str, str], _PendingFeedback] = {}
        # Ids the unroutable path already fail-closed (expired card posted +
        # upstream resolve attempted, §4.6): replays and rebuilds must never
        # re-card or re-resolve them (audit M2).
        self._expired_approval_ids: set[str] = set()
        self._expired_question_ids: set[str] = set()
        # Terminal delivery dedup: one terminal card per (session, prompt)
        # even when two finalize paths (live event / reconcile retry) race.
        # Bounded hygiene (audit L9): FIFO-evict beyond the cap — eviction
        # only re-arms the dedup for a long-finished prompt (a duplicate
        # finalize would re-deliver; never worse than missing the dedup).
        self._terminal_delivered: set[tuple[str, str]] = set()
        self._terminal_delivered_order: deque[tuple[str, str]] = deque()
        self._terminal_retry_timers: dict[tuple[str, str], TimerHandle] = {}
        # Volatile streaming transcripts, keyed by (session_id, prompt_id).
        self._transcripts: dict[tuple[str, str], StreamingTranscript] = {}
        # Side-channel (/btw) tracking, both keyed by (session_id, agent_id):
        # the in-flight side turn and the FIFO of /btw submissions awaiting
        # turn attribution (fed by the AppHandler's note_btw_prompt seam).
        self._btw_turns: dict[tuple[str, str], _BtwTurnState] = {}
        self._btw_prompts: dict[tuple[str, str], deque[str]] = {}
        # Just-ended marker per (session, agent) side channel (monotonic
        # timestamp): an error frame landing inside the window is the
        # just-ended turn's own trailing frame (upstream emits both for one
        # failure in the same tick), never a fresh corpse (audit R4-HIGH-1);
        # the window also dedups the degraded untracked-turn.ended notice
        # (audit R4 LOW). Entries prune lazily on read.
        self._btw_ended_marks: dict[tuple[str, str], float] = {}
        self._shutdown = False

    # ------------------------------------------------------------------
    # Entry points (any thread; hop onto the RuntimeLoop)
    # ------------------------------------------------------------------

    def handle_event(self, event: KapEvent) -> None:
        """WS on_event callback: normalize, then dispatch on the loop."""
        durable = normalize_durable_event(event)
        if durable is None:
            return
        self._loop.submit(self._dispatch, durable)

    def handle_resync_required(self, request: ResyncRequest) -> None:
        """WS on_resync_required callback: snapshot rebuild on the loop."""
        self._loop.submit(self._rebuild_session, request.session_id, "resync")

    def handle_error_frame(self, error: KapErrorFrame) -> None:
        """WS on_error_frame callback: surface a dead prompt as a failed
        terminal result instead of leaving the submit ack hanging
        (fail-closed, mvp-scope §4.5 spirit)."""
        self._loop.submit(self._error_frame_impl, error)

    def handle_volatile(self, delta: AssistantDelta) -> None:
        """WS on_volatile callback: presentation-only assistant deltas; the
        transcript mutation is serialized on the loop like everything else."""
        self._loop.submit(self._assistant_delta, delta)

    def startup_recovery(self, session_ids: Sequence[str]) -> None:
        """kited restart recovery (§4.6): rebuild every bound session from a
        snapshot so in-flight cards are re-anchored and pending approvals that
        cannot be rebuilt are explicitly expired."""
        for session_id in session_ids:
            self._loop.submit(self._rebuild_session, session_id, "startup")

    def shutdown(self) -> None:
        """kited clean shutdown: fail-close sweep of every pending
        approval/question, then cancel all pending timers."""
        self._loop.submit(self._shutdown_impl)

    # ------------------------------------------------------------------
    # Introspection (loop-thread state; read via loop.call by tests/kitectl)
    # ------------------------------------------------------------------

    def work_state_of(self, session_id: str) -> tuple[bool, Optional[str]]:
        """(busy, pending_interaction) as tracked from work_changed/snapshots."""
        session = self._sessions.get(session_id)
        if session is None:
            return False, None
        return session.busy, session.pending_interaction

    def set_snapshot_rebuilt_hook(
        self, hook: Optional[Callable[[str, SessionSnapshot], None]]
    ) -> None:
        """Observability hook fired after every successful snapshot rebuild."""
        self._on_snapshot_rebuilt = hook

    # ------------------------------------------------------------------
    # Dispatch (RuntimeLoop thread)
    # ------------------------------------------------------------------

    def _dispatch(self, event: DurableEvent) -> None:
        if self._shutdown:
            return
        try:
            if not _is_main_agent(event.agent_id):
                # Side-channel (/btw) agent: the lightweight path (aligned
                # item 13) — never the card pipeline below.
                self._dispatch_btw(event)
                return
            if isinstance(event, TurnStarted):
                self._turn_started(event)
            elif isinstance(event, TurnEnded):
                self._turn_ended(event)
            elif isinstance(event, ToolCallStarted):
                self._tool_call_started(event)
            elif isinstance(event, ToolCallResult):
                self._tool_call_result(event)
            elif isinstance(event, PromptAborted):
                self._prompt_aborted(event)
            elif isinstance(event, PromptSteered):
                self._prompt_steered(event)
            elif isinstance(event, ApprovalRequested):
                self._approval_requested(event)
            elif isinstance(event, ApprovalResolved):
                self._approval_resolved(event)
            elif isinstance(event, QuestionRequested):
                self._question_requested(event)
            elif isinstance(event, QuestionResolved):
                self._question_resolved(event)
            elif isinstance(event, SessionWorkChanged):
                self._work_changed(event)
        except Exception:
            logger.exception("outbound dispatch failed for %r", event)

    # ------------------------------------------------------------------
    # /btw side-channel: the lightweight path (aligned item 13)
    # ------------------------------------------------------------------

    def _dispatch_btw(self, event: DurableEvent) -> None:
        """Route a side-channel agent's durable event.

        Only the turn lifecycle and prompt.aborted carry meaning here: the
        side answer is delivered on turn.ended, and a remote abort retires
        the submission's FIFO entry. Everything else is inert by
        construction — the upstream btw agent has every tool call vetoed
        (agent-core-v2 SessionBtwService), so it can never raise approvals,
        questions, or tool events, and KITE exposes no abort/steer surface
        for side prompts itself. work_changed tracks the main agent only.
        """
        if isinstance(event, TurnStarted):
            self._btw_turn_started(event)
        elif isinstance(event, TurnEnded):
            self._btw_turn_ended(event)
        elif isinstance(event, PromptAborted):
            self._btw_prompt_aborted(event)
        else:
            logger.debug(
                "btw event %s ignored session=%s agent=%s",
                type(event).__name__,
                event.session_id,
                event.agent_id,
            )

    def note_btw_prompt(self, session_id: str, agent_id: str, prompt_id: str) -> None:
        """AppHandler seam: a /btw submit succeeded; FIFO-attribute the side
        agent's next turn to this prompt.

        Called directly from ``_cmd_btw`` on the RuntimeLoop thread (inbound
        commands are serialized there), so the entry is in place before the
        turn's events can be dispatched — the WS-delivered turn.started hops
        onto the same loop behind the command.
        """
        key = (session_id, agent_id)
        queue = self._btw_prompts.get(key)
        if queue is None:
            queue = deque()
            self._btw_prompts[key] = queue
        queue.append(prompt_id)

    def _btw_turn_started(self, event: TurnStarted) -> None:
        key = (event.session_id, event.agent_id)
        queue = self._btw_prompts.get(key)
        # POP, not peek (audit R4-HIGH-1): an attributed turn's submission
        # leaves the FIFO at start, so "no tracked turn + a FIFO head"
        # genuinely means that head never reached a turn (dead-on-arrival).
        prompt_id = queue.popleft() if queue else ""
        if queue is not None and not queue:
            self._btw_prompts.pop(key, None)
        if not prompt_id:
            logger.warning(
                "btw turn.started without a tracked submission session=%s agent=%s turn=%s; "
                "the answer will broadcast to attached chats",
                event.session_id,
                event.agent_id,
                event.turn_id,
            )
        # A new turn re-baselines the side stream: any leftover from a turn
        # whose turn.ended was missed is dropped, never merged.
        self._btw_turns[key] = _BtwTurnState(turn_id=event.turn_id, prompt_id=prompt_id)
        logger.info(
            "btw turn started session=%s agent=%s turn=%s prompt=%s",
            event.session_id,
            event.agent_id,
            event.turn_id,
            prompt_id or "-",
        )

    def _btw_assistant_delta(self, delta: AssistantDelta) -> None:
        turn = self._btw_turns.get((delta.session_id, delta.agent_id))
        if turn is None:
            # A delta for an untracked side turn (started while kited was
            # down): volatile text is enhancement, never evidence — drop it;
            # turn.ended still closes with the fallback note.
            return
        # Upstream stamps no offsets for non-main agents (the in-flight
        # tracker is main-only), so the append feeds the transcript its own
        # expected offset: arrival order, never a gap.
        turn.transcript.append_delta(turn.transcript.expected_offset, delta.text_delta)

    def _btw_turn_ended(self, event: TurnEnded) -> None:
        key = (event.session_id, event.agent_id)
        turn = self._btw_turns.get(key)
        if turn is None:
            # Never tracked: a kited restart crossed the in-flight side turn
            # (the snapshot's in-flight projection is main-only, so nothing
            # rebuilds it), a reconnect replayed an already-closed end, or a
            # rebuild already swept it. The answer text is unrecoverable,
            # but the end of the turn is never silent (audit R3-MED-3) —
            # attached chats get ONE degraded notice per just-ended window;
            # a late duplicate or the post-sweep end does not re-notify
            # (audit R4 LOW).
            if self._btw_ended_recently(key):
                logger.debug(
                    "btw degraded notice suppressed (window) session=%s agent=%s turn=%s",
                    event.session_id,
                    event.agent_id,
                    event.turn_id,
                )
                return
            self._btw_ended_marks[key] = self._monotonic()
            logger.info(
                "btw turn.ended for an untracked turn session=%s agent=%s turn=%s; "
                "delivering the degraded notice",
                event.session_id,
                event.agent_id,
                event.turn_id,
            )
            for chat_id, _binding in self._attached_chats(event.session_id):
                self._send_text(
                    chat_id, "旁路 prompt 已结束（KITE 重启或事件重连，答复内容无法取回）。"
                )
            return
        # Delivery targets BEFORE retiring the turn (audit R3-HIGH-1):
        # _end_btw_turn forgets the prompt's ownership, which is exactly what
        # the targeting read needs — computing targets after the retire used
        # to degrade every answer to a broadcast.
        targets = self._btw_target_chats(event.session_id, turn.prompt_id)
        self._end_btw_turn(key)
        text = turn.transcript.full_text()
        if event.reason == "completed":
            body = f"旁路回复：{text}" if text else "旁路 prompt 已完成（没有文本输出）。"
        elif event.reason == "cancelled":
            body = "旁路 prompt 已取消。"
        else:
            detail = f"：{event.error_message}" if event.error_message else "。"
            body = f"旁路 prompt 执行失败{detail}"
        for chat_id in targets:
            self._send_text(chat_id, body)
        logger.info(
            "btw turn ended session=%s agent=%s turn=%s reason=%s prompt=%s",
            event.session_id,
            event.agent_id,
            event.turn_id,
            event.reason,
            turn.prompt_id or "-",
        )

    def _end_btw_turn(self, key: tuple[str, str], *, retire_queue_head: bool = False) -> None:
        """Close the side turn's own tracking (turn.ended / error frame):
        pop the turn, forget its ownership entry, and stamp the just-ended
        marker so the trailing error frame upstream emits for the same
        failure stays silent (audit R4-HIGH-1).

        The FIFO is otherwise untouched: an attributed submission left it at
        turn.started (pop-on-start), and an UNATTRIBUTED turn (e.g. another
        client's submission) must never retire the head — that head is a
        live queued submission, not this turn's corpse (audit R4-MED-1).
        Only the error frame's dead-before-start path may retire the head
        (``retire_queue_head``): that prompt never reached turn.started, so
        it was never popped.
        """
        turn = self._btw_turns.pop(key, None)
        self._btw_ended_marks[key] = self._monotonic()
        prompt_id = turn.prompt_id if turn is not None else ""
        queue = self._btw_prompts.get(key)
        if not prompt_id and retire_queue_head and queue:
            prompt_id = queue[0]
        if prompt_id and queue and queue[0] == prompt_id:
            queue.popleft()
            if not queue:
                self._btw_prompts.pop(key, None)
        if prompt_id:
            self._ownership.forget(prompt_id)

    def _btw_ended_recently(self, key: tuple[str, str]) -> bool:
        """True while the side channel's just-ended marker is fresh (audit
        R4-HIGH-1). Stale markers are pruned on read."""
        mark = self._btw_ended_marks.get(key)
        if mark is None:
            return False
        if self._monotonic() - mark >= _BTW_ENDED_MARK_SECONDS:
            self._btw_ended_marks.pop(key, None)
            return False
        return True

    def _btw_target_chats(self, session_id: str, prompt_id: str) -> list[str]:
        """The side answer's audience: the initiating chat when attribution
        is certain (ownership recorded at /btw submit time); every attached
        chat otherwise. With zero attached chats there is no delivery surface
        at all — the answer is dropped there with a log warning (the only
        possible behavior; the binding itself is the surface)."""
        attached = [chat_id for chat_id, _binding in self._attached_chats(session_id)]
        if prompt_id:
            entry = self._ownership.entry_of(prompt_id)
            if entry is not None and entry.chat_id in attached:
                return [entry.chat_id]
        if not attached:
            logger.warning(
                "btw delivery has no attached chat session=%s prompt=%s; answer dropped",
                session_id,
                prompt_id or "-",
            )
        return attached

    def _btw_error_frame(self, error: KapErrorFrame) -> None:
        """A side-channel agent's error frame applies only to the side turn
        (audit N3-HIGH-1); the main prompt is untouched.

        Upstream emits turn.ended(failed) for the same failure
        (loopService.ts) in the same tick, so:
        - a tracked turn is left alone here — turn.ended delivers the
          single failure notice (audit R3-MED-1);
        - inside the just-ended window the frame is that turn's own
          trailing frame (the KapErrorFrame carries no prompt id) — silent,
          or the NEXT queued submission would be misread as a fresh corpse
          and retired (audit R4-HIGH-1);
        - only a prompt that died BEFORE its turn.started was tracked (no
          tracked turn, no fresh marker, but a FIFO-head submission exists)
          is closed out with an explicit note; anything else is a stray
          frame and only logged.
        """
        session_id = error.session_id
        if not session_id:
            logger.error("btw error frame without session: %s %s", error.code, error.message)
            return
        key = (session_id, error.agent_id or "")
        if key in self._btw_turns:
            logger.debug(
                "btw error frame on a tracked turn session=%s agent=%s; "
                "leaving the failure to turn.ended",
                session_id,
                error.agent_id,
            )
            return
        if self._btw_ended_recently(key):
            logger.info(
                "btw error frame suppressed by the just-ended marker session=%s agent=%s: %s %s",
                session_id,
                error.agent_id,
                error.code,
                error.message,
            )
            return
        queue = self._btw_prompts.get(key)
        prompt_id = queue[0] if queue else ""
        if not prompt_id:
            logger.info(
                "btw error frame with nothing in flight session=%s agent=%s: %s %s",
                session_id,
                error.agent_id,
                error.code,
                error.message,
            )
            return
        # Targets before retiring (audit R3-HIGH-1, same ordering as
        # _btw_turn_ended): the retire forgets the ownership the targeting
        # read depends on.
        targets = self._btw_target_chats(session_id, prompt_id)
        self._end_btw_turn(key, retire_queue_head=True)
        text = f"上游错误 {error.code}: {error.message}" if error.code else error.message
        for chat_id in targets:
            self._send_text(chat_id, f"⚠️ 旁路 prompt 失败：{text}")
        logger.info(
            "btw error frame closed the side turn session=%s agent=%s prompt=%s: %s",
            session_id,
            error.agent_id,
            prompt_id,
            text,
        )

    def _btw_prompt_aborted(self, event: PromptAborted) -> None:
        """A remotely-aborted side prompt retires its FIFO entry (audit
        R3-MED-2): left in place it would mis-attribute the NEXT submission
        to the aborted prompt's owner. When its turn is already tracked,
        turn.ended (cancelled) closes it out and retires instead."""
        key = (event.session_id, event.agent_id)
        turn = self._btw_turns.get(key)
        if turn is not None and turn.prompt_id == event.prompt_id:
            logger.debug(
                "btw prompt.aborted for the tracked turn session=%s agent=%s prompt=%s; "
                "turn.ended closes it",
                event.session_id,
                event.agent_id,
                event.prompt_id,
            )
            return
        queue = self._btw_prompts.get(key)
        if queue and event.prompt_id in queue:
            queue.remove(event.prompt_id)
            if not queue:
                self._btw_prompts.pop(key, None)
            self._ownership.forget(event.prompt_id)
            logger.info(
                "btw prompt aborted before its turn session=%s agent=%s prompt=%s; "
                "FIFO entry retired",
                event.session_id,
                event.agent_id,
                event.prompt_id,
            )
            return
        logger.debug(
            "btw prompt.aborted for an unknown prompt session=%s agent=%s prompt=%s",
            event.session_id,
            event.agent_id,
            event.prompt_id,
        )

    def _sweep_btw_tracking(self, session_id: str) -> None:
        """Fail-closed sweep of the session's btw tracking on a snapshot
        rebuild (audit R3-MED-2).

        A rebuild means durable events may have been lost — including a btw
        turn.ended — so in-flight side state is unverifiable and a stale
        FIFO head would mis-attribute the next submission to the previous
        owner. Retire everything for the session and say so; never guess.
        (Startup recovery sweeps too, but a fresh process has empty maps,
        so it is a no-op there.) The just-ended marker is stamped for the
        swept channels, so a late untracked turn.ended does not deliver a
        second degraded notice on top of this one (audit R4 LOW).
        """
        retired: list[str] = []
        swept_keys: list[tuple[str, str]] = []
        for key in [key for key in self._btw_turns if key[0] == session_id]:
            turn = self._btw_turns.pop(key)
            swept_keys.append(key)
            if turn.prompt_id and turn.prompt_id not in retired:
                retired.append(turn.prompt_id)
        for key in [key for key in self._btw_prompts if key[0] == session_id]:
            queue = self._btw_prompts.pop(key)
            swept_keys.append(key)
            for prompt_id in queue:
                if prompt_id not in retired:
                    retired.append(prompt_id)
        if not retired:
            return
        for key in swept_keys:
            self._btw_ended_marks[key] = self._monotonic()
        for prompt_id in retired:
            self._ownership.forget(prompt_id)
        for chat_id, _binding in self._attached_chats(session_id):
            self._send_text(
                chat_id,
                "⚠️ 事件流重建，在飞的旁路 prompt 状态已丢失；若答复未送达，请重新发送 /btw。",
            )
        logger.warning(
            "btw tracking swept on rebuild session=%s retired=%s", session_id, retired
        )

    def _session(self, session_id: str) -> _SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            session = _SessionState()
            self._sessions[session_id] = session
        return session

    # ------------------------------------------------------------------
    # turn.* / tool.call.* -> execution cards
    # ------------------------------------------------------------------

    def _turn_started(self, event: TurnStarted) -> None:
        session = self._session(event.session_id)
        queue = self._fetch_queue(event.session_id)
        if queue is not None:
            session.queue_depth = queue.queue_depth
            session.active_prompt_id = queue.active_prompt_id
        prompt_id = queue.active_prompt_id if queue else None
        if not prompt_id:
            # The turn cannot be attributed to a prompt; kap FIFO says the
            # active prompt owns it, so no active prompt means this turn is
            # not prompt-driven (e.g. a system trigger). Never guess a card
            # into existence.
            logger.warning(
                "turn.started without an active prompt session=%s turn=%s; no card created",
                event.session_id,
                event.turn_id,
            )
            return
        session.turn_prompts[event.turn_id] = prompt_id
        session.busy = True
        title = self._session_title(event.session_id)
        owner_entry = self._ownership.entry_of(prompt_id)
        logger.info(
            "prompt started chat_id=%s session_id=%s prompt_id=%s turn=%s",
            owner_entry.chat_id if owner_entry is not None else "-",
            event.session_id,
            prompt_id,
            event.turn_id,
        )
        for chat_id, _binding in self._attached_chats(event.session_id):
            existing = self._cards.get(chat_id)
            if (
                existing is not None
                and existing.anchor.session_id == event.session_id
                and existing.anchor.prompt_id == prompt_id
            ):
                # Duplicate/replayed turn.started for the prompt that already
                # owns the chat's card (audit L7; FOCUS reuse_existing_card):
                # keep the live card — freezing and re-sending would churn
                # the chat for zero information.
                logger.info(
                    "duplicate turn.started session=%s prompt=%s turn=%s; reusing existing card",
                    event.session_id,
                    prompt_id,
                    event.turn_id,
                )
                continue
            self._create_execution_card(
                chat_id, event.session_id, prompt_id, event.prompt_text, title, session.queue_depth
            )

    def _create_execution_card(
        self,
        chat_id: str,
        session_id: str,
        prompt_id: str,
        prompt_text: str,
        session_title: str,
        queue_length: int,
    ) -> None:
        stale = self._cards.get(chat_id)
        if stale is not None:
            # One current card per chat. A stale anchor means its prompt's
            # terminal event was missed; kap FIFO guarantees it ended. Freeze
            # it as done (no fabricated terminal card) and replace it.
            logger.warning(
                "replacing stale execution card chat=%s prompt=%s",
                chat_id,
                stale.anchor.prompt_id,
            )
            self._cancel_stream_state(stale)
            self._transcripts.pop((stale.anchor.session_id, stale.anchor.prompt_id), None)
            self._freeze_card_done(stale)
        card = cards.build_execution_card(
            session_title=session_title,
            session_id=session_id,
            prompt_text=prompt_text,
            queue_length=queue_length,
            prompt_id=prompt_id,
        )
        message_id = self._send_card(chat_id, card)
        if not message_id:
            logger.error("execution card send failed chat=%s prompt=%s", chat_id, prompt_id)
            return
        self._cards[chat_id] = _ExecutionCardState(
            anchor=ExecutionCardAnchor(
                chat_id=chat_id,
                session_id=session_id,
                prompt_id=prompt_id,
                card_message_id=message_id,
            ),
            session_title=session_title,
            prompt_text=prompt_text,
            started_at=self._monotonic(),
            queue_length=queue_length,
        )

    def _tool_call_started(self, event: ToolCallStarted) -> None:
        session = self._sessions.get(event.session_id)
        prompt_id = session.turn_prompts.get(event.turn_id) if session else None
        if not prompt_id:
            return
        line = self._render_tool_line("⏳", event.name, event.detail or event.description)
        for state in self._cards_for_prompt(event.session_id, prompt_id):
            if len(state.tool_lines) >= _MAX_TOOL_LINES:
                # Tail eviction (audit L6, FOCUS parity): drop the OLDEST
                # line, never the newest activity, and let the card show a
                # truncation notice. Open-tool indices shift with the
                # eviction; a tool whose line was evicted simply loses its
                # result update (the pop below finds nothing).
                del state.tool_lines[0]
                state.open_tools = {
                    call_id: index - 1
                    for call_id, index in state.open_tools.items()
                    if index > 0
                }
                state.tool_lines_truncated = True
            state.open_tools[event.tool_call_id] = len(state.tool_lines)
            state.tool_lines.append(line)
            self._patch_execution_card(state)

    def _tool_call_result(self, event: ToolCallResult) -> None:
        session = self._sessions.get(event.session_id)
        prompt_id = session.turn_prompts.get(event.turn_id) if session else None
        if not prompt_id:
            return
        marker = "❌" if event.is_error else "✅"
        for state in self._cards_for_prompt(event.session_id, prompt_id):
            index = state.open_tools.pop(event.tool_call_id, None)
            if index is None or index >= len(state.tool_lines):
                continue
            state.tool_lines[index] = marker + state.tool_lines[index][1:]
            self._patch_execution_card(state)

    @staticmethod
    def _render_tool_line(marker: str, name: str, detail: str) -> str:
        name = str(name or "").strip() or "tool"
        detail = " ".join(str(detail or "").split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        return f"{marker} `{name}` {detail}".rstrip()

    @staticmethod
    def _tool_lines_for_card(state: _ExecutionCardState) -> list[str]:
        """Tool lines as rendered on the card: the kept tail plus the
        truncation notice head line once the cap evicted something (L6)."""
        if not state.tool_lines_truncated:
            return state.tool_lines
        return [_TOOL_LINES_TRUNCATION_NOTICE, *state.tool_lines]

    def _turn_ended(self, event: TurnEnded) -> None:
        session = self._sessions.get(event.session_id)
        prompt_id = session.turn_prompts.pop(event.turn_id, None) if session else None
        if event.reason == "completed" and prompt_id:
            # Audit L3: the reconcile's moved_on guard reads
            # session.active_prompt_id, which this handler is about to clear
            # and which only the trailing queue refresh repopulates — so
            # attempt-1 always used to run with a stale watermark (the
            # "secondary window"). Refresh the watermark FIRST: a session
            # that already advanced to the next prompt must never fetch
            # unattributable text for this one.
            self._refresh_active_prompt_watermark(event.session_id)
        if session is not None and session.active_prompt_id == prompt_id:
            session.active_prompt_id = None
        if not prompt_id:
            return
        if event.reason == "completed":
            # The turn-end-vs-final-flush race is normal, not an edge case:
            # reconcile the terminal text (retry-on-empty) before the card
            # falls back to stub text.
            self._terminal_reconcile(event.session_id, prompt_id, attempt=1)
        elif event.reason == "cancelled":
            self._finish_prompt(event.session_id, prompt_id, cards.TERMINAL_ABORTED, "")
        else:
            self._finish_prompt(
                event.session_id, prompt_id, cards.TERMINAL_FAILED, event.error_message
            )
        self._refresh_queue_depth(event.session_id)

    def _terminal_reconcile(self, session_id: str, prompt_id: str, attempt: int) -> None:
        """Snapshot-authoritative terminal text fetch with retry-on-empty.

        Attempt 1 runs synchronously from turn.ended; an empty fetch is
        retried via the timer factory up to ``terminal_empty_retry_count``
        times, then the terminal card goes out with stub text (the builder's
        fallback). Delivery is deduped by ``_finish_prompt``; once the
        session has moved on to a newer active prompt the fetch is skipped —
        the latest assistant text can no longer be attributed to this prompt
        (monotonic rule: never pin stale text on its terminal card).

        Attribution boundary (audit L3): the fetch heuristic takes the
        newest assistant text on the latest page. When the finished turn
        produced NO assistant text of its own (it ended on tool output) and
        no newer prompt has activated, the previous prompt's reply is
        unattributable yet indistinguishable — the moved_on guard cannot
        fire. Recorded as a known boundary; the mid-term fix is upstream
        per-message prompt attribution (not populated today), not another
        local heuristic.
        """
        key = (session_id, prompt_id)
        self._terminal_retry_timers.pop(key, None)  # the timer that fired is spent
        if key in self._terminal_delivered:
            return
        session = self._sessions.get(session_id)
        moved_on = bool(
            session is not None
            and session.active_prompt_id
            and session.active_prompt_id != prompt_id
        )
        if not moved_on:
            text = self._fetch_terminal_text(session_id)
            if text:
                self._finish_prompt(
                    session_id,
                    prompt_id,
                    cards.TERMINAL_COMPLETED,
                    text,
                    standalone_when_orphaned=True,
                )
                return
            if attempt <= self._terminal_empty_retry_count:
                self._schedule_terminal_retry(session_id, prompt_id, attempt + 1)
                return
            logger.warning(
                "terminal text still empty after %d retries session=%s prompt=%s; "
                "falling back to stub text",
                self._terminal_empty_retry_count,
                session_id,
                prompt_id,
            )
        else:
            logger.warning(
                "session %s moved on to prompt %s during terminal reconcile of %s; "
                "closing with stub text",
                session_id,
                session.active_prompt_id if session else None,
                prompt_id,
            )
        self._finish_prompt(
            session_id,
            prompt_id,
            cards.TERMINAL_COMPLETED,
            "",
            standalone_when_orphaned=True,
        )

    def _schedule_terminal_retry(self, session_id: str, prompt_id: str, next_attempt: int) -> None:
        key = (session_id, prompt_id)

        def _fire() -> None:
            try:
                self._loop.submit(self._terminal_reconcile, session_id, prompt_id, next_attempt)
            except Exception:  # loop closed during shutdown
                logger.debug("terminal retry fired after loop close: %s", prompt_id)

        try:
            handle = self._timer_factory(self._terminal_retry_delay, _fire)
        except Exception:
            # Fail-closed: never leave the terminal undelivered because a
            # timer could not be started.
            logger.exception("terminal retry timer failed session=%s prompt=%s", session_id, prompt_id)
            self._finish_prompt(
                session_id,
                prompt_id,
                cards.TERMINAL_COMPLETED,
                "",
                standalone_when_orphaned=True,
            )
            return
        self._terminal_retry_timers[key] = handle

    def _prompt_aborted(self, event: PromptAborted) -> None:
        # Covers both active and queued aborts (spike S2). Queued prompts have
        # no card; the queue refresh below is their only visible effect.
        self._finish_prompt(event.session_id, event.prompt_id, cards.TERMINAL_ABORTED, "")
        self._ownership.forget(event.prompt_id)
        self._refresh_queue_depth(event.session_id)

    def _error_frame_impl(self, error: KapErrorFrame) -> None:
        """Terminal-fail the session's active prompt from a WS error frame.

        The REST submit can succeed while the turn dies immediately
        (e.g. ``model.not_configured``, observed live 2026-07-22); without
        this the user is left with "已提交，正在执行" forever. Only
        main-agent frames apply here — a side-channel agent's frame ends
        only its own turn's tracking (audit N3-HIGH-1).
        """
        if self._shutdown:
            return
        if not _is_main_agent(error.agent_id):
            self._btw_error_frame(error)
            return
        session_id = error.session_id
        if not session_id:
            logger.error("kap error frame without session: %s %s", error.code, error.message)
            return
        text = f"上游错误 {error.code}: {error.message}" if error.code else error.message
        prompt_id = self._attribute_prompt(session_id, None)
        if prompt_id is None:
            logger.error(
                "kap error %s on session %s (no active prompt): %s",
                error.code,
                session_id,
                error.message,
            )
            for chat_id, _binding in self._attached_chats(session_id):
                self._send_text(chat_id, f"⚠️ {text}")
            self._refresh_queue_depth(session_id)
            return
        states = self._cards_for_prompt(session_id, prompt_id)
        if states:
            self._finish_prompt(session_id, prompt_id, cards.TERMINAL_FAILED, text)
        else:
            # The prompt died before any execution card existed: send a
            # standalone terminal card so the failure is still visible.
            for chat_id, _binding in self._attached_chats(session_id):
                self._send_card(
                    chat_id,
                    cards.build_terminal_card(
                        outcome=cards.TERMINAL_FAILED,  # type: ignore[arg-type]
                        text=text,
                    ),
                )
            self._ownership.forget(prompt_id)
        self._refresh_queue_depth(session_id)

    def _prompt_steered(self, event: PromptSteered) -> None:
        # No steer surface in the MVP; only the queue shape changed.
        logger.info(
            "prompt steered session=%s active=%s merged=%s",
            event.session_id,
            event.active_prompt_id,
            event.prompt_ids,
        )
        self._refresh_queue_depth(event.session_id)

    def _finish_prompt(
        self,
        session_id: str,
        prompt_id: str,
        outcome: str,
        text: str,
        *,
        standalone_when_orphaned: bool = False,
    ) -> None:
        """Deliver the terminal card exactly once per (session, prompt).

        The dedup registry is the single choke point: the live event path, a
        reconcile retry, and any later finalize path all pass through here,
        and the second one notices the recorded terminal and skips. Once a
        terminal went out with real text nothing replaces it with shorter or
        stale content (monotonic rule).
        """
        key = (session_id, prompt_id)
        if key in self._terminal_delivered:
            logger.info(
                "terminal already delivered session=%s prompt=%s; skipping duplicate finalize",
                session_id,
                prompt_id,
            )
            return
        states = self._cards_for_prompt(session_id, prompt_id)
        if not states and not standalone_when_orphaned:
            # Queued prompts never had a card; their abort is queue-shape only.
            self._ownership.forget(prompt_id)
            self._transcripts.pop(key, None)
            return
        self._terminal_delivered.add(key)
        self._terminal_delivered_order.append(key)
        while len(self._terminal_delivered_order) > _TERMINAL_DELIVERED_CAP:
            self._terminal_delivered.discard(self._terminal_delivered_order.popleft())
        retry = self._terminal_retry_timers.pop(key, None)
        if retry is not None:
            retry.cancel()
        if outcome == cards.TERMINAL_COMPLETED and text:
            # §3.5: the completed turn's authoritative text reconciles over
            # the delta-accumulated text (monotonic, never shrink) before the
            # frozen card renders the final body.
            transcript = self._transcripts.get(key)
            if transcript is not None:
                transcript.reconcile(text)
        for state in states:
            self._cancel_stream_state(state)
            self._send_terminal_and_freeze(state, outcome, text)
            del self._cards[state.anchor.chat_id]
        if not states:
            # The anchor vanished while the terminal reconcile was pending
            # (rebound / snapshot rebuild froze it): deliver the terminal
            # standalone so the result is never lost silently — and persist
            # it like the anchored path (audit L4), or /last loses it.
            for chat_id, _binding in self._attached_chats(session_id):
                terminal_message_id = self._deliver_terminal_card(
                    chat_id,
                    outcome,
                    text,
                    result_id=prompt_id,
                    checksum=cards.terminal_result_checksum(text),
                )
                if terminal_message_id and text:
                    self._terminal_store.upsert(
                        TerminalResultRecord(
                            message_id=terminal_message_id,
                            execution_message_id="",
                            final_reply_text=text,
                            recorded_at=time.time(),
                            terminal_result_id=prompt_id,
                            session_id=session_id,
                            checksum=cards.terminal_result_checksum(text),
                        )
                    )
        self._transcripts.pop(key, None)
        owner_entry = self._ownership.entry_of(prompt_id)
        logger.info(
            "prompt ended chat_id=%s session_id=%s prompt_id=%s outcome=%s",
            owner_entry.chat_id if owner_entry is not None else "-",
            session_id,
            prompt_id,
            outcome,
        )
        self._ownership.forget(prompt_id)

    def _send_terminal_and_freeze(
        self, state: _ExecutionCardState, outcome: str, text: str
    ) -> None:
        anchor = state.anchor
        result_id = anchor.prompt_id
        checksum = cards.terminal_result_checksum(text)
        terminal_message_id = self._deliver_terminal_card(
            anchor.chat_id, outcome, text, result_id=result_id, checksum=checksum
        )
        self._freeze_card_done(state)
        if terminal_message_id and text:
            self._terminal_store.upsert(
                TerminalResultRecord(
                    message_id=terminal_message_id,
                    execution_message_id=anchor.card_message_id,
                    final_reply_text=text,
                    recorded_at=time.time(),
                    terminal_result_id=result_id,
                    session_id=anchor.session_id,
                    checksum=checksum,
                )
            )

    def _deliver_terminal_card(
        self,
        chat_id: str,
        outcome: str,
        text: str,
        *,
        result_id: str,
        checksum: str,
    ) -> str:
        """Deliver the terminal card; returns its message id ("" on failure).

        Terminal budget discipline (streaming-cards §3.7, ported from FOCUS):
        the serialized card must fit the utf-8 byte budget, otherwise the
        content goes out as plain text. A failed send gets a one-time
        plain-text content rescue (§3.4) — the result is never lost silently.
        """
        terminal_card = cards.build_terminal_card(
            outcome=outcome,  # type: ignore[arg-type]
            text=text,
            terminal_result_id=result_id,
            checksum=checksum,
        )
        content = json.dumps(terminal_card)
        message_id = ""
        if len(content.encode("utf-8")) <= self._terminal_byte_budget:
            message_id = self._send_card(chat_id, terminal_card)
        else:
            logger.warning(
                "terminal card over utf-8 budget (%d > %d) chat=%s; plain-text fallback",
                len(content.encode("utf-8")),
                self._terminal_byte_budget,
                chat_id,
            )
        if not message_id and text:
            message_id = self._send_text_get_id(chat_id, text)
        return message_id

    def _send_text_get_id(self, chat_id: str, text: str) -> str:
        """Plain-text send returning its message id ("" on failure)."""
        try:
            return str(self._transport.reply_get_id(chat_id, text) or "").strip()
        except Exception:
            logger.exception("terminal plain-text rescue failed chat=%s", chat_id)
            return ""

    def _freeze_card_done(self, state: _ExecutionCardState) -> None:
        self._patch_frozen_execution_card(state, cards.EXECUTION_STATE_FROZEN_DONE)

    def _patch_frozen_execution_card(self, state: _ExecutionCardState, execution_state: str) -> None:
        """Freeze a non-running execution card, with a one-shot minimal retry.

        The patches go through the CardPatchDispatcher (audit R-3): a Feishu
        230020 rate limit on a freeze is requeued after ``retry_after``
        instead of leaving the card "执行中" forever, and the blocking IO
        moves off the RuntimeLoop. The frozen content is computed eagerly —
        the card is terminal, so there is no later state to coalesce. The
        minimal retry (230099 ``content_rejected`` → strip tool lines and
        the reply projection, ported from FOCUS 5787d4c) fires from the
        dispatcher's result callback and stays one-shot: a rejected minimal
        card is dropped (the terminal card still carries the result).
        """
        message_id = state.anchor.card_message_id
        state.frozen_seq += 1
        frozen_seq = state.frozen_seq
        reply_text = self._stream_projection(state)
        card = cards.build_execution_card(
            session_title=state.session_title,
            session_id=state.anchor.session_id,
            prompt_text=state.prompt_text,
            state=execution_state,
            elapsed_seconds=self._elapsed(state),
            queue_length=0,
            tool_lines=self._tool_lines_for_card(state),
            reply_text=reply_text,
        )
        content = json.dumps(card)
        # Capture the strippability now: by the time the result callback
        # runs, the transcript may already be popped (the read must match
        # what the full card actually carried).
        strippable = bool(state.tool_lines) or bool(reply_text)

        def _on_full_result(result: MessagePatchResult) -> None:
            # Fired on the RuntimeLoop via the dispatcher's render invoker.
            if frozen_seq != state.frozen_seq:
                # A newer freeze of this card was scheduled after this one:
                # its own callback owns the minimal decision — a stale
                # minimal would clobber the newer freeze's face (audit R-4).
                return
            if not result.content_rejected or not strippable:
                return
            if state.frozen_minimal_submitted:
                return  # one-shot
            state.frozen_minimal_submitted = True
            logger.warning(
                "frozen execution card content rejected by Feishu, retrying minimal: message_id=%s",
                message_id,
            )
            minimal_card = cards.build_execution_card(
                session_title=state.session_title,
                session_id=state.anchor.session_id,
                prompt_text=state.prompt_text,
                state=execution_state,
                elapsed_seconds=self._elapsed(state),
                queue_length=0,
                tool_lines=[],
                reply_text="",
            )
            minimal_content = json.dumps(minimal_card)
            self._dispatcher.submit(message_id, lambda: minimal_content)

        self._dispatcher.submit(message_id, lambda: content, on_result=_on_full_result)

    def _fetch_terminal_text(self, session_id: str) -> str:
        try:
            return self._ops.latest_assistant_text(session_id)
        except (KapError, KapTransportError) as exc:
            # The terminal card still goes out (with the builder's fallback
            # text); losing the final reply body must not lose the result.
            logger.warning("terminal text fetch failed session=%s: %s", session_id, exc)
            return ""

    def _fetch_queue(self, session_id: str):
        try:
            return self._session_ops.get_prompts(session_id)
        except (KapError, KapTransportError) as exc:
            logger.warning("prompts fetch failed session=%s: %s", session_id, exc)
            return None

    def _refresh_active_prompt_watermark(self, session_id: str) -> None:
        """Best-effort active-prompt watermark refresh (no card patches).

        The full queue-depth refresh still runs at the end of turn.ended;
        this only moves the attribution watermark read by the terminal
        reconcile's moved_on guard (audit L3).
        """
        queue = self._fetch_queue(session_id)
        if queue is not None and queue.active_prompt_id:
            self._session(session_id).active_prompt_id = queue.active_prompt_id

    def _refresh_queue_depth(self, session_id: str) -> None:
        queue = self._fetch_queue(session_id)
        if queue is None:
            return
        session = self._session(session_id)
        session.queue_depth = queue.queue_depth
        if queue.active_prompt_id:
            session.active_prompt_id = queue.active_prompt_id
        for state in list(self._cards.values()):
            if state.anchor.session_id != session_id:
                continue
            state.queue_length = queue.queue_depth
            self._patch_execution_card(state)

    def _work_changed(self, event: SessionWorkChanged) -> None:
        session = self._session(event.session_id)
        session.busy = event.busy
        session.pending_interaction = event.pending_interaction
        if event.last_turn_reason:
            session.last_turn_reason = event.last_turn_reason

    # ------------------------------------------------------------------
    # assistant.delta -> streamed execution-card body (streaming-cards.md)
    # ------------------------------------------------------------------

    def _assistant_delta(self, delta: AssistantDelta) -> None:
        if self._shutdown:
            return
        if not _is_main_agent(delta.agent_id):
            # Side-channel (/btw) stream: accumulates for the plain-text
            # answer, NEVER into the main card's transcript (N3-HIGH-1).
            self._btw_assistant_delta(delta)
            return
        if delta.offset is None:
            # normalize_volatile_event already drops offset-less main deltas;
            # this guards a direct handle_volatile caller.
            return
        session = self._sessions.get(delta.session_id)
        prompt_id = session.active_prompt_id if session else None
        if not prompt_id:
            # Target matching (§3.9): an unattributable delta mutates nothing.
            return
        states = self._cards_for_prompt(delta.session_id, prompt_id)
        if not states:
            return
        key = (delta.session_id, prompt_id)
        transcript = self._transcripts.get(key)
        if transcript is None:
            transcript = StreamingTranscript()
            self._transcripts[key] = transcript
        was_gapped = transcript.gapped
        if transcript.append_delta(delta.offset, delta.text_delta):
            # §4.1: an offset gap jumps straight to the snapshot-rebuild path
            # (never guess the missing text), exactly once per gap episode —
            # the gapped latch holds until the rebuild re-baselines the stream.
            if not was_gapped:
                logger.warning(
                    "assistant delta offset gap session=%s prompt=%s; rebuilding from snapshot",
                    delta.session_id,
                    prompt_id,
                )
                self._rebuild_session(delta.session_id, "delta-gap")
            return
        for state in states:
            self._schedule_stream_patch(state)

    def _schedule_stream_patch(self, state: _ExecutionCardState) -> None:
        """Throttle (§3.3): patch immediately when idle; inside the min
        interval arm a single trailing timer, which renders the latest state
        (the final state is never dropped)."""
        if self._shutdown:
            return
        now = self._monotonic()
        since_last = now - state.last_stream_patch_at
        if since_last >= self._stream_patch_interval:
            if state.stream_timer is not None:
                state.stream_timer.cancel()
                state.stream_timer = None
            state.last_stream_patch_at = now
            self._submit_stream_patch(state)
            return
        if state.stream_timer is not None:
            return

        def _fire() -> None:
            try:
                self._loop.submit(self._flush_stream_patch, state)
            except Exception:  # loop closed during shutdown
                logger.debug("stream trailing timer fired after loop close")

        try:
            state.stream_timer = self._timer_factory(
                self._stream_patch_interval - since_last, _fire
            )
        except Exception:
            logger.exception(
                "stream trailing timer start failed chat=%s", state.anchor.chat_id
            )

    def _flush_stream_patch(self, state: _ExecutionCardState) -> None:
        # Generation guard (§3.8): a stale timer firing after its prompt
        # ended (or its card was replaced) is a no-op.
        if self._cards.get(state.anchor.chat_id) is not state:
            return
        state.stream_timer = None
        state.last_stream_patch_at = self._monotonic()
        self._submit_stream_patch(state)

    def _submit_stream_patch(self, state: _ExecutionCardState) -> None:
        message_id = state.anchor.card_message_id
        if not message_id:
            return
        self._dispatcher.submit(message_id, lambda: self._render_running_card(state))

    def _render_running_card(self, state: _ExecutionCardState) -> Optional[str]:
        """Full-snapshot render of the running card (§3.1), invoked by the
        dispatcher on the RuntimeLoop at patch time.

        Rendering at patch time means a coalesced render still produces the
        latest content; a stale anchor (prompt ended, card replaced,
        shutdown) returns None and the patch is skipped (§3.8).
        """
        if self._shutdown:
            return None
        if self._cards.get(state.anchor.chat_id) is not state:
            return None
        card = cards.build_execution_card(
            session_title=state.session_title,
            session_id=state.anchor.session_id,
            prompt_text=state.prompt_text,
            state=cards.EXECUTION_STATE_RUNNING,
            elapsed_seconds=self._elapsed(state),
            queue_length=state.queue_length,
            tool_lines=self._tool_lines_for_card(state),
            reply_text=self._stream_projection(state),
            prompt_id=state.anchor.prompt_id,
        )
        return json.dumps(card)

    def _stream_projection(self, state: _ExecutionCardState) -> str:
        transcript = self._transcripts.get(
            (state.anchor.session_id, state.anchor.prompt_id)
        )
        if transcript is None:
            return ""
        return transcript.project_for_card(self._stream_reply_char_limit)

    def _cancel_stream_state(self, state: _ExecutionCardState) -> None:
        """Timer hygiene (§3.8): terminal/replacement transitions cancel the
        card's trailing timer and its queued dispatcher work."""
        if state.stream_timer is not None:
            state.stream_timer.cancel()
            state.stream_timer = None
        self._dispatcher.cancel(state.anchor.card_message_id)

    def _patch_message_result(self, message_id: str, content: str) -> MessagePatchResult:
        """Structured patch IO for the dispatcher (transport edge, off-loop)."""
        patch_message_result = getattr(self._transport, "patch_message_result", None)
        if callable(patch_message_result):
            try:
                result = patch_message_result(message_id, content)
            except Exception:
                logger.exception("stream card patch raised message=%s", message_id)
                return MessagePatchResult.failure()
            if isinstance(result, MessagePatchResult):
                return result
            return MessagePatchResult.success() if result else MessagePatchResult.failure()
        try:
            ok = bool(self._transport.patch_message(message_id, content))
        except Exception:
            logger.exception("stream card patch raised message=%s", message_id)
            return MessagePatchResult.failure()
        return MessagePatchResult.success() if ok else MessagePatchResult.failure()

    def _reseed_transcripts(
        self, session_id: str, snapshot: SessionSnapshot, current_prompt: Optional[str]
    ) -> None:
        """Heal the volatile transcript from the snapshot watermark (§1.2).

        The snapshot's in-flight assistant text is step-relative — the same
        reference frame as the delta offsets — so it re-seeds both the
        accumulated text and the expected offset; transcripts of prompts no
        longer in flight are dropped.
        """
        in_flight_prompt = current_prompt if snapshot.in_flight and current_prompt else None
        if in_flight_prompt:
            transcript = self._transcripts.setdefault(
                (session_id, in_flight_prompt), StreamingTranscript()
            )
            transcript.rebuild_from_snapshot(snapshot.in_flight_assistant_text)
        for key in [
            key for key in self._transcripts if key[0] == session_id and key[1] != in_flight_prompt
        ]:
            self._transcripts.pop(key, None)

    # ------------------------------------------------------------------
    # approval.* -> approval cards, read-only notices, timeouts
    # ------------------------------------------------------------------

    def _approval_requested(self, event: ApprovalRequested) -> None:
        if event.approval_id in self._approvals or event.approval_id in self._expired_approval_ids:
            return  # replayed duplicate (tracked, or already fail-closed)
        prompt_id = self._attribute_prompt(event.session_id, event.turn_id)
        if prompt_id is None:
            logger.warning(
                "approval %s cannot be attributed to a prompt; expiring", event.approval_id
            )
            self._expire_unroutable_approval(
                event.session_id, event.approval_id, None, reason="无法确定所属 prompt"
            )
            return
        self._route_approval(
            session_id=event.session_id,
            approval_id=event.approval_id,
            prompt_id=prompt_id,
            tool_name=event.tool_name,
            action=event.action,
            detail=event.detail,
        )

    def _attribute_prompt(self, session_id: str, turn_id: Optional[int]) -> Optional[str]:
        """turn_id -> prompt_id, with kap FIFO fallbacks. None = unattributable."""
        session = self._session(session_id)
        if turn_id is not None and turn_id in session.turn_prompts:
            return session.turn_prompts[turn_id]
        if session.active_prompt_id:
            return session.active_prompt_id
        queue = self._fetch_queue(session_id)
        if queue is not None and queue.active_prompt_id:
            session.active_prompt_id = queue.active_prompt_id
            return queue.active_prompt_id
        return None

    def _route_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        prompt_id: str,
        tool_name: str,
        action: str,
        detail: str,
    ) -> None:
        entry = self._ownership.entry_of(prompt_id)
        attached = dict(self._attached_chats(session_id))
        owner_chat = entry.chat_id if entry is not None else ""
        if entry is None or entry.certainty != CERTAINTY_CERTAIN or owner_chat not in attached:
            # Fail-closed (§4.6): never route an actionable approval card on a
            # best-effort guess; close it out as expired instead — upstream
            # included, or the turn would block forever (audit M2).
            if entry is not None and entry.certainty == CERTAINTY_CERTAIN:
                reason = "该审批的发起聊天当前不可达"
            elif entry is not None:
                reason = "KITE 重启后无法确认该审批的发起者"
            else:
                reason = "无法确定该审批的发起者"
            targets = [owner_chat] if owner_chat in attached else list(attached)
            self._expire_unroutable_approval(
                session_id, approval_id, targets, reason=reason, prompt_id=prompt_id
            )
            return
        card = cards.build_approval_card(
            approval_id=approval_id,
            prompt_id=prompt_id,
            tool_name=tool_name,
            action=action,
            detail=detail,
            timeout_seconds=self._approval_timeout,
        )
        message_id = self._send_card(owner_chat, card)
        if not message_id:
            logger.error(
                "approval card send failed chat=%s approval=%s", owner_chat, approval_id
            )
        timer = self._start_timer(
            self._approval_timeout, self._approval_timed_out, approval_id
        )
        self._approvals[approval_id] = _PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            prompt_id=prompt_id,
            owner_chat_id=owner_chat,
            card_message_id=message_id or "",
            timer=timer,
        )
        logger.info(
            "approval requested session_id=%s prompt_id=%s approval_id=%s owner=%s",
            session_id,
            prompt_id,
            approval_id,
            owner_chat,
        )
        label = self._initiator_label(prompt_id)
        if label:
            notice = f"⏳ 该会话有一个审批待处理：等待 {label}（`{prompt_id}` 号 prompt 的发起者）处理审批。"
        else:
            notice = f"⏳ 该会话有一个审批待处理：等待 `{prompt_id}` 号 prompt 的发起者处理审批。"
        for chat_id in attached:
            if chat_id != owner_chat:
                self._send_text(chat_id, notice)

    def _send_approval_expired(
        self,
        session_id: str,
        chat_ids: Optional[Sequence[str]],
        *,
        reason: str,
        prompt_id: str = "",
    ) -> None:
        card = cards.build_approval_expired_card(reason=reason)
        targets = list(chat_ids) if chat_ids is not None else [
            chat_id for chat_id, _ in self._attached_chats(session_id)
        ]
        for chat_id in targets:
            self._send_card(chat_id, card)
        logger.info(
            "approval expired card posted session=%s prompt=%s targets=%s",
            session_id,
            prompt_id or "-",
            targets,
        )

    def _expire_unroutable_approval(
        self,
        session_id: str,
        approval_id: str,
        chat_ids: Optional[Sequence[str]],
        *,
        reason: str,
        prompt_id: str = "",
    ) -> None:
        """Fail-closed to the end (§4.6): post the expired card AND resolve
        the approval upstream as rejected.

        design §4.6 says "explicitly expired and closed out": an unroutable
        approval that is only carded stays pending upstream forever
        (upstream approvals never expire), blocking its turn until a manual
        `kitectl interaction sweep` (audit M2; FOCUS always auto-rejects
        upstream in this case). The id is recorded so replays and snapshot
        rebuilds never re-card or re-resolve it. The upstream resolve is
        best-effort: an already-resolved/conflicting approval is fine, and
        an unreachable kap leaves the close-out recorded locally (the sweep
        remains the manual backstop).
        """
        if approval_id in self._expired_approval_ids:
            return
        self._expired_approval_ids.add(approval_id)
        self._send_approval_expired(session_id, chat_ids, reason=reason, prompt_id=prompt_id)
        try:
            self._ops.resolve_approval(
                session_id, approval_id, decision=cards.APPROVAL_DECISION_REJECTED
            )
        except (KapError, KapTransportError) as exc:
            logger.warning(
                "unroutable approval %s upstream reject failed: %s", approval_id, exc
            )
            return
        logger.info(
            "unroutable approval rejected upstream session_id=%s approval_id=%s",
            session_id,
            approval_id,
        )

    def _approval_resolved(self, event: ApprovalResolved) -> None:
        """Freeze the card everywhere (resolution may come from any client,
        e.g. the web UI — spike S1 broadcasts approval.resolved to all)."""
        pending = self._approvals.pop(event.approval_id, None)
        if pending is None:
            return
        self._cancel_timer(pending)
        self._drop_pending_feedback(event.approval_id)
        if pending.card_message_id:
            self._patch_card(
                pending.card_message_id,
                cards.build_approval_resolved_card(
                    decision=event.decision, feedback=event.feedback
                ),
            )
        logger.info(
            "approval resolved chat_id=%s session_id=%s prompt_id=%s approval_id=%s decision=%s",
            pending.owner_chat_id if pending is not None else "-",
            event.session_id,
            pending.prompt_id if pending is not None else "-",
            event.approval_id,
            event.decision,
        )

    def _approval_timed_out(self, approval_id: str) -> None:
        pending = self._approvals.get(approval_id)
        if pending is None or pending.resolved:
            return
        self._approvals.pop(approval_id, None)
        try:
            self._ops.resolve_approval(
                pending.session_id, approval_id, decision=cards.APPROVAL_DECISION_REJECTED
            )
        except KapError as exc:
            if exc.code in (KAP_ERROR_ALREADY_RESOLVED, KAP_ERROR_APPROVAL_NOT_FOUND):
                # Resolved (or dropped) upstream meanwhile: freeze as handled.
                self._drop_pending_feedback(approval_id)
                if pending.card_message_id:
                    self._patch_card(
                        pending.card_message_id,
                        cards.build_approval_resolved_card(decision=""),
                    )
                return
            logger.warning("approval timeout resolve failed %s: %s", approval_id, exc)
            self._approvals[approval_id] = pending
            return
        except KapTransportError as exc:
            # kap is unreachable: keep tracking — the approval is still
            # pending upstream and the card buttons still work; the timeout
            # does not retry (one-shot), but a later approval.resolved event
            # or snapshot rebuild closes the card out.
            logger.warning("approval timeout resolve unreachable %s: %s", approval_id, exc)
            self._approvals[approval_id] = pending
            return
        # Never auto-approve (§3): timeout = rejected + explicit notification.
        self._drop_pending_feedback(approval_id)
        if pending.card_message_id:
            self._patch_card(
                pending.card_message_id,
                cards.build_approval_expired_card(reason="超时未处理，已自动拒绝"),
            )
        self._send_text(
            pending.owner_chat_id,
            f"⏰ 审批 `{approval_id}` 超过 {self._approval_timeout // 60} 分钟未处理，"
            "已自动拒绝（KITE 不会自动批准）。",
        )
        logger.info(
            "approval timed out and rejected session_id=%s approval_id=%s",
            pending.session_id,
            approval_id,
        )

    # ------------------------------------------------------------------
    # Approval card actions + reject-with-feedback (AppHandler seams)
    # ------------------------------------------------------------------

    def handle_approval_action(
        self,
        action: CardAction,
        *,
        is_admin: Optional[Callable[[str], bool]] = None,
    ) -> CardActionResponse:
        """Approval card buttons (AppHandler E3 seam; runs on the loop).

        Two-phase click guard: the entry flips pending -> processing before
        the REST resolve and rolls back on failure, so a second click while
        processing is a "正在处理中" notice and a click on a missing entry is
        a "已失效或已处理" notice — never an error, never a double-submit.

        Actor check (group-chat contract §3.3): only the prompt initiator or
        an admin may act; a bystander click gets a denial toast and changes
        nothing (no state change, no upstream call — the card stays live for
        the actor). p2p is unchanged: the only human in a p2p chat is an
        admin, which always passes the check.
        """
        name = str(action.value.get("action") or "")
        approval_id = str(action.value.get("approval_id") or "").strip()
        pending = self._approvals.get(approval_id)
        if not approval_id or pending is None or pending.resolved:
            return CardActionResponse(toast=cards.APPROVAL_STALE_NOTICE)
        if pending.status == _APPROVAL_STATUS_PROCESSING:
            return CardActionResponse(toast=cards.APPROVAL_PROCESSING_NOTICE)
        if pending.owner_chat_id != action.chat_id:
            logger.warning(
                "approval action from foreign chat approval=%s chat=%s",
                approval_id,
                action.chat_id,
            )
            return CardActionResponse(toast="该审批只能由发起聊天处理。", toast_type="error")
        if not self._is_interaction_actor(
            pending.prompt_id, action.operator_open_id, is_admin
        ):
            logger.warning(
                "approval action by non-actor approval=%s operator=%s",
                approval_id,
                action.operator_open_id,
            )
            return CardActionResponse(
                toast="只有该 prompt 的发起者或管理员可以处理此审批。", toast_type="error"
            )
        if name == cards.ACTION_APPROVAL_RESOLVE:
            decision = str(action.value.get("decision") or "").strip()
            if decision not in (
                cards.APPROVAL_DECISION_APPROVED,
                cards.APPROVAL_DECISION_REJECTED,
            ):
                return CardActionResponse(toast="未知的审批操作。", toast_type="error")
            return self._resolve_approval_from_card(pending, decision)
        if name == cards.ACTION_APPROVAL_REJECT_WITH_FEEDBACK:
            self._pending_feedback[(action.chat_id, action.operator_open_id)] = _PendingFeedback(
                approval_id=approval_id,
                chat_id=action.chat_id,
                operator_open_id=action.operator_open_id,
            )
            toast = "请直接回复一段文字作为拒绝反馈（你的下一条消息将作为反馈提交"
            # The @ clause only applies where plain text is @-gated: in
            # mention_only/assistant groups non-@ text never reaches the
            # interaction claim; in `all` mode every text does (audit L17).
            group_mode = self._group_mode_of(action.chat_id) if self._group_mode_of else None
            if group_mode in ("mention_only", "assistant"):
                toast += "；群聊中需 @机器人 回复"
            toast += "）。"
            return CardActionResponse(toast=toast)
        return CardActionResponse()

    def handle_abort_action(
        self,
        action: CardAction,
        *,
        is_admin: Optional[Callable[[str], bool]] = None,
    ) -> CardActionResponse:
        """Execution-card cancel button (AppHandler seam; runs on the loop).

        Same permission rule as /abort: the prompt initiator or an admin
        (group-chat §3.3 actor check; bystanders get a denial toast). The
        click is idempotent: an already-finished prompt answers "已结束"
        (upstream 40402), never an error.
        """
        prompt_id = str(action.value.get("prompt_id") or "").strip()
        session_id = str(action.value.get("session_id") or "").strip()
        if not prompt_id or not session_id:
            logger.warning("abort action with malformed value: %r", action.value)
            return CardActionResponse(toast="操作无效。", toast_type="error")
        if not self._is_interaction_actor(prompt_id, action.operator_open_id, is_admin):
            logger.warning(
                "abort action by non-actor prompt=%s operator=%s",
                prompt_id,
                action.operator_open_id,
            )
            return CardActionResponse(
                toast="只有该 prompt 的发起者或管理员可以取消执行。", toast_type="error"
            )
        try:
            self._session_ops.abort_prompt(session_id, prompt_id)
        except KapError as exc:
            if exc.code == KAP_ERROR_PROMPT_NOT_PENDING:
                return CardActionResponse(toast="该 prompt 已结束。")
            logger.warning("abort action upstream error prompt=%s: %s", prompt_id, exc)
            return CardActionResponse(toast=f"中止失败：{exc.msg}", toast_type="error")
        except KapTransportError:
            return CardActionResponse(
                toast="中止失败：无法连接 kap-server，请稍后重试。", toast_type="error"
            )
        logger.info("abort requested from card session=%s prompt=%s", session_id, prompt_id)
        return CardActionResponse(toast="已发起中止。")

    def _initiator_label(self, prompt_id: str) -> str:
        """The initiator's display name for group-facing notices, "" unknown.

        Display names ride the ownership record's sender_open_id through the
        IdentityNames cache (fail-soft: fallback text keeps the old wording).
        """
        entry = self._ownership.entry_of(prompt_id)
        sender = entry.sender_open_id if entry is not None else ""
        if sender and self._names is not None:
            return str(self._names.name_of(sender) or "")
        return ""

    def _is_interaction_actor(
        self,
        prompt_id: str,
        operator_open_id: str,
        is_admin: Optional[Callable[[str], bool]],
    ) -> bool:
        """The actor rule (§3.3): prompt initiator or admin.

        A missing operator identity is treated as a non-member (fail-closed,
        §4.4); an unknown initiator (restart rebuild, control-plane submit)
        fails closed to admin-only.
        """
        operator = str(operator_open_id or "").strip()
        if not operator:
            return False
        entry = self._ownership.entry_of(prompt_id)
        initiator = entry.sender_open_id if entry is not None else ""
        if initiator and operator == initiator:
            return True
        return bool(is_admin is not None and is_admin(operator))

    def _resolve_approval_from_card(
        self, pending: _PendingApproval, decision: str, *, feedback: str = ""
    ) -> CardActionResponse:
        pending.status = _APPROVAL_STATUS_PROCESSING
        try:
            self._ops.resolve_approval(
                pending.session_id, pending.approval_id, decision=decision, feedback=feedback
            )
        except KapError as exc:
            if exc.code == KAP_ERROR_ALREADY_RESOLVED:
                # §4.4: idempotency conflict -> "已被处理", card freezes.
                self._close_approval(pending)
                return CardActionResponse(
                    card=cards.build_approval_resolved_card(decision=""),
                    toast=cards.APPROVAL_ALREADY_PROCESSED_NOTICE,
                )
            pending.status = _APPROVAL_STATUS_PENDING  # roll back: the click may be retried
            return CardActionResponse(toast=f"审批处理失败：{exc.msg}", toast_type="error")
        except KapTransportError:
            pending.status = _APPROVAL_STATUS_PENDING  # roll back on transport failure
            return CardActionResponse(toast=_KAP_UNREACHABLE_TOAST, toast_type="error")
        self._close_approval(pending)
        label = "已批准" if decision == cards.APPROVAL_DECISION_APPROVED else "已拒绝"
        return CardActionResponse(
            card=cards.build_approval_resolved_card(decision=decision, feedback=feedback),
            toast=f"{label}。",
        )

    def _close_approval(self, pending: _PendingApproval) -> None:
        pending.resolved = True
        self._approvals.pop(pending.approval_id, None)
        self._cancel_timer(pending)
        self._drop_pending_feedback(pending.approval_id)

    def _drop_pending_feedback(self, approval_id: str) -> None:
        """Reject-with-feedback step 2 state dies with its approval (audit
        M3): a leftover entry would swallow the user's next plain text into
        ``try_handle_interaction_reply`` ("该审批已处理", never a prompt)."""
        for key, feedback in list(self._pending_feedback.items()):
            if feedback.approval_id == approval_id:
                self._pending_feedback.pop(key, None)

    # ------------------------------------------------------------------
    # Question card actions (AppHandler E3 seam)
    # ------------------------------------------------------------------

    def handle_question_action(
        self,
        action: CardAction,
        *,
        is_admin: Optional[Callable[[str], bool]] = None,
    ) -> CardActionResponse:
        """Question option buttons (AppHandler E3 seam; runs on the loop).

        Actor rule identical to approvals (group-chat §3.3): only the prompt
        initiator or an admin may answer; a bystander click is a denial toast
        with no state change and no upstream call. A valid click answers over
        REST (same answers payload shape as the numbered reply) and freezes
        the clicked card with the chosen label; the matching
        question.answered event then closes the entry and freezes the other
        item cards. A click before that event lands gets the "已回答" notice
        (the entry is kept, marked resolved, so the click never
        double-submits); a click on a gone entry gets "已失效或已处理".
        """
        question_id = str(action.value.get("question_id") or "").strip()
        pending = self._questions.get(question_id)
        if not question_id or pending is None:
            return CardActionResponse(toast=cards.QUESTION_STALE_NOTICE)
        if pending.resolved:
            return CardActionResponse(toast=cards.QUESTION_ALREADY_ANSWERED_NOTICE)
        if pending.owner_chat_id != action.chat_id:
            logger.warning(
                "question action from foreign chat question=%s chat=%s",
                question_id,
                action.chat_id,
            )
            return CardActionResponse(toast="该问题只能由发起聊天回答。", toast_type="error")
        if not self._is_interaction_actor(
            pending.prompt_id, action.operator_open_id, is_admin
        ):
            logger.warning(
                "question action by non-actor question=%s operator=%s",
                question_id,
                action.operator_open_id,
            )
            return CardActionResponse(
                toast="只有该 prompt 的发起者或管理员可以回答此问题。", toast_type="error"
            )
        item_index = action.value.get("item_index")
        label = str(action.value.get("label") or "").strip()
        if (
            not isinstance(item_index, int)
            or isinstance(item_index, bool)
            or not (0 <= item_index < len(pending.items))
            or not label
        ):
            logger.warning("question action with malformed value: %r", action.value)
            return CardActionResponse(toast="操作无效。", toast_type="error")
        item = pending.items[item_index]
        option = next(
            (opt for opt in item.options if opt.label.strip() == label), None
        )
        if option is None:
            logger.warning(
                "question action with unknown label question=%s label=%r",
                question_id,
                label,
            )
            return CardActionResponse(toast="操作无效。", toast_type="error")
        # Mark resolved BEFORE the REST call (click-guard discipline): a
        # nested click while the answer is in flight is a "已回答" notice,
        # never a double-submit. Transport/business failure rolls back so
        # the click may be retried.
        pending.resolved = True
        pending.answered_item_index = item_index
        try:
            self._ops.answer_question(
                pending.session_id,
                question_id,
                {item.item_id: {"kind": "single", "option_id": option.option_id}},
            )
        except KapError as exc:
            if exc.code == KAP_ERROR_ALREADY_RESOLVED:
                # Resolved elsewhere meanwhile: keep the resolved mark and
                # freeze; the entry closes with the answered event.
                return CardActionResponse(
                    card=cards.build_question_dismissed_card(
                        header=item.header,
                        question=item.question,
                        reason="已在其他客户端处理",
                    ),
                    toast=cards.QUESTION_ALREADY_ANSWERED_NOTICE,
                )
            pending.resolved = False
            pending.answered_item_index = None
            return CardActionResponse(toast=f"提交回答失败：{exc.msg}", toast_type="error")
        except KapTransportError:
            pending.resolved = False
            pending.answered_item_index = None
            return CardActionResponse(toast=_KAP_UNREACHABLE_TOAST, toast_type="error")
        if pending.timer is not None:
            pending.timer.cancel()
        logger.info(
            "question answered from card session_id=%s question_id=%s item=%s option=%s",
            pending.session_id,
            question_id,
            item.item_id,
            option.option_id,
        )
        return CardActionResponse(
            card=cards.build_question_dismissed_card(
                header=item.header,
                question=item.question,
                answer_label=option.label.strip(),
            ),
            toast="已回答。",
        )

    # ------------------------------------------------------------------
    # question.* -> option-button cards, numbered-reply fallback, timeouts
    # ------------------------------------------------------------------

    def _question_requested(self, event: QuestionRequested) -> None:
        if event.question_id in self._questions or event.question_id in self._expired_question_ids:
            return  # replayed duplicate (tracked, or already fail-closed)
        prompt_id = self._attribute_prompt(event.session_id, event.turn_id)
        if prompt_id is None:
            logger.warning(
                "question %s cannot be attributed to a prompt; expiring", event.question_id
            )
            self._expire_unroutable_question(event.session_id, event.question_id, None)
            return
        entry = self._ownership.entry_of(prompt_id)
        attached = dict(self._attached_chats(event.session_id))
        owner_chat = entry.chat_id if entry is not None else ""
        if entry is None or entry.certainty != CERTAINTY_CERTAIN or owner_chat not in attached:
            targets = [owner_chat] if owner_chat in attached else list(attached)
            self._expire_unroutable_question(
                event.session_id, event.question_id, targets, prompt_id=prompt_id
            )
            return
        specs = tuple(_question_spec(item) for item in event.items)
        card_message_ids: list[str] = []
        all_cards_sent = True
        for index, spec in enumerate(specs):
            card = cards.build_question_card(
                question_id=event.question_id,
                item_index=index,
                item=spec,
                item_count=len(specs),
                timeout_seconds=self._question_timeout,
            )
            message_id = self._send_card(owner_chat, card)
            if not message_id:
                all_cards_sent = False
                logger.error(
                    "question card send failed chat=%s question=%s item=%d",
                    owner_chat,
                    event.question_id,
                    index,
                )
            card_message_ids.append(message_id)
        if not all_cards_sent:
            # Fail-closed on the fallback surface (§3.9): a missing card
            # would hide its options, so the numbered text goes out too —
            # numbered replies land on the same pending entry.
            self._send_text(
                owner_chat,
                cards.build_question_text(specs, timeout_seconds=self._question_timeout),
            )
        timer = self._start_timer(
            self._question_timeout, self._question_timed_out, event.question_id
        )
        self._questions[event.question_id] = _PendingQuestion(
            question_id=event.question_id,
            session_id=event.session_id,
            prompt_id=prompt_id,
            owner_chat_id=owner_chat,
            items=event.items,
            card_message_ids=tuple(card_message_ids),
            timer=timer,
        )
        logger.info(
            "question requested session_id=%s prompt_id=%s question_id=%s owner=%s",
            event.session_id,
            prompt_id,
            event.question_id,
            owner_chat,
        )
        label = self._initiator_label(prompt_id)
        if label:
            notice = f"⏳ 该会话有一个问题待回答：等待 {label}（`{prompt_id}` 号 prompt 的发起者）处理。"
        else:
            notice = f"⏳ 该会话有一个问题待回答：等待 `{prompt_id}` 号 prompt 的发起者处理。"
        for chat_id in attached:
            if chat_id != owner_chat:
                self._send_text(chat_id, notice)

    def _send_question_expired(
        self,
        session_id: str,
        chat_ids: Optional[Sequence[str]],
        *,
        prompt_id: str = "",
    ) -> None:
        targets = list(chat_ids) if chat_ids is not None else [
            chat_id for chat_id, _ in self._attached_chats(session_id)
        ]
        for chat_id in targets:
            self._send_text(
                chat_id, "该问题已过期（KITE 无法确认发起者）。请在本地直接处理。"
            )
        logger.info(
            "question expired notice posted session=%s prompt=%s targets=%s",
            session_id,
            prompt_id or "-",
            targets,
        )

    def _expire_unroutable_question(
        self,
        session_id: str,
        question_id: str,
        chat_ids: Optional[Sequence[str]],
        *,
        prompt_id: str = "",
    ) -> None:
        """Fail-closed to the end (§4.6): post the expired notice AND dismiss
        the question upstream.

        Same discipline as ``_expire_unroutable_approval`` (audit M2): an
        unroutable question that is only notified stays pending upstream,
        blocking its turn indefinitely. The id is recorded so replays and
        snapshot rebuilds never re-notify or re-dismiss it; the upstream
        dismiss is best-effort (the 40909 success quirk is already absorbed
        by ``KapInteractionOps.dismiss_question``).
        """
        if question_id in self._expired_question_ids:
            return
        self._expired_question_ids.add(question_id)
        self._send_question_expired(session_id, chat_ids, prompt_id=prompt_id)
        try:
            self._ops.dismiss_question(session_id, question_id)
        except (KapError, KapTransportError) as exc:
            logger.warning(
                "unroutable question %s upstream dismiss failed: %s", question_id, exc
            )
            return
        logger.info(
            "unroutable question dismissed upstream session_id=%s question_id=%s",
            session_id,
            question_id,
        )

    def _question_resolved(self, event: QuestionResolved) -> None:
        """Freeze the item cards (resolution may come from any client, e.g.
        the web UI or the local CLI — the resolved event is broadcast)."""
        pending = self._questions.pop(event.question_id, None)
        if pending is None:
            return
        if pending.timer is not None:
            pending.timer.cancel()
        self._patch_question_cards_closed(
            pending,
            reason="已在其他客户端关闭" if event.dismissed else "已在其他客户端回答",
        )
        logger.info(
            "question resolved session_id=%s question_id=%s dismissed=%s",
            event.session_id,
            event.question_id,
            event.dismissed,
        )

    def _patch_question_cards_closed(self, pending: _PendingQuestion, *, reason: str) -> None:
        """Patch every item card to its frozen closed form.

        The card already frozen by the answering click keeps its
        "已回答：<label>" render (it carries the choice; the closed render
        does not know it)."""
        for index, message_id in enumerate(pending.card_message_ids):
            if not message_id or index == pending.answered_item_index:
                continue
            header = question = ""
            if index < len(pending.items):
                header = pending.items[index].header
                question = pending.items[index].question
            self._patch_card(
                message_id,
                cards.build_question_dismissed_card(
                    header=header, question=question, reason=reason
                ),
            )

    def _question_timed_out(self, question_id: str) -> None:
        pending = self._questions.get(question_id)
        if pending is None or pending.resolved:
            # resolved: the timer fire raced the answering click — leave the
            # entry for the question.answered event, which closes it and
            # freezes the remaining item cards (popping it here would strand
            # those cards clickable forever, audit M1).
            return
        self._questions.pop(question_id, None)
        try:
            self._ops.dismiss_question(pending.session_id, question_id)
        except KapError as exc:
            if exc.code in (KAP_ERROR_ALREADY_RESOLVED, KAP_ERROR_QUESTION_NOT_FOUND):
                # Handled upstream meanwhile: freeze as closed elsewhere.
                self._patch_question_cards_closed(pending, reason="已在其他客户端处理")
                return
            # Transient business error: re-add like the approval path does,
            # or the entry is lost while the card stays clickable and the
            # question pends upstream forever (audit M1).
            logger.warning("question dismiss failed %s: %s", question_id, exc)
            self._questions[question_id] = pending
            return
        except KapTransportError as exc:
            # Keep tracking: the question is still pending upstream and the
            # user can still answer it; a rebuild closes it out eventually.
            logger.warning("question dismiss unreachable %s: %s", question_id, exc)
            self._questions[question_id] = pending
            return
        self._patch_question_cards_closed(pending, reason="超时未回复")
        self._send_text(
            pending.owner_chat_id,
            f"⏰ 问题 `{question_id}` 超过 {self._question_timeout // 60} 分钟未回复，已自动关闭。",
        )

    # ------------------------------------------------------------------
    # Fail-close sweep (interaction_request_controller discipline)
    # ------------------------------------------------------------------

    def sweep_session_interactions(
        self,
        session_id: str,
        *,
        owner_chat_id: Optional[str] = None,
        reason: str,
    ) -> int:
        """Sweep one session's pending approvals/questions, optionally only
        those routed to one chat (the /new /switch unbind entry point)."""
        return self._sweep_interactions(
            lambda pending: pending.session_id == session_id
            and (owner_chat_id is None or pending.owner_chat_id == owner_chat_id),
            reason=reason,
        )

    def sweep_all_interactions(self, *, reason: str) -> int:
        """Sweep every pending approval/question (the kited shutdown entry)."""
        return self._sweep_interactions(lambda _pending: True, reason=reason)

    def _sweep_interactions(
        self,
        predicate: Callable[[Any], bool],
        *,
        reason: str,
    ) -> int:
        """Fail-close sweep: respond upstream (approvals -> rejected,
        questions -> dismissed) and patch the cards to expired/closed.

        A swept card never stays clickable; when kap is unreachable the
        upstream respond is skipped but the card is still patched expired
        locally. Returns the number of swept entries.
        """
        swept = 0
        swept_approval_ids: set[str] = set()
        for approval_id, pending in list(self._approvals.items()):
            if not predicate(pending):
                continue
            self._approvals.pop(approval_id, None)
            self._cancel_timer(pending)
            swept += 1
            swept_approval_ids.add(approval_id)
            try:
                self._ops.resolve_approval(
                    pending.session_id,
                    approval_id,
                    decision=cards.APPROVAL_DECISION_REJECTED,
                )
                card: Optional[dict] = cards.build_approval_expired_card(reason=reason)
            except KapError as exc:
                if exc.code in (KAP_ERROR_ALREADY_RESOLVED, KAP_ERROR_APPROVAL_NOT_FOUND):
                    card = cards.build_approval_resolved_card(decision="")
                else:
                    logger.warning("sweep: approval %s resolve failed: %s", approval_id, exc)
                    card = cards.build_approval_expired_card(reason=reason)
            except KapTransportError as exc:
                logger.warning("sweep: approval %s resolve unreachable: %s", approval_id, exc)
                card = cards.build_approval_expired_card(reason=reason)
            if pending.card_message_id:
                self._patch_card(pending.card_message_id, card)
            logger.info(
                "approval swept session_id=%s approval_id=%s reason=%s",
                pending.session_id,
                approval_id,
                reason,
            )
        for key, feedback in list(self._pending_feedback.items()):
            if feedback.approval_id in swept_approval_ids:
                self._pending_feedback.pop(key, None)
        for question_id, pending in list(self._questions.items()):
            if not predicate(pending):
                continue
            self._questions.pop(question_id, None)
            if pending.timer is not None:
                pending.timer.cancel()
            swept += 1
            try:
                self._ops.dismiss_question(pending.session_id, question_id)
            except KapError as exc:
                if exc.code not in (KAP_ERROR_ALREADY_RESOLVED, KAP_ERROR_QUESTION_NOT_FOUND):
                    logger.warning("sweep: question %s dismiss failed: %s", question_id, exc)
            except KapTransportError as exc:
                logger.warning("sweep: question %s dismiss unreachable: %s", question_id, exc)
            # A swept card never stays clickable (fail-closed); the closing
            # notice keeps the reason visible in the chat stream.
            self._patch_question_cards_closed(pending, reason=reason)
            self._send_text(
                pending.owner_chat_id,
                f"问题 `{question_id}` 已关闭（{reason}）。",
            )
            logger.info(
                "question swept session_id=%s question_id=%s reason=%s",
                pending.session_id,
                question_id,
                reason,
            )
        return swept

    # ------------------------------------------------------------------
    # Interaction replies (AppHandler E3 seam; runs on the loop)
    # ------------------------------------------------------------------

    def try_handle_interaction_reply(
        self,
        message: InboundMessage,
        *,
        is_admin: Optional[Callable[[str], bool]] = None,
    ) -> bool:
        """First claim on plain text: approval feedback, then question replies.

        In a group chat the question branch enforces the same actor rule as
        card clicks (§3.3): a non-actor's text is never consumed as an
        answer — it falls through to the prompt path untouched (no state
        change, no upstream call). Approval feedback needs no extra check:
        the pending-feedback key is planted at click time, which is already
        actor-gated.
        """
        feedback = self._pending_feedback.get((message.chat_id, message.sender_open_id))
        if feedback is not None:
            self._handle_feedback_reply(feedback, message.text.strip())
            return True
        question = self._question_for_chat(message.chat_id)
        if question is None:
            return False
        if message.chat_type == "group" and not self._is_interaction_actor(
            question.prompt_id, message.sender_open_id, is_admin
        ):
            return False
        try:
            answers = _parse_question_reply(message.text, question.items)
        except _InvalidReply as exc:
            self._send_text(message.chat_id, f"无法识别回答：{exc}。请按问题里的编号格式回复。")
            return True
        if answers is None:
            return False  # not a reply attempt; let it become a prompt
        self._handle_question_answers(question, answers)
        return True

    def _question_for_chat(self, chat_id: str) -> Optional[_PendingQuestion]:
        for pending in self._questions.values():
            if pending.owner_chat_id == chat_id:
                return pending
        return None

    def _handle_feedback_reply(self, feedback: _PendingFeedback, text: str) -> None:
        pending = self._approvals.get(feedback.approval_id)
        if pending is None or pending.resolved:
            self._pending_feedback.pop((feedback.chat_id, feedback.operator_open_id), None)
            self._send_text(feedback.chat_id, cards.APPROVAL_ALREADY_PROCESSED_NOTICE)
            return
        if not text:
            self._send_text(feedback.chat_id, "反馈不能为空；请重新发送，或忽略以取消。")
            return
        try:
            self._ops.resolve_approval(
                pending.session_id,
                pending.approval_id,
                decision=cards.APPROVAL_DECISION_REJECTED,
                feedback=text,
            )
        except KapError as exc:
            if exc.code == KAP_ERROR_ALREADY_RESOLVED:
                self._pending_feedback.pop((feedback.chat_id, feedback.operator_open_id), None)
                self._close_approval(pending)
                if pending.card_message_id:
                    self._patch_card(
                        pending.card_message_id,
                        cards.build_approval_resolved_card(decision=""),
                    )
                self._send_text(feedback.chat_id, cards.APPROVAL_ALREADY_PROCESSED_NOTICE)
            else:
                self._send_text(feedback.chat_id, f"审批处理失败：{exc.msg}")
            return
        except KapTransportError:
            # Keep the pending feedback so the user can retry (fail-closed).
            self._send_text(feedback.chat_id, f"{_KAP_UNREACHABLE_TOAST}反馈未提交，请重新发送。")
            return
        self._pending_feedback.pop((feedback.chat_id, feedback.operator_open_id), None)
        self._close_approval(pending)
        if pending.card_message_id:
            self._patch_card(
                pending.card_message_id,
                cards.build_approval_resolved_card(
                    decision=cards.APPROVAL_DECISION_REJECTED, feedback=text
                ),
            )
        self._send_text(feedback.chat_id, "已拒绝并提交反馈。")

    def _handle_question_answers(
        self, pending: _PendingQuestion, answers: Mapping[str, Any]
    ) -> None:
        try:
            self._ops.answer_question(pending.session_id, pending.question_id, answers)
        except KapError as exc:
            if exc.code == KAP_ERROR_ALREADY_RESOLVED:
                self._send_text(pending.owner_chat_id, "该问题已被处理。")
                self._question_resolved(
                    QuestionResolved(
                        session_id=pending.session_id,
                        question_id=pending.question_id,
                        dismissed=False,
                    )
                )
            else:
                self._send_text(pending.owner_chat_id, f"提交回答失败：{exc.msg}")
            return
        except KapTransportError:
            self._send_text(pending.owner_chat_id, f"{_KAP_UNREACHABLE_TOAST}回答未提交。")
            return
        self._send_text(pending.owner_chat_id, "已提交回答。")
        # The matching event.question.answered closes the pending entry.

    # ------------------------------------------------------------------
    # Snapshot rebuild (resync + startup recovery)
    # ------------------------------------------------------------------

    def _rebuild_session(self, session_id: str, origin: str) -> None:
        if not session_id or self._shutdown:
            return
        # Fail-closed btw sweep (audit R3-MED-2): a rebuild means durable
        # events may have been lost, so the side-channel tracking is
        # unverifiable — retire it explicitly, on the success AND the
        # failure path below alike (never guess).
        self._sweep_btw_tracking(session_id)
        try:
            snapshot = self._rest.get_snapshot(session_id)
            queue = self._session_ops.get_prompts(session_id)
        except (KapError, KapTransportError) as exc:
            # §4.2/§4.3: freeze the session's cards as "状态未知" with a
            # kitectl hint — never guess the state.
            logger.error(
                "snapshot rebuild failed (%s) session=%s: %s; freezing cards as unknown",
                origin,
                session_id,
                exc,
            )
            self._freeze_session_unknown(session_id)
            return
        self._adopt_snapshot_cursor(session_id, snapshot)
        self._apply_snapshot(session_id, snapshot, queue)
        if self._on_snapshot_rebuilt is not None:
            try:
                self._on_snapshot_rebuilt(session_id, snapshot)
            except Exception:
                logger.exception("on_snapshot_rebuilt hook failed session=%s", session_id)
        # mvp-scope §6: one single-line log per resync/snapshot rebuild
        # carrying session_id + prompt_id; chat_id only where a chat is
        # attributable (the snapshot's current prompt may have a recorded
        # owner — best-effort ownership survives restarts).
        owner_entry = self._ownership.entry_of(snapshot.current_prompt_id)
        logger.info(
            "session rebuilt (%s) chat_id=%s session=%s as_of_seq=%d busy=%s prompt_id=%s",
            origin,
            owner_entry.chat_id if owner_entry is not None else "-",
            session_id,
            snapshot.as_of_seq,
            snapshot.busy,
            snapshot.current_prompt_id or "-",
        )

    def _adopt_snapshot_cursor(self, session_id: str, snapshot: SessionSnapshot) -> None:
        """snapshot.as_of_seq is a cursor source of truth (§5); never move the
        stored cursor backwards (WS replay may already be ahead)."""
        if self._cursor_store is None:
            return
        current = self._cursor_store.get(session_id)
        incoming = snapshot.cursor
        if (
            current is not None
            and current.epoch == incoming.epoch
            and current.seq >= incoming.seq
        ):
            return
        self._cursor_store.set(session_id, incoming)

    def _apply_snapshot(self, session_id: str, snapshot: SessionSnapshot, queue: Any) -> None:
        session = self._session(session_id)
        session.busy = snapshot.busy
        session.pending_interaction = snapshot.pending_interaction
        session.queue_depth = queue.queue_depth
        session.active_prompt_id = queue.active_prompt_id
        session.title = None  # refetch lazily on next card build
        current_prompt = snapshot.current_prompt_id or queue.active_prompt_id
        if snapshot.in_flight and snapshot.in_flight_turn_id is not None and current_prompt:
            session.turn_prompts[snapshot.in_flight_turn_id] = current_prompt
        # Heal the volatile transcript from the snapshot watermark BEFORE the
        # wholesale card refresh below renders it (streaming-cards §1.2).
        self._reseed_transcripts(session_id, snapshot, current_prompt)

        for chat_id, _binding in self._attached_chats(session_id):
            state = self._cards.get(chat_id)
            if snapshot.in_flight and current_prompt:
                if state is not None and state.anchor.matches_prompt(current_prompt):
                    # Wholesale refresh of the in-flight card.
                    state.queue_length = queue.queue_depth
                    self._patch_execution_card(state)
                elif state is not None:
                    # The anchored prompt is no longer in flight: it finished
                    # while we were disconnected and its terminal event is
                    # lost. kap FIFO says it ended; freeze as done, never
                    # fabricate a terminal outcome.
                    logger.warning(
                        "anchored prompt %s no longer in flight after rebuild; freezing",
                        state.anchor.prompt_id,
                    )
                    self._cancel_stream_state(state)
                    self._freeze_card_done(state)
                    self._create_execution_card(
                        chat_id,
                        session_id,
                        current_prompt,
                        "",
                        self._session_title(session_id),
                        queue.queue_depth,
                    )
                else:
                    self._create_execution_card(
                        chat_id,
                        session_id,
                        current_prompt,
                        "",
                        self._session_title(session_id),
                        queue.queue_depth,
                    )
            elif state is not None:
                logger.warning(
                    "session idle after rebuild; freezing anchored card prompt=%s",
                    state.anchor.prompt_id,
                )
                self._cancel_stream_state(state)
                self._freeze_card_done(state)
                del self._cards[chat_id]

        self._rebuild_approvals(session_id, snapshot)
        self._rebuild_questions(session_id, snapshot)

    def _rebuild_approvals(self, session_id: str, snapshot: SessionSnapshot) -> None:
        session = self._session(session_id)
        upstream_pending = {view.approval_id: view for view in snapshot.pending_approvals}
        # Tracked cards whose approval is gone upstream: resolved elsewhere
        # while disconnected -> freeze (decision unknown -> "已处理").
        for approval_id, pending in list(self._approvals.items()):
            if pending.session_id != session_id:
                continue
            if approval_id in upstream_pending:
                continue
            self._approvals.pop(approval_id, None)
            self._cancel_timer(pending)
            self._drop_pending_feedback(approval_id)
            if pending.card_message_id:
                self._patch_card(
                    pending.card_message_id,
                    cards.build_approval_resolved_card(decision=""),
                )
        # Untracked pending approvals (their requested event predates us or
        # was lost): route when ownership is certain, expire otherwise (§4.6).
        for view in upstream_pending.values():
            if view.approval_id in self._approvals:
                continue
            prompt_id = (
                session.turn_prompts.get(view.turn_id)
                if view.turn_id is not None
                else None
            ) or session.active_prompt_id
            if not prompt_id:
                self._expire_unroutable_approval(
                    session_id, view.approval_id, None, reason="无法确定所属 prompt"
                )
                continue
            self._route_approval(
                session_id=session_id,
                approval_id=view.approval_id,
                prompt_id=prompt_id,
                tool_name=view.tool_name,
                action=view.action,
                detail=view.detail,
            )

    def _rebuild_questions(self, session_id: str, snapshot: SessionSnapshot) -> None:
        session = self._session(session_id)
        upstream_pending = {view.question_id: view for view in snapshot.pending_questions}
        for question_id, pending in list(self._questions.items()):
            if pending.session_id != session_id:
                continue
            if question_id in upstream_pending:
                continue
            # Resolved elsewhere while disconnected -> freeze the cards
            # (the chosen answer is unknown -> generic closed render).
            self._questions.pop(question_id, None)
            if pending.timer is not None:
                pending.timer.cancel()
            self._patch_question_cards_closed(pending, reason="已在其他客户端处理")
        for view in upstream_pending.values():
            if view.question_id in self._questions:
                continue
            prompt_id = (
                session.turn_prompts.get(view.turn_id)
                if view.turn_id is not None
                else None
            ) or session.active_prompt_id
            if not prompt_id:
                self._expire_unroutable_question(session_id, view.question_id, None)
                continue
            self._question_requested(
                QuestionRequested(
                    session_id=session_id,
                    question_id=view.question_id,
                    turn_id=view.turn_id,
                    items=view.items,
                )
            )

    def _freeze_session_unknown(self, session_id: str) -> None:
        for chat_id, state in list(self._cards.items()):
            if state.anchor.session_id != session_id:
                continue
            self._cancel_stream_state(state)
            self._patch_frozen_execution_card(state, cards.EXECUTION_STATE_FROZEN_UNKNOWN)
            del self._cards[chat_id]

    # ------------------------------------------------------------------
    # Card/transport helpers
    # ------------------------------------------------------------------

    def _attached_chats(self, session_id: str) -> list[tuple[str, StoredBinding]]:
        return [
            (chat_id, binding)
            for chat_id, binding in self._binding_store.load_all().items()
            if binding["session_id"] == session_id and binding["attached"]
        ]

    def _cards_for_prompt(self, session_id: str, prompt_id: str) -> list[_ExecutionCardState]:
        """Anchor rule (§6): an event may touch a card iff the anchor matches."""
        return [
            state
            for state in self._cards.values()
            if state.anchor.session_id == session_id and state.anchor.matches_prompt(prompt_id)
        ]

    def _session_title(self, session_id: str) -> str:
        session = self._session(session_id)
        if session.title is not None:
            return session.title
        try:
            info = self._session_ops.get_session(session_id)
        except (KapError, KapTransportError) as exc:
            # Do NOT cache the failure (audit L8): a transient REST error
            # must not pin an empty title forever — retry on the next card.
            logger.warning("session title fetch failed session=%s: %s", session_id, exc)
            return ""
        session.title = info.title
        return session.title

    def _elapsed(self, state: _ExecutionCardState) -> int:
        return max(int(self._monotonic() - state.started_at), 0)

    def _patch_execution_card(self, state: _ExecutionCardState) -> None:
        """Patch the running card (tool lines / queue depth / wholesale
        refresh).

        Through the dispatcher (audit R-4, same discipline as the stream and
        freeze paths): a Feishu 230020 on a tool-line patch is requeued
        after ``retry_after`` instead of silently dropped. The render stays
        full-snapshot at patch time (§3.1); a stale anchor renders None.
        """
        message_id = state.anchor.card_message_id
        if not message_id:
            return
        self._dispatcher.submit(message_id, lambda: self._render_running_card(state))

    def _send_card(self, chat_id: str, card: dict) -> str:
        """Send a card; returns its message id ("" on failure)."""
        try:
            return str(
                self._transport.send_message_get_id(chat_id, "interactive", json.dumps(card))
                or ""
            ).strip()
        except Exception:
            logger.exception("card send failed chat=%s", chat_id)
            return ""

    def _patch_card(self, message_id: str, card: dict) -> bool:
        return self._patch_card_result(message_id, card).ok

    def _patch_card_result(self, message_id: str, card: dict) -> MessagePatchResult:
        if not message_id:
            return MessagePatchResult.failure()
        return self._patch_message_result(message_id, json.dumps(card))

    def _send_text(self, chat_id: str, text: str) -> None:
        try:
            self._transport.reply(chat_id, text)
        except Exception:
            logger.exception("text send failed chat=%s", chat_id)

    # ------------------------------------------------------------------
    # Timers / lifecycle
    # ------------------------------------------------------------------

    def _start_timer(
        self, delay_seconds: int, handler: Callable[[str], None], key: str
    ) -> Optional[TimerHandle]:
        if delay_seconds <= 0:
            return None

        def _fire() -> None:
            try:
                self._loop.submit(handler, key)
            except Exception:  # loop closed during shutdown
                logger.debug("timer fired after loop close: %s", key)

        try:
            return self._timer_factory(delay_seconds, _fire)
        except Exception:
            logger.exception("timer start failed for %s", key)
            return None

    @staticmethod
    def _cancel_timer(pending: _PendingApproval) -> None:
        if pending.timer is not None:
            pending.timer.cancel()

    def _shutdown_impl(self) -> None:
        self._shutdown = True
        # Fail-close sweep (mvp-scope aligned item 8): respond upstream
        # (approvals rejected, questions dismissed) and patch the cards
        # expired/closed. kited keeps kap-server up until this has run; if it
        # is unreachable anyway, the cards are still patched locally.
        swept = self.sweep_all_interactions(reason="KITE 服务已停止")
        if swept:
            logger.info("shutdown sweep closed %d pending interaction(s)", swept)
        # Timer hygiene (streaming-cards §3.8): trailing stream timers and the
        # dispatcher's retry timers are cancelled; queued renders guard on the
        # shutdown flag and become no-ops.
        for state in self._cards.values():
            if state.stream_timer is not None:
                state.stream_timer.cancel()
                state.stream_timer = None
        self._dispatcher.shutdown()
        for timer in self._terminal_retry_timers.values():
            timer.cancel()
        self._terminal_retry_timers.clear()


# ---------------------------------------------------------------------------
# Question reply parsing (MVP numbered convention; see cards.build_question_text)
# ---------------------------------------------------------------------------


def _question_spec(item: QuestionItemView) -> cards.QuestionItemSpec:
    return cards.QuestionItemSpec(
        question=item.question,
        header=item.header,
        options=tuple(
            cards.QuestionOptionSpec(label=option.label, description=option.description)
            for option in item.options
        ),
        multi_select=item.multi_select,
        allow_other=item.allow_other,
    )


_OTHER_PREFIX = re.compile(r"^其他\s*[:：]\s*(.+)$", re.S)
_SINGLE_NUMBERS = re.compile(r"\d+(?:\s*[,，]\s*\d+)*")
_MULTI_LINE = re.compile(r"(\d+)\s*[:：]\s*(\d+(?:\s*[,，]\s*\d+)*)")


def _parse_question_reply(
    text: str, items: tuple[QuestionItemView, ...]
) -> Optional[dict[str, Any]]:
    """Parse a numbered question reply into the kap answers payload.

    Returns None when the text does not look like a reply attempt (it should
    become a normal prompt); raises _InvalidReply when it looks like one but
    fails validation. Single question: `1` / `1,3` / `其他：…`; multiple
    questions: one `问题号:选项号` per line.
    """
    stripped = str(text or "").strip()
    if not stripped or not items:
        return None
    other = _OTHER_PREFIX.match(stripped)
    if other is not None:
        if len(items) != 1:
            raise _InvalidReply("多问题请按 `问题号:选项号` 逐行回复")
        item = items[0]
        if not item.allow_other:
            raise _InvalidReply("该问题不支持自定义回答")
        content = other.group(1).strip()
        if not content:
            raise _InvalidReply("自定义回答不能为空")
        return {item.item_id: {"kind": "other", "text": content}}
    if len(items) == 1:
        if not _SINGLE_NUMBERS.fullmatch(stripped):
            return None
        numbers = [int(part) for part in re.split(r"[,，]", stripped)]
        item = items[0]
        return {item.item_id: _numbered_answer(item, numbers)}
    answers: dict[str, Any] = {}
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    for line in lines:
        match = _MULTI_LINE.fullmatch(line)
        if match is None:
            return None
    for line in lines:
        match = _MULTI_LINE.fullmatch(line)
        assert match is not None
        item_index = int(match.group(1))
        if not (1 <= item_index <= len(items)):
            raise _InvalidReply(f"问题号 {item_index} 超出范围（共 {len(items)} 个）")
        numbers = [int(part) for part in re.split(r"[,，]", match.group(2))]
        item = items[item_index - 1]
        answers[item.item_id] = _numbered_answer(item, numbers)
    return answers


def _numbered_answer(item: QuestionItemView, numbers: Sequence[int]) -> dict[str, Any]:
    options = item.options
    if not options:
        raise _InvalidReply("该问题没有可编号的选项")
    for number in numbers:
        if not (1 <= number <= len(options)):
            raise _InvalidReply(f"选项 {number} 超出范围（共 {len(options)} 个）")
    if len(numbers) == 1:
        return {"kind": "single", "option_id": options[numbers[0] - 1].option_id}
    if not item.multi_select:
        raise _InvalidReply("该问题为单选，请只回复一个编号")
    return {"kind": "multi", "option_ids": [options[n - 1].option_id for n in numbers]}


# ---------------------------------------------------------------------------
# AppHandler seam wiring
# ---------------------------------------------------------------------------


class OutboundAppHandler(AppHandler):
    """AppHandler with the E3 seams wired to the outbound pipeline."""

    def __init__(self, *, event_pipeline: EventPipeline, **kwargs: Any) -> None:
        # The /btw FIFO-attribution seam (aligned item 13): the pipeline
        # learns each side-channel submission so the side turn's events can
        # find their initiating chat.
        kwargs.setdefault("on_btw_prompt_submitted", event_pipeline.note_btw_prompt)
        super().__init__(**kwargs)
        self._event_pipeline = event_pipeline

    def handle_approval_action(self, action: CardAction) -> CardActionResponse:
        # The actor check (group-chat §3.3) needs the admin set, which the
        # handler owns; the pipeline owns the pending-approval state.
        return self._event_pipeline.handle_approval_action(action, is_admin=self._is_admin)

    def handle_abort_action(self, action: CardAction) -> CardActionResponse:
        # Same split as approvals: the pipeline owns the abort, the handler
        # owns the admin set for the actor check.
        return self._event_pipeline.handle_abort_action(action, is_admin=self._is_admin)

    def handle_question_action(self, action: CardAction) -> CardActionResponse:
        # Same split as approvals: the pipeline owns the pending-question
        # state, the handler owns the admin set for the actor check.
        return self._event_pipeline.handle_question_action(action, is_admin=self._is_admin)

    def try_handle_interaction_reply(self, message: InboundMessage) -> bool:
        return self._event_pipeline.try_handle_interaction_reply(
            message, is_admin=self._is_admin
        )

    def on_session_unbound(self, chat_id: str, old_session_id: str) -> None:
        # Fail-close sweep of the old session's pending approvals/questions
        # routed to this chat (we are on the loop: commands run serialized).
        self._event_pipeline.sweep_session_interactions(
            old_session_id,
            owner_chat_id=chat_id,
            reason="发起聊天已切换到其他会话",
        )


class SwappableKapRest:
    """Thread-safe indirection over the current KapRestClient.

    kited swaps the delegate on every kap-server incarnation (port/token
    change), so the long-lived handler/pipeline never hold a stale client.
    Before the first swap every call fails closed with KapTransportError.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: Any = None

    def set_client(self, client: Any) -> None:
        with self._lock:
            self._client = client

    def _current(self) -> Any:
        with self._lock:
            client = self._client
        if client is None:
            raise KapTransportError("kap-server 尚未就绪")
        return client

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return self._current().call(method, path, body)

    def get(self, path: str) -> Any:
        return self._current().get(path)

    def post(self, path: str, body: Any = None) -> Any:
        return self._current().post(path, body)

    def list_sessions(self) -> Any:
        return self._current().list_sessions()

    def get_prompts(self, session_id: str) -> Any:
        return self._current().get_prompts(session_id)

    def get_snapshot(self, session_id: str) -> Any:
        return self._current().get_snapshot(session_id)


class WsSubscriptionHook:
    """The on_session_bound hook: live-subscribe the CURRENT WS client.

    kited swaps the delegate per kap incarnation; when no WS is up the
    subscription is deferred to the startup resubscribe of persisted bindings
    (the binding is already on disk by the time this hook fires).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ws: Any = None

    def set_client(self, ws: Any) -> None:
        with self._lock:
            self._ws = ws

    def __call__(self, session_id: str) -> None:
        with self._lock:
            ws = self._ws
        if ws is None:
            logger.info(
                "no WS client yet; session %s will be subscribed at startup", session_id
            )
            return
        ws.subscribe(session_id)

    def resubscribe_after_rebuild(self, session_id: str) -> None:
        """The pipeline's post-rebuild re-subscribe seam (audit M7), forwarded
        to the live WS client (which owns the anti-tight-loop guard)."""
        with self._lock:
            ws = self._ws
        if ws is None:
            return
        ws.resubscribe_after_rebuild(session_id)
