"""
kap-server adapter.

This is the ONLY module in the repository allowed to know kap-server's wire
schemas: the REST envelope ``{code, msg, data, request_id}``, error codes, the
WS frame protocol (``server_hello`` / ``client_hello`` / ``subscribe`` /
``ack`` / ``resync_required`` / event envelopes), the server-side file layout
(``server.token``, ``server/instances/<id>.json``), and the
``KIMI_MODEL_*`` env overlay. Everything else in KITE consumes the normalized
dataclasses defined here.

Upstream facts this module encodes (evidence: docs/verification/spike-results.md
and ``~/llm/kimi/kimi-code/packages/kap-server``):

- REST envelope: HTTP 200 with ``code == 0`` means success; anything else is a
  business error. ``POST /shutdown`` must not carry a ``Content-Type`` header
  with an empty body (Fastify rejects it with 50001).
- ``GET /sessions/{id}/prompts`` is resume-backed: touching it "warms" a cold
  session so a subsequent WS subscribe does not yield an unexplained
  ``resync_required`` (lazy activation via ``ISessionLifecycleService.get``).
- Cursor source of truth: WS ack ``payload.cursors`` and snapshot
  ``as_of_seq``/``epoch``. REST ``session.last_seq`` is a hardcoded
  placeholder 0 — never used here.
- A standalone ``resync_required`` frame may arrive BEFORE the subscribe ack;
  replayed event frames also precede the ack. Frames are therefore dispatched
  in strict wire order while awaiting acks.
- WS has no heartbeat: the client treats "no frame of any kind for N seconds"
  as a stale connection and reconnects.
- Port: default 58627; on conflict the server retries with port+1, so the
  actual port is resolved from the instance registry
  (``<KIMI_CODE_HOME>/server/instances/*.json``), never assumed.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import websockets.exceptions
import websockets.sync.client

from kite.process_utils import process_exists
from kite.stores.event_cursor_store import EventCursor, EventCursorStore

logger = logging.getLogger("kite.adapters.kap")

API_PREFIX = "/api/v1"
DEFAULT_KAP_PORT = 58627
DEFAULT_KAP_HOST = "127.0.0.1"

# The kimi-code version KITE's adapter was last verified against
# (docs/architecture/kite-design.md §10: follow, don't pin — warn, don't block).
VERIFIED_KIMI_VERSION = "0.29.0"

# Env vars passed through to the kap-server child process. Everything else is
# dropped so the child environment is explicit and reproducible.
CHILD_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)

# Replay-window default upstream (constructor option, not runtime-configurable).
# Used only to annotate resync reasons; the server is the source of truth.
DEFAULT_REPLAY_WINDOW = 1000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KapTransportError(Exception):
    """The server could not be reached or the reply was not a kap envelope."""


class KapError(Exception):
    """A kap business error (envelope ``code != 0``)."""

    def __init__(
        self,
        code: int,
        msg: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"kap error {code}: {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status
        self.request_id = request_id


class KapWsError(Exception):
    """A WS protocol violation or handshake failure."""


# ---------------------------------------------------------------------------
# Normalized types (the only kap-derived types the rest of KITE may see)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerMeta:
    server_version: str
    server_id: str
    backend: str
    started_at: str


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    cwd: str | None
    busy: bool
    pending_interaction: str | None
    archived: bool
    # ISO-8601 upstream activity timestamp; "" when the server omits it.
    # Lexicographic compare works for the server's uniform format.
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class PromptQueueState:
    active_prompt_id: str | None
    queued_prompt_ids: tuple[str, ...]

    @property
    def queue_depth(self) -> int:
        return len(self.queued_prompt_ids)


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    """The pending-approval projection the rebuild path needs (wire
    ``approvalRequestSchema``; see kap-server ``routes/approvals.toWireApproval``).

    ``detail`` is a plain-text salient field extracted from
    ``tool_input_display`` (command/path/query/url/summary, kind-prefixed);
    presentation is the application layer's job.
    """

    approval_id: str
    turn_id: int | None
    tool_call_id: str
    tool_name: str
    action: str
    detail: str


@dataclass(frozen=True, slots=True)
class QuestionOptionView:
    """One wire question option (id synthesized upstream: ``opt_<q>_<o>``)."""

    option_id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class QuestionItemView:
    """One wire question item (``q_<index>``) with its selectable options."""

    item_id: str
    question: str
    header: str
    options: tuple[QuestionOptionView, ...]
    multi_select: bool
    allow_other: bool


@dataclass(frozen=True, slots=True)
class QuestionRequestView:
    """The pending-question projection the rebuild path needs (wire
    ``questionRequestSchema``; see kap-server ``routes/questions.toWireQuestion``)."""

    question_id: str
    turn_id: int | None
    items: tuple[QuestionItemView, ...]


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The rebuild watermark + work state from ``GET .../snapshot``."""

    as_of_seq: int
    epoch: str
    busy: bool
    pending_interaction: str | None
    current_prompt_id: str | None
    in_flight: bool
    pending_approval_ids: tuple[str, ...]
    pending_question_ids: tuple[str, ...]
    in_flight_turn_id: int | None = None
    pending_approvals: tuple[ApprovalRequestView, ...] = ()
    pending_questions: tuple[QuestionRequestView, ...] = ()
    # Assistant text of the in-flight turn's current step (volatile healing:
    # step-relative, exactly like the delta offsets, so it re-baselines both).
    in_flight_assistant_text: str = ""

    @property
    def cursor(self) -> EventCursor:
        return EventCursor(seq=self.as_of_seq, epoch=self.epoch)


@dataclass(frozen=True, slots=True)
class KapEvent:
    """One normalized WS event frame (durable or volatile)."""

    type: str
    session_id: str | None
    seq: int | None
    epoch: str | None
    volatile: bool
    offset: int | None
    timestamp: str | None
    payload: Any


@dataclass(frozen=True, slots=True)
class ResyncRequest:
    """The server told us our cursor for a session is unusable.

    ``reason``/``current_seq``/``epoch`` are only present when the standalone
    ``resync_required`` frame carried them; an ack-listed resync (e.g. the
    cold-session case) leaves them None. The receiver must rebuild from a REST
    snapshot and re-store the cursor from ``snapshot.cursor``.
    """

    session_id: str
    reason: str | None
    current_seq: int | None
    epoch: str | None


# ---------------------------------------------------------------------------
# Typed durable events (the outbound path consumes ONLY these)
# ---------------------------------------------------------------------------
#
# ``KapEvent.payload`` is the raw protocol event dict; reading its keys is
# schema knowledge and therefore lives here. ``normalize_durable_event`` maps
# the durable slice of the v1 event catalog (kite-design.md §5) onto the typed
# dataclasses below; volatile and unknown types normalize to None. Upstream
# field facts (packages/protocol/src/events.ts + the kap-server interaction
# synthesis in transport/ws/v1/sessionEventBroadcaster.ts):
#
# - core agent events use camelCase (turnId / toolCallId / promptId /
#   activePromptId / promptIds);
# - approval/question events are synthesized from the interaction kernel and
#   use the REST wire projections (snake_case: approval_id / question_id /
#   turn_id / tool_call_id);
# - every agent-event payload carries the broadcaster-stamped ``agentId``
#   (``{...event, agentId, sessionId}`` in sessionEventBroadcaster.ts): the
#   main agent is ``"main"``, a `/btw` side-channel agent carries its forked
#   id; the field is normalized onto every typed event as ``agent_id`` (""
#   when absent) so the pipeline can route by agent (audit N3-HIGH-1);
# - turn.started carries NO prompt id — prompt attribution is the receiver's
#   job (kap prompt FIFO: the turn belongs to the active prompt);
# - question resolution arrives as event.question.answered or
#   event.question.dismissed; both fold into QuestionResolved.


@dataclass(frozen=True, slots=True)
class TurnStarted:
    session_id: str
    turn_id: int
    prompt_text: str
    origin_kind: str
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class TurnEnded:
    session_id: str
    turn_id: int
    reason: str  # completed | cancelled | failed | blocked
    error_message: str
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    session_id: str
    turn_id: int
    tool_call_id: str
    name: str
    description: str
    detail: str  # salient field from the display payload, kind-prefixed
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    session_id: str
    turn_id: int
    tool_call_id: str
    is_error: bool
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class PromptAborted:
    session_id: str
    prompt_id: str
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class PromptSteered:
    session_id: str
    active_prompt_id: str
    prompt_ids: tuple[str, ...]
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    session_id: str
    approval_id: str
    turn_id: int | None
    tool_call_id: str
    tool_name: str
    action: str
    detail: str
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    session_id: str
    approval_id: str
    decision: str  # approved | rejected | cancelled
    feedback: str
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class QuestionRequested:
    session_id: str
    question_id: str
    turn_id: int | None
    items: tuple[QuestionItemView, ...]
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class QuestionResolved:
    session_id: str
    question_id: str
    dismissed: bool
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class SessionWorkChanged:
    session_id: str
    busy: bool
    pending_interaction: str | None
    last_turn_reason: str | None
    agent_id: str = ""


DurableEvent = (
    TurnStarted
    | TurnEnded
    | ToolCallStarted
    | ToolCallResult
    | PromptAborted
    | PromptSteered
    | ApprovalRequested
    | ApprovalResolved
    | QuestionRequested
    | QuestionResolved
    | SessionWorkChanged
)


def normalize_durable_event(event: KapEvent) -> DurableEvent | None:
    """Map a normalized KapEvent to a typed durable event.

    Returns None for volatile types, unknown types, events without a session
    id, and malformed payloads (logged; a bad frame must not kill the stream
    and must never be guessed at).
    """
    if event.volatile or not event.session_id:
        return None
    payload = event.payload
    if not isinstance(payload, dict):
        if event.type in _DURABLE_EVENT_TYPES:
            logger.warning("durable event %s with non-dict payload dropped", event.type)
        return None
    session_id = event.session_id
    # Broadcaster-stamped emitting agent ("main" or a forked side-channel id);
    # "" when the frame predates/omits it — routed as the main agent.
    agent_id = _optional_str(payload.get("agentId")) or ""
    try:
        if event.type == "turn.started":
            turn_id = _optional_non_negative_int(payload.get("turnId"))
            if turn_id is None:
                raise ValueError("turnId missing")
            origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
            return TurnStarted(
                session_id=session_id,
                turn_id=turn_id,
                prompt_text=str(payload.get("prompt") or ""),
                origin_kind=str(origin.get("kind") or ""),
                agent_id=agent_id,
            )
        if event.type == "turn.ended":
            turn_id = _optional_non_negative_int(payload.get("turnId"))
            if turn_id is None:
                raise ValueError("turnId missing")
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            return TurnEnded(
                session_id=session_id,
                turn_id=turn_id,
                reason=str(payload.get("reason") or ""),
                error_message=str(error.get("message") or ""),
                agent_id=agent_id,
            )
        if event.type == "tool.call.started":
            turn_id = _optional_non_negative_int(payload.get("turnId"))
            tool_call_id = _optional_str(payload.get("toolCallId"))
            if turn_id is None or not tool_call_id:
                raise ValueError("turnId/toolCallId missing")
            return ToolCallStarted(
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                detail=_tool_display_detail(payload.get("display")),
                agent_id=agent_id,
            )
        if event.type == "tool.result":
            turn_id = _optional_non_negative_int(payload.get("turnId"))
            tool_call_id = _optional_str(payload.get("toolCallId"))
            if turn_id is None or not tool_call_id:
                raise ValueError("turnId/toolCallId missing")
            return ToolCallResult(
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                is_error=bool(payload.get("isError")),
                agent_id=agent_id,
            )
        if event.type == "prompt.aborted":
            prompt_id = _optional_str(payload.get("promptId"))
            if not prompt_id:
                raise ValueError("promptId missing")
            return PromptAborted(session_id=session_id, prompt_id=prompt_id, agent_id=agent_id)
        if event.type == "prompt.steered":
            prompt_ids = tuple(
                pid for pid in (_optional_str(item) for item in _as_list(payload.get("promptIds"))) if pid
            )
            return PromptSteered(
                session_id=session_id,
                active_prompt_id=str(payload.get("activePromptId") or ""),
                prompt_ids=prompt_ids,
                agent_id=agent_id,
            )
        if event.type == "event.approval.requested":
            view = _parse_approval_request(payload)
            if view is None:
                raise ValueError("approval payload missing required fields")
            return ApprovalRequested(
                session_id=session_id,
                approval_id=view.approval_id,
                turn_id=view.turn_id,
                tool_call_id=view.tool_call_id,
                tool_name=view.tool_name,
                action=view.action,
                detail=view.detail,
                agent_id=agent_id,
            )
        if event.type == "event.approval.resolved":
            approval_id = _optional_str(payload.get("approval_id"))
            if not approval_id:
                raise ValueError("approval_id missing")
            return ApprovalResolved(
                session_id=session_id,
                approval_id=approval_id,
                decision=str(payload.get("decision") or ""),
                feedback=str(payload.get("feedback") or ""),
                agent_id=agent_id,
            )
        if event.type == "event.question.requested":
            view = _parse_question_request(payload)
            if view is None:
                raise ValueError("question payload missing required fields")
            return QuestionRequested(
                session_id=session_id,
                question_id=view.question_id,
                turn_id=view.turn_id,
                items=view.items,
                agent_id=agent_id,
            )
        if event.type in ("event.question.answered", "event.question.dismissed"):
            question_id = _optional_str(payload.get("question_id"))
            if not question_id:
                raise ValueError("question_id missing")
            return QuestionResolved(
                session_id=session_id,
                question_id=question_id,
                dismissed=event.type == "event.question.dismissed",
                agent_id=agent_id,
            )
        if event.type == "event.session.work_changed":
            return SessionWorkChanged(
                session_id=session_id,
                busy=bool(payload.get("busy")),
                pending_interaction=_optional_pending_interaction(payload.get("pending_interaction")),
                last_turn_reason=_optional_str(payload.get("last_turn_reason")),
                agent_id=agent_id,
            )
    except ValueError as exc:
        logger.warning("durable event %s dropped: %s", event.type, exc)
        return None
    return None


# Durable types the outbound path recognizes (for drop logging).
_DURABLE_EVENT_TYPES = frozenset(
    {
        "turn.started",
        "turn.ended",
        "tool.call.started",
        "tool.result",
        "prompt.aborted",
        "prompt.steered",
        "event.approval.requested",
        "event.approval.resolved",
        "event.question.requested",
        "event.question.answered",
        "event.question.dismissed",
        "event.session.work_changed",
    }
)


# ---------------------------------------------------------------------------
# Typed volatile events (the streaming side-channel; docs/contracts/streaming-cards.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssistantDelta:
    """One normalized volatile ``assistant.delta`` frame.

    ``offset`` is the pre-append offset of this delta within the turn's
    current step, stamped by the server's in-flight tracker (it resets at
    every ``turn.step.started``); it is the gap-detection input for the
    streaming transcript. Volatile frames never advance the durable cursor.

    ``agent_id`` is the broadcaster-stamped emitting agent ("" when absent —
    routed as the main agent). The tracker only annotates MAIN-agent turns
    (inFlightTurnTracker.ts: subagent deltas share the session id but
    describe a different stream), so a side-channel (`/btw`) agent's deltas
    arrive WITHOUT an offset and accumulate in arrival order instead
    (``offset`` is None for them).
    """

    session_id: str
    offset: int | None
    text_delta: str
    agent_id: str = ""


def normalize_volatile_event(event: KapEvent) -> AssistantDelta | None:
    """Map a normalized KapEvent to a typed volatile event.

    Only ``assistant.delta`` is in scope (streaming-cards contract §2:
    thinking/tool/shell/status deltas are explicit non-goals); everything
    else normalizes to None. A main-agent delta without a usable offset or
    payload is dropped — the missing text must never be guessed at (the
    durable path still produces correct cards without it, §4.3). A
    side-channel agent's delta is kept without an offset (upstream never
    stamps one for non-main agents; the btw path accumulates in arrival
    order, audit N3-HIGH-1).
    """
    if not event.volatile or not event.session_id or event.type != "assistant.delta":
        return None
    payload = event.payload
    if not isinstance(payload, dict):
        logger.warning("volatile event %s with non-dict payload dropped", event.type)
        return None
    delta = payload.get("delta")
    if not isinstance(delta, str) or not delta:
        return None
    agent_id = _optional_str(payload.get("agentId")) or ""
    if event.offset is None and agent_id in ("", "main"):
        logger.warning("assistant.delta without an offset dropped (cannot gap-check)")
        return None
    return AssistantDelta(
        session_id=event.session_id,
        offset=event.offset,
        text_delta=delta,
        agent_id=agent_id,
    )


@dataclass(frozen=True, slots=True)
class LiveServer:
    """A live kap-server instance from the on-disk instance registry."""

    pid: int
    port: int
    server_id: str


# ---------------------------------------------------------------------------
# Server home / binary / token / registry helpers
# ---------------------------------------------------------------------------


def default_kap_home() -> pathlib.Path:
    raw = os.environ.get("KIMI_CODE_HOME", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return pathlib.Path.home() / ".kimi-code"


def resolve_kap_home(configured: str | None = None) -> pathlib.Path:
    """Home resolution order: explicit config → $KIMI_CODE_HOME → ~/.kimi-code."""
    if configured and str(configured).strip():
        return pathlib.Path(str(configured)).expanduser()
    return default_kap_home()


def resolve_kimi_bin(configured: str | None = None) -> str | None:
    """Binary resolution order: explicit config → $KIMI_BIN → PATH lookup."""
    if configured and str(configured).strip():
        return str(configured).strip()
    env_bin = os.environ.get("KIMI_BIN", "").strip()
    if env_bin:
        return env_bin
    return shutil.which("kimi")


def server_token_path(home: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(home) / "server.token"


def read_server_token(home: pathlib.Path) -> str:
    path = server_token_path(home)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"server token file is empty: {path}")
    return token


def read_kimi_default_model(home: pathlib.Path) -> str | None:
    """``default_model`` from ``<home>/config.toml``; None when absent/unreadable."""
    path = pathlib.Path(home) / "config.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("default_model")
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_prompt_model(config_model: str | None, home: pathlib.Path) -> str | None:
    """The model carried explicitly on every prompt.

    REST-created sessions inherit neither the ``KIMI_MODEL_*`` env overlay
    nor ``config.toml``'s ``default_model`` (spike-results §0, extended by
    the 2026-07-22 live finding), so every submit must name a model.
    Resolution order: ``kap.model`` config → ``config.toml`` ``default_model``
    → None (the submit then fails upstream and the WS error frame surfaces
    it; fail-closed).
    """
    if config_model and str(config_model).strip():
        return str(config_model).strip()
    return read_kimi_default_model(home)


def _instance_registry_dir(home: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(home) / "server" / "instances"


def find_live_server(home: pathlib.Path) -> LiveServer | None:
    """Return the first live instance in the registry, or None.

    Used by local CLI tools to discover the actual port of the running
    kap-server (the requested port may have been bumped on conflict).
    """
    registry = _instance_registry_dir(home)
    try:
        names = sorted(registry.iterdir())
    except OSError:
        return None
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            port = int(info["port"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if not process_exists(pid):
            continue
        return LiveServer(pid=pid, port=port, server_id=path.stem)
    return None


def detect_kimi_version(kimi_bin: str, *, timeout: float = 10.0) -> str | None:
    """Best-effort ``kimi --version`` probe; None when undeterminable."""
    try:
        result = subprocess.run(
            [kimi_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\d+\.\d+\.\d+", output)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Child environment construction
# ---------------------------------------------------------------------------


def build_child_env(
    home: pathlib.Path,
    env_overlay: Mapping[str, str] | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the kap-server child environment.

    - passthrough of a fixed allowlist from the current environment,
    - the kite env-file overlay (provider credentials; see kite/env_file.py),
    - ``KIMI_CODE_HOME`` pinned to the managed home,
    - the ``KIMI_MODEL_*`` overlay mapping: an isolated home has no provider
      config, and agent-core-v2 synthesizes one from these vars (spike §0).
    """
    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in CHILD_PASSTHROUGH_ENV if key in source}
    for key, value in (env_overlay or {}).items():
        env[str(key)] = str(value)
    env["KIMI_CODE_HOME"] = str(home)
    if "KIMI_MODEL_API_KEY" not in env and env.get("KIMI_API_KEY"):
        env["KIMI_MODEL_API_KEY"] = env["KIMI_API_KEY"]
    if "KIMI_MODEL_BASE_URL" not in env and env.get("KIMI_BASE_URL"):
        env["KIMI_MODEL_BASE_URL"] = env["KIMI_BASE_URL"]
    return env


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class KapRestClient:
    """Synchronous REST client with envelope unwrapping and Bearer auth."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._base = f"http://{host}:{port}{API_PREFIX}"
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Call the API and return the unwrapped ``data``.

        Raises KapError on business errors (code != 0) and KapTransportError
        on transport/protocol failures. A None body sends no body and — per
        the Fastify empty-body gotcha — no Content-Type header.
        """
        url = self._base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return self._unwrap(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return self._unwrap_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise KapTransportError(f"{method} {path}: {exc}") from exc

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body)

    def _unwrap_http_error(self, exc: urllib.error.HTTPError) -> Any:
        try:
            raw = exc.read()
        except OSError as read_exc:
            raise KapTransportError(f"HTTP {exc.code}: unreadable body: {read_exc}") from read_exc
        try:
            envelope = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError) as parse_exc:
            raise KapTransportError(f"HTTP {exc.code}: non-envelope reply") from parse_exc
        if isinstance(envelope, dict) and isinstance(envelope.get("code"), int):
            raise KapError(
                envelope["code"],
                str(envelope.get("msg") or ""),
                http_status=exc.code,
                request_id=_optional_str(envelope.get("request_id")),
            )
        raise KapTransportError(f"HTTP {exc.code}: non-envelope reply")

    @staticmethod
    def _unwrap(http_status: int, raw: bytes) -> Any:
        try:
            envelope = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise KapTransportError(f"HTTP {http_status}: non-envelope reply") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), int):
            raise KapTransportError(f"HTTP {http_status}: non-envelope reply")
        if envelope["code"] != 0:
            raise KapError(
                envelope["code"],
                str(envelope.get("msg") or ""),
                http_status=http_status,
                request_id=_optional_str(envelope.get("request_id")),
            )
        return envelope.get("data")

    # -- slice surface ------------------------------------------------------

    def meta(self) -> ServerMeta:
        data = self.get("/meta")
        if not isinstance(data, dict):
            raise KapTransportError("/meta: unexpected data shape")
        return ServerMeta(
            server_version=str(data.get("server_version") or ""),
            server_id=str(data.get("server_id") or ""),
            backend=str(data.get("backend") or ""),
            started_at=str(data.get("started_at") or ""),
        )

    def list_sessions(self, *, page_size: int = 100, max_pages: int = 20) -> list[SessionSummary]:
        """List sessions, following id-cursor pagination."""
        sessions: list[SessionSummary] = []
        after_id: str | None = None
        for _ in range(max_pages):
            query = {"page_size": page_size}
            if after_id:
                query["after_id"] = after_id
            data = self.get(f"/sessions?{urllib.parse.urlencode(query)}")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise KapTransportError("/sessions: unexpected data shape")
            items = data["items"]
            for raw in items:
                if isinstance(raw, dict):
                    sessions.append(_parse_session_summary(raw))
            if not data.get("has_more") or not items:
                return sessions
            last = items[-1]
            after_id = str(last.get("id")) if isinstance(last, dict) and last.get("id") else None
            if not after_id:
                return sessions
        logger.warning("list_sessions: hit page cap %d; result may be truncated", max_pages)
        return sessions

    def get_prompts(self, session_id: str) -> PromptQueueState:
        """Active + queued prompts; also the canonical resume-backed route used
        to warm a cold session before subscribing (spike S3 nuance)."""
        data = self.get(f"/sessions/{_url_quote(session_id)}/prompts")
        if not isinstance(data, dict):
            raise KapTransportError("prompts: unexpected data shape")
        active = data.get("active")
        queued = data.get("queued") if isinstance(data.get("queued"), list) else []
        active_id = _prompt_id(active) if isinstance(active, dict) else None
        queued_ids = tuple(pid for pid in (_prompt_id(item) for item in queued) if pid)
        return PromptQueueState(active_prompt_id=active_id, queued_prompt_ids=queued_ids)

    def get_snapshot(self, session_id: str) -> SessionSnapshot:
        data = self.get(f"/sessions/{_url_quote(session_id)}/snapshot")
        if not isinstance(data, dict):
            raise KapTransportError("snapshot: unexpected data shape")
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        in_flight_turn = data.get("in_flight_turn")
        return SessionSnapshot(
            as_of_seq=_non_negative_int(data.get("as_of_seq")),
            epoch=str(data.get("epoch") or ""),
            busy=bool(session.get("busy")),
            pending_interaction=_optional_pending_interaction(session.get("pending_interaction")),
            current_prompt_id=_optional_str(session.get("current_prompt_id")),
            in_flight=isinstance(in_flight_turn, dict),
            pending_approval_ids=_collect_ids(data.get("pending_approvals"), "approval_id"),
            pending_question_ids=_collect_ids(data.get("pending_questions"), "question_id"),
            in_flight_turn_id=(
                _optional_non_negative_int(in_flight_turn.get("turn_id"))
                if isinstance(in_flight_turn, dict)
                else None
            ),
            in_flight_assistant_text=(
                str(in_flight_turn.get("assistant_text") or "")
                if isinstance(in_flight_turn, dict)
                else ""
            ),
            pending_approvals=tuple(
                view
                for view in (_parse_approval_request(item) for item in _as_list(data.get("pending_approvals")))
                if view is not None
            ),
            pending_questions=tuple(
                view
                for view in (_parse_question_request(item) for item in _as_list(data.get("pending_questions")))
                if view is not None
            ),
        )

    def shutdown(self) -> None:
        """POST /shutdown — no body and no Content-Type header (spike S4)."""
        data = self.post("/shutdown")
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise KapTransportError(f"shutdown: unexpected reply {data!r}")


# ---------------------------------------------------------------------------
# Managed kap-server child process
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackoffPolicy:
    """Bounded exponential restart backoff.

    The streak resets once a child stayed up for ``stable_after_seconds`` —
    a server that ran fine for an hour and then crashed restarts fast.
    """

    base_seconds: float = 1.0
    cap_seconds: float = 30.0
    stable_after_seconds: float = 60.0
    _consecutive: int = 0

    def next_delay(self, uptime_seconds: float) -> float:
        if uptime_seconds >= self.stable_after_seconds:
            self._consecutive = 0
        delay = min(self.base_seconds * (2**self._consecutive), self.cap_seconds)
        self._consecutive += 1
        return delay


class KapServerProcess:
    """A managed ``kimi web --no-open`` child process.

    Handles spawning with an explicit child environment, readiness wait
    (instance registry + token file + TCP probe), crash observation
    (``poll``/``wait``), and graceful shutdown (SIGTERM, escalating to
    SIGKILL). The restart policy lives in the supervisor (kited) which pairs
    this class with BackoffPolicy.
    """

    def __init__(
        self,
        *,
        kimi_bin: str,
        home: pathlib.Path | str,
        host: str = DEFAULT_KAP_HOST,
        requested_port: int = DEFAULT_KAP_PORT,
        env_overlay: Mapping[str, str] | None = None,
        extra_args: tuple[str, ...] | list[str] = (),
        readiness_timeout_seconds: float = 60.0,
        stdout_path: pathlib.Path | str | None = None,
    ) -> None:
        self._kimi_bin = kimi_bin
        self._home = pathlib.Path(home).expanduser()
        self._host = host
        self._requested_port = int(requested_port)
        self._env_overlay = dict(env_overlay or {})
        self._extra_args = tuple(extra_args)
        self._readiness_timeout = float(readiness_timeout_seconds)
        self._stdout_path = (
            pathlib.Path(stdout_path).expanduser()
            if stdout_path is not None
            else self._home / "server.stdout.log"
        )
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_handle: Any = None
        self.port: int | None = None
        self.token: str | None = None

    @property
    def home(self) -> pathlib.Path:
        return self._home

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def start(self) -> "KapServerProcess":
        """Spawn the child and block until it is ready (or raise)."""
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("kap-server child already running")
        self._home.mkdir(parents=True, exist_ok=True)
        self._stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdout_handle = open(self._stdout_path, "ab")
        args = [
            self._kimi_bin,
            "web",
            "--no-open",
            "--port",
            str(self._requested_port),
            *self._extra_args,
        ]
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=self._stdout_handle,
                stderr=subprocess.STDOUT,
                env=build_child_env(self._home, self._env_overlay),
                start_new_session=True,  # own process group: signals target it alone
            )
        except Exception:
            self._close_stdout()
            raise
        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise
        return self

    def poll(self) -> int | None:
        return self._proc.poll() if self._proc is not None else None

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("kap-server child not started")
        return self._proc.wait(timeout=timeout)

    def stop(self, grace_seconds: float = 10.0) -> int | None:
        """SIGTERM, escalating to SIGKILL after the grace window."""
        proc = self._proc
        if proc is None:
            self._close_stdout()
            return None
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                logger.warning("kap-server pid=%d ignored SIGTERM; sending SIGKILL", proc.pid)
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("kap-server pid=%d did not die after SIGKILL", proc.pid)
        self._close_stdout()
        return proc.returncode

    def _close_stdout(self) -> None:
        if self._stdout_handle is not None:
            try:
                self._stdout_handle.close()
            except OSError:
                pass
            self._stdout_handle = None

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self._readiness_timeout
        token_path = server_token_path(self._home)
        while time.monotonic() < deadline:
            returncode = self.poll()
            if returncode is not None:
                raise RuntimeError(
                    f"kimi web exited early rc={returncode}\n{self._stdout_tail()}"
                )
            port = self._port_from_registry()
            if port is not None and token_path.exists():
                try:
                    with socket.create_connection((self._host, port), timeout=1):
                        pass
                except OSError:
                    time.sleep(0.2)
                    continue
                self.port = port
                self.token = read_server_token(self._home)
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"kimi web not ready within {self._readiness_timeout}s; "
            f"log: {self._stdout_path}"
        )

    def _port_from_registry(self) -> int | None:
        """The actual bound port (handles the server's port+1 conflict retry)."""
        registry = _instance_registry_dir(self._home)
        try:
            names = sorted(registry.iterdir())
        except OSError:
            return None
        for path in names:
            if path.suffix != ".json":
                continue
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                pid = int(info["pid"])
                port = int(info["port"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if self._proc is not None and pid == self._proc.pid:
                return port
        return None

    def _stdout_tail(self, limit: int = 3000) -> str:
        try:
            with open(self._stdout_path, "rb") as handle:
                return handle.read()[-limit:].decode(errors="replace")
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# WS subscription client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KapErrorFrame:
    """Normalized WS ``error`` frame payload.

    Distinct from REST business errors: a prompt whose REST submit succeeded
    can still die immediately (e.g. ``model.not_configured``); the failure
    only ever shows up as one of these frames.
    """

    code: str | None
    message: str
    session_id: str | None
    agent_id: str | None
    retryable: bool


class KapWsClient:
    """Thread-based WS subscription client with cursor resume.

    One background thread owns the connection lifecycle:

      connect → server_hello → client_hello(subscriptions + cursors)
      → dispatch frames in wire order → on stale/drop: reconnect + resubscribe

    Resync discipline (docs/architecture/kite-design.md §5):

    - every (re)subscribe is preceded by a resume-backed REST warmup
      (``GET .../prompts``) so cold sessions after a server restart do not
      yield an unexplained resync;
    - frames arriving while an ack is awaited (replayed events, the standalone
      ``resync_required`` frame) are dispatched in strict wire order — never
      dropped, never reordered;
    - ack ``payload.cursors`` are adopted as a cursor source of truth,
      monotonically (audit M4: same epoch keeps the higher seq, a new epoch
      is always adopted) — the ack wait races the recv loop's own cursor
      advances and must never rewind them;
    - durable event frames advance the stored per-session cursor, including
      durable ``error`` frames (audit M6: advancing BEFORE the error
      callback fires, so a reconnect never replays them);
    - ``resync_required`` (standalone frame or ack-listed) is surfaced via the
      on_resync_required callback; the receiver rebuilds from a REST snapshot
      and stores ``snapshot.cursor``. An ack-listed resync WITHOUT a cursor
      means no server-side subscription was established (cold/deleted
      session), so a successful rebuild must be answered with
      ``resubscribe_after_rebuild`` — one guarded attempt per connection per
      session until a clean ack lands (audit M7);
    - with no server heartbeat, no frame of any kind for ``stale_seconds``
      triggers a reconnect.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        rest_client: KapRestClient,
        cursor_store: EventCursorStore | None = None,
        client_id: str = "kited",
        stale_seconds: float = 45.0,
        reconnect_delay_seconds: float = 2.0,
        ack_timeout_seconds: float = 15.0,
        open_timeout_seconds: float = 15.0,
        on_event: Callable[[KapEvent], None] | None = None,
        on_resync_required: Callable[[ResyncRequest], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        on_error_frame: Callable[[KapErrorFrame], None] | None = None,
        on_volatile: Callable[[AssistantDelta], None] | None = None,
    ) -> None:
        self._url = f"ws://{host}:{port}{API_PREFIX}/ws"
        self._token = token
        self._rest = rest_client
        self._cursor_store = cursor_store
        self._client_id = client_id
        self._stale_seconds = float(stale_seconds)
        self._reconnect_delay = float(reconnect_delay_seconds)
        self._ack_timeout = float(ack_timeout_seconds)
        self._open_timeout = float(open_timeout_seconds)
        self._on_event = on_event
        self._on_resync = on_resync_required
        self._on_connection_change = on_connection_change
        self._on_error_frame = on_error_frame
        self._on_volatile = on_volatile

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._wanted_sessions: set[str] = set()
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._ack_cond = threading.Condition()
        # Acks are only stored while a _send_and_wait waiter is registered
        # for their id (audit R-4): an ack that arrives after its wait timed
        # out (or for a never-awaited id) is dropped instead of leaking into
        # the map forever. Both maps are therefore bounded by the number of
        # in-flight waits; waiters unregister on success AND on timeout.
        self._pending_acks: dict[str, dict[str, Any]] = {}
        self._awaited_acks: set[str] = set()
        self._memory_cursors: dict[str, EventCursor] = {}
        self._connected_at: float | None = None
        # M7 anti-tight-loop state (per connection): sessions an ack flagged
        # for resync WITHOUT a cursor (no server-side subscription was
        # established) and sessions that already used their one post-rebuild
        # re-subscribe attempt. Both clear on disconnect and when a clean
        # ack lands for the session.
        self._ack_resync_sessions: set[str] = set()
        self._resubscribe_attempted: set[str] = set()
        self.server_hello: dict[str, Any] | None = None

    # -- public API ----------------------------------------------------------

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("WS client already started")
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="kite-kap-ws", daemon=True
            )
            self._thread.start()

    def stop(self, *, join_timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._close_ws()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    @property
    def connected(self) -> bool:
        return self._connected_at is not None and self._ws is not None

    @property
    def connected_at(self) -> float | None:
        """Epoch seconds when the current connection was established."""
        return self._connected_at

    def subscribe(self, session_id: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Subscribe to a session now (when connected) and on every reconnect.

        Returns the ack payload, or None when not connected (the subscription
        is recorded and applied on connect).
        """
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        with self._state_lock:
            self._wanted_sessions.add(session_id)
            # Only a fully handshaken connection may be used: the handshake
            # pumps frames inline and must remain the sole recv() caller.
            ws = self._ws if self._connected_at is not None else None
        if ws is None:
            return None
        self._warm_session(session_id)
        payload: dict[str, Any] = {"session_ids": [session_id]}
        cursor = self._cursor_get(session_id)
        if cursor is not None:
            payload["cursors"] = {session_id: {"seq": cursor.seq, "epoch": cursor.epoch}}
        ack = self._send_and_wait(ws, "subscribe", payload, timeout or self._ack_timeout)
        return self._handle_ack_payload(ack)

    def unsubscribe(self, session_id: str, *, timeout: float | None = None) -> None:
        session_id = str(session_id).strip()
        with self._state_lock:
            self._wanted_sessions.discard(session_id)
            ws = self._ws
        if ws is None:
            return
        self._send_and_wait(
            ws, "unsubscribe", {"session_ids": [session_id]}, timeout or self._ack_timeout
        )

    def cursor_for(self, session_id: str) -> EventCursor | None:
        return self._cursor_get(session_id)

    def resubscribe_after_rebuild(self, session_id: str, *, timeout: float | None = None) -> bool:
        """Re-subscribe once after a successful snapshot rebuild (audit M7).

        An ack-listed resync that carried no cursor means no server-side
        subscription was established (cold/deleted session): the snapshot
        rebuild adopts the cursor, but without a fresh subscribe the session
        stays silent until the next reconnect. Guarded against tight loops:
        at most one attempt per connection per session until a clean ack
        lands (the session stops appearing in resync_required); a
        persistently cold session therefore cannot cycle rebuild ->
        subscribe -> resync. Returns True when an attempt was made.
        """
        session_id = str(session_id or "").strip()
        with self._state_lock:
            if (
                not session_id
                or session_id not in self._wanted_sessions
                or session_id not in self._ack_resync_sessions
                or session_id in self._resubscribe_attempted
            ):
                return False
            self._resubscribe_attempted.add(session_id)
        try:
            self.subscribe(session_id, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - the reconnect path covers it
            logger.warning(
                "post-rebuild re-subscribe failed session=%s: %s", session_id, exc
            )
        return True

    # -- connection lifecycle (background thread) ----------------------------

    def _run(self) -> None:
        failures = 0
        auth_failures = 0
        while not self._stop_event.is_set():
            try:
                self._connect_once()
                # A completed handshake resets both counters.
                failures = 0
                auth_failures = 0
                self._recv_loop()
            except _StaleConnection:
                logger.warning("WS stale: no frame for %.1fs; reconnecting", self._stale_seconds)
            except websockets.exceptions.ConnectionClosed as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("WS connection closed (%s); reconnecting", exc)
            except (
                KapError,
                KapWsError,
                KapTransportError,
                OSError,
                # The WS upgrade HTTP rejection (401 → InvalidStatus; neither
                # an OSError nor a Kap error, so without being named here it
                # fell into the generic branch below and the auth-escalation
                # counter never moved — audit R-5).
                websockets.exceptions.InvalidHandshake,
            ) as exc:
                if self._stop_event.is_set():
                    break
                if _is_auth_failure(exc):
                    auth_failures += 1
                    if auth_failures >= 3:
                        # Persistent auth failure used to hot-loop every
                        # reconnect_delay and feed the upstream auth-failure
                        # rate limiter (audit L31); surface it loudly.
                        logger.error(
                            "WS auth failed %d times in a row; the token may be "
                            "rotated or revoked — check kitectl session status",
                            auth_failures,
                        )
                logger.warning("WS connection failed: %s", exc)
            except Exception:  # noqa: BLE001 - keep the supervisor thread alive
                if self._stop_event.is_set():
                    break
                logger.exception("WS loop unexpected error; reconnecting")
            finally:
                self._mark_disconnected()
            failures += 1
            # Exponential backoff with a cap (flat-delay hot loop before).
            delay = min(self._reconnect_delay * (2 ** min(failures - 1, 5)), 60.0)
            if self._stop_event.wait(delay):
                break

    def _connect_once(self) -> None:
        ws = websockets.sync.client.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            open_timeout=self._open_timeout,
        )
        try:
            raw_hello = ws.recv(timeout=self._open_timeout)
            hello = json.loads(raw_hello)
            if not isinstance(hello, dict) or hello.get("type") != "server_hello":
                raise KapWsError(f"expected server_hello, got {hello!r}")
            self.server_hello = hello.get("payload") if isinstance(hello.get("payload"), dict) else {}
            with self._state_lock:
                wanted = sorted(self._wanted_sessions)
            cursors: dict[str, dict[str, Any]] = {}
            for session_id in wanted:
                self._warm_session(session_id)
                cursor = self._cursor_get(session_id)
                if cursor is not None:
                    cursors[session_id] = {"seq": cursor.seq, "epoch": cursor.epoch}
            payload: dict[str, Any] = {"client_id": self._client_id, "subscriptions": wanted}
            if cursors:
                payload["cursors"] = cursors
            ack = self._send_and_wait(ws, "client_hello", payload, self._ack_timeout)
            self._handle_ack_payload(ack)
            with self._state_lock:
                self._ws = ws
                self._connected_at = time.time()
            logger.info(
                "WS connected (%d subscription(s)); server protocol=%s",
                len(wanted),
                (self.server_hello or {}).get("protocol_version"),
            )
            self._notify_connection_change(True)
        except Exception:
            with self._state_lock:
                if self._ws is ws:
                    self._ws = None
                    self._connected_at = None
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
            raise

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._state_lock:
                ws = self._ws
            if ws is None:
                return
            try:
                raw = ws.recv(timeout=self._stale_seconds)
            except TimeoutError as exc:
                # kap has no heartbeat and does NOT answer app-level pings
                # (schema-defined but dropped by the server's default branch —
                # verified live against 0.29.1, audit 2026-07-25): a silent
                # window is the only staleness signal available.
                raise _StaleConnection() from exc
            try:
                frame = json.loads(raw)
            except ValueError:
                logger.warning("WS: dropping non-JSON frame")
                continue
            if not isinstance(frame, dict):
                continue
            self._dispatch_frame(ws, frame)

    def _dispatch_frame(self, ws: Any, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        frame_id = frame.get("id")
        if frame_type == "ack" and isinstance(frame_id, str):
            with self._ack_cond:
                if frame_id in self._awaited_acks:
                    self._pending_acks[frame_id] = frame
                    self._ack_cond.notify_all()
                # else: a late (post-timeout) or never-awaited ack — dropped
                # (audit R-4), never stored.
            return
        if frame_type == "resync_required":
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            request = ResyncRequest(
                session_id=str(payload.get("session_id") or ""),
                reason=_optional_str(payload.get("reason")),
                current_seq=(
                    payload.get("current_seq")
                    if isinstance(payload.get("current_seq"), int)
                    else None
                ),
                epoch=_optional_str(payload.get("epoch")),
            )
            self._fire_resync(request)
            return
        if frame_type in ("server_hello", "pong"):
            return
        if frame_type == "error":
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            error = KapErrorFrame(
                code=_optional_str(payload.get("code")),
                message=str(payload.get("message") or ""),
                session_id=_optional_str(payload.get("sessionId")),
                agent_id=_optional_str(payload.get("agentId")),
                retryable=bool(payload.get("retryable")),
            )
            # Upstream errors are ordinary durable events (seq/epoch/
            # session_id on the frame, verified live against 0.29.1, audit
            # M6): advance the cursor through the normal path BEFORE firing
            # the callback, or a reconnect replays the frame and a healthy
            # prompt can be misjudged failed.
            event = _parse_event_frame(frame)
            if event is not None and not self._is_session_event_wanted(event):
                logger.debug(
                    "dropping error frame for unsubscribed session %s",
                    event.session_id,
                )
                return
            if event is not None and event.seq is not None and not event.volatile and event.session_id:
                self._advance_cursor(event)
            logger.error("WS error frame: %s", payload)
            if self._on_error_frame is not None:
                try:
                    self._on_error_frame(error)
                except Exception:  # noqa: BLE001 - callbacks must not kill the loop
                    logger.exception("on_error_frame callback failed")
            return
        event = _parse_event_frame(frame)
        if event is None:
            return
        if not self._is_session_event_wanted(event):
            # 0.29.1 turned event.session.work_changed into a GLOBAL event
            # fanned out to every connection without a subscription
            # (upstream d751b6796; audit U1): only wanted (subscribed/bound)
            # sessions may advance cursors or feed work-state tracking —
            # unsolicited sessions are ignored.
            logger.debug(
                "dropping event %s for unsubscribed session %s",
                event.type,
                event.session_id,
            )
            return
        if event.seq is not None and not event.volatile and event.session_id:
            self._advance_cursor(event)
        if event.volatile:
            # The volatile side-channel (streaming-cards contract): normalized
            # deltas go only to on_volatile; they never advance the durable
            # cursor above and never reach the durable on_event path. Other
            # volatile types keep flowing to on_event, which drops them.
            volatile_event = normalize_volatile_event(event)
            if volatile_event is not None and self._on_volatile is not None:
                try:
                    self._on_volatile(volatile_event)
                except Exception:  # noqa: BLE001 - a bad callback must not kill the stream
                    logger.exception("on_volatile callback failed for %s", event.type)
                return
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the stream
                logger.exception("on_event callback failed for %s", event.type)

    # -- ack handling ----------------------------------------------------------

    def _send_and_wait(
        self, ws: Any, frame_type: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Send a control frame and wait for its ack.

        Non-ack frames that arrive while waiting are dispatched by the recv
        loop (or inline during the handshake) in strict wire order — this is
        how the pre-ack ``resync_required`` / replay frames are honored.
        """
        request_id = f"{self._client_id}-{uuid.uuid4().hex[:8]}"
        message = json.dumps({"type": frame_type, "id": request_id, "payload": payload})
        with self._ack_cond:
            # Register BEFORE sending so the ack can never arrive to a
            # dispatcher that has no waiter for it (audit R-4).
            self._awaited_acks.add(request_id)
        try:
            with self._send_lock:
                ws.send(message)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    raise KapWsError(f"{frame_type} aborted: client stopping")
                with self._ack_cond:
                    ack = self._pending_acks.pop(request_id, None)
                if ack is not None:
                    if ack.get("code") not in (0, None):
                        raise KapWsError(f"{frame_type} rejected: {ack.get('msg')}")
                    return ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
                # During the handshake the recv loop is not running yet: pump
                # frames inline so pre-ack frames are still dispatched in order.
                if self._connected_at is None:
                    try:
                        raw = ws.recv(timeout=max(0.05, min(0.5, deadline - time.monotonic())))
                    except TimeoutError:
                        continue
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(frame, dict):
                        self._dispatch_frame(ws, frame)
                else:
                    with self._ack_cond:
                        self._ack_cond.wait(timeout=max(0.05, min(0.5, deadline - time.monotonic())))
            # A late ack must not leak into the next request's slot (audit L32).
            with self._ack_cond:
                self._pending_acks.pop(request_id, None)
            raise KapWsError(f"no ack for {frame_type} within {timeout}s")
        finally:
            with self._ack_cond:
                self._awaited_acks.discard(request_id)
                self._pending_acks.pop(request_id, None)

    def _handle_ack_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Adopt ack cursors (a cursor source of truth) and surface resyncs."""
        cursors = payload.get("cursors") if isinstance(payload.get("cursors"), dict) else {}
        raw_not_found = payload.get("not_found")
        not_found_ids = (
            sorted({raw for raw in raw_not_found if isinstance(raw, str) and raw})
            if isinstance(raw_not_found, list)
            else []
        )
        if not_found_ids:
            # Deleted/unknown sessions must leave the resubscribe loop
            # (audit L33): stop wanting them instead of resyncing forever.
            with self._state_lock:
                for session_id in not_found_ids:
                    self._wanted_sessions.discard(session_id)
            logger.info("subscribe not_found; dropping sessions: %s", not_found_ids)
        raw_resync = payload.get("resync_required")
        resync_ids = (
            sorted({raw for raw in raw_resync if isinstance(raw, str) and raw})
            if isinstance(raw_resync, list)
            else []
        )
        with self._state_lock:
            for session_id in resync_ids:
                if _parse_wire_cursor(cursors.get(session_id)) is None:
                    # Resync listed WITHOUT a cursor: no server-side
                    # subscription was established (cold/deleted session) —
                    # the post-rebuild re-subscribe (M7) applies. A resync
                    # carrying a cursor (buffer_overflow/epoch_changed)
                    # keeps its server-side subscription.
                    self._ack_resync_sessions.add(session_id)
            for session_id in cursors:
                if str(session_id) not in resync_ids:
                    # A clean ack for the session: its server-side
                    # subscription is live, so a later subscription-less
                    # resync earns one fresh re-subscribe attempt.
                    self._ack_resync_sessions.discard(str(session_id))
                    self._resubscribe_attempted.discard(str(session_id))
        for session_id, raw_cursor in cursors.items():
            cursor = _parse_wire_cursor(raw_cursor)
            if cursor is not None:
                self._adopt_ack_cursor(str(session_id), cursor)
        for session_id in resync_ids:
            server_cursor = _parse_wire_cursor(cursors.get(session_id))
            self._fire_resync(
                ResyncRequest(
                    session_id=session_id,
                    reason=None,
                    current_seq=server_cursor.seq if server_cursor else None,
                    epoch=server_cursor.epoch if server_cursor else None,
                )
            )
        return payload

    def _adopt_ack_cursor(self, session_id: str, cursor: EventCursor) -> None:
        """Monotonic ack-cursor adoption (audit M4): subscribe waits for its
        ack on the caller thread while the recv loop keeps advancing the
        cursor with replayed/live events, so an older ack seq must never
        rewind the stored cursor (same guard as the pipeline's
        snapshot-adopt path); a new epoch is always adopted."""
        current = self._cursor_get(session_id)
        if (
            current is not None
            and current.epoch == cursor.epoch
            and current.seq >= cursor.seq
        ):
            return
        self._cursor_set(session_id, cursor)

    # -- helpers ----------------------------------------------------------------

    def _warm_session(self, session_id: str) -> None:
        """Resume-backed REST touch so a cold session activates before subscribe."""
        try:
            self._rest.get_prompts(session_id)
        except (KapError, KapTransportError) as exc:
            if _is_auth_failure(exc):
                # A warmup 401 must NOT be swallowed (audit R-5): the token
                # is dead, the subscribe that follows would fail anyway, and
                # only a raised connect failure feeds the run loop's
                # auth-escalation counter. Every caller of subscribe() wraps
                # exceptions, so this never crashes a caller.
                raise
            # Not fatal: the subscribe ack reports not_found / resync itself.
            logger.warning("warmup GET prompts failed for %s: %s", session_id, exc)

    def _advance_cursor(self, event: KapEvent) -> None:
        current = self._cursor_get(event.session_id or "")
        # Epoch first (audit L11): an epoch rotation invalidates the cursor
        # no matter where the new seq lands — checking seq first would let a
        # lower new seq skip the defensive resync until the new epoch's seq
        # climbs past the old high-water mark.
        if event.epoch and current is not None and event.epoch != current.epoch:
            # A mid-stream epoch change means our cursor is invalid; the
            # server should also resync us, but do not wait for it.
            logger.warning(
                "epoch changed mid-stream for %s (%s -> %s); requesting resync",
                event.session_id,
                current.epoch,
                event.epoch,
            )
            self._fire_resync(
                ResyncRequest(
                    session_id=event.session_id or "",
                    reason="epoch_changed",
                    current_seq=event.seq,
                    epoch=event.epoch,
                )
            )
            return
        if current is not None and event.seq is not None and event.seq <= current.seq:
            return
        if event.seq is not None:
            self._cursor_set(
                event.session_id or "",
                EventCursor(seq=event.seq, epoch=event.epoch or (current.epoch if current else "")),
            )

    def _fire_resync(self, request: ResyncRequest) -> None:
        if not request.session_id:
            return
        logger.info(
            "resync_required session=%s reason=%s", request.session_id, request.reason
        )
        if self._on_resync is not None:
            try:
                self._on_resync(request)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the stream
                logger.exception("on_resync_required callback failed")

    def _is_session_event_wanted(self, event: KapEvent) -> bool:
        """Wanted-set filter (audit U1): events without a session id cannot
        be attributed and pass; a session-scoped event passes iff its
        session is in the wanted (subscribed/bound) set."""
        if not event.session_id:
            return True
        with self._state_lock:
            return event.session_id in self._wanted_sessions

    def _cursor_get(self, session_id: str) -> EventCursor | None:
        if not session_id:
            return None
        if self._cursor_store is not None:
            return self._cursor_store.get(session_id)
        return self._memory_cursors.get(session_id)

    def _cursor_set(self, session_id: str, cursor: EventCursor) -> None:
        if not session_id or not cursor.epoch:
            return
        if self._cursor_store is not None:
            self._cursor_store.set(session_id, cursor)
        else:
            self._memory_cursors[session_id] = cursor

    def _mark_disconnected(self) -> None:
        was_connected = self._connected_at is not None
        self._connected_at = None
        with self._state_lock:
            # Per-connection M7 state: the next connection gets one fresh
            # re-subscribe attempt per subscription-less session.
            self._ack_resync_sessions.clear()
            self._resubscribe_attempted.clear()
        self._close_ws()
        if was_connected:
            self._notify_connection_change(False)

    def _close_ws(self) -> None:
        with self._state_lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass

    def _notify_connection_change(self, connected: bool) -> None:
        if self._on_connection_change is not None:
            try:
                self._on_connection_change(connected)
            except Exception:  # noqa: BLE001 - observability must not kill the loop
                logger.exception("on_connection_change callback failed")


class _StaleConnection(Exception):
    """No frame of any kind arrived within the stale window."""


def _is_auth_failure(exc: BaseException) -> bool:
    """Whether a connect failure looks like an auth rejection (401 / 4401)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 401:
        return True
    # KapError carries the HTTP status as ``http_status``; the websockets
    # upgrade rejection (InvalidStatus) carries it on ``response.status_code``.
    if getattr(exc, "http_status", None) == 401:
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 401:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "rcvd_code", None)
    if code in (4401, 401):
        return True
    # Word-bounded: a kap business code like 40401 contains "401" but is not
    # an auth rejection.
    return re.search(r"\b401\b", str(exc)) is not None


# ---------------------------------------------------------------------------
# Wire-shape parsing helpers (private; schema knowledge stays in this module)
# ---------------------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_pending_interaction(value: Any) -> str | None:
    """Upstream enum is 'none' | 'approval' | 'question'; 'none' means no
    pending interaction and must not reach consumers as a truthy string."""
    parsed = _optional_str(value)
    return None if parsed in (None, "none") else parsed


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KapTransportError(f"expected a non-negative integer, got {value!r}")
    return value


def _url_quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _prompt_id(item: Any) -> str | None:
    if isinstance(item, dict):
        return _optional_str(item.get("prompt_id"))
    return None


def _collect_ids(items: Any, key: str) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = _optional_str(item.get(key))
            if value:
                out.append(value)
    return tuple(out)


def _parse_session_summary(raw: dict[str, Any]) -> SessionSummary:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return SessionSummary(
        session_id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        cwd=_optional_str(metadata.get("cwd")),
        busy=bool(raw.get("busy")),
        pending_interaction=_optional_pending_interaction(raw.get("pending_interaction")),
        archived=bool(raw.get("archived")),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _parse_wire_cursor(raw: Any) -> EventCursor | None:
    if not isinstance(raw, dict):
        return None
    seq = raw.get("seq")
    epoch = raw.get("epoch")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    if not isinstance(epoch, str) or not epoch:
        return None
    return EventCursor(seq=seq, epoch=epoch)


def _tool_display_detail(display: Any) -> str:
    """Salient plain-text field(s) from a ToolInputDisplay payload.

    Returns a short per-kind extraction — never the kind itself — e.g. the
    command string (command), "<operation> <path>" (file_io), the path
    (diff), the query (search), the URL (url_fetch), the agent name
    (agent_call), "<name> <args>" (skill_call), the description (task),
    comma-joined item titles (todo_list), the task description (task_stop),
    the plan (plan_review), the objective (goal_start) or the summary
    (generic); "" for a non-dict payload or an unknown kind. A neutral
    extraction, not presentation (protocol/display.ts
    ToolInputDisplaySchema).
    """
    if not isinstance(display, dict):
        return ""
    kind = str(display.get("kind") or "")
    if kind == "command":
        return str(display.get("command") or "")
    if kind == "file_io":
        operation = str(display.get("operation") or "")
        path = str(display.get("path") or "")
        return f"{operation} {path}".strip()
    if kind == "diff":
        return str(display.get("path") or "")
    if kind == "search":
        return str(display.get("query") or "")
    if kind == "url_fetch":
        return str(display.get("url") or "")
    if kind == "agent_call":
        return str(display.get("agent_name") or "")
    if kind == "skill_call":
        name = str(display.get("skill_name") or "")
        args = str(display.get("args") or "")
        return f"{name} {args}".strip()
    if kind == "task":
        return str(display.get("description") or "")
    if kind == "todo_list":
        items = display.get("items")
        if isinstance(items, list):
            return ", ".join(
                str(item.get("title") or "")
                for item in items
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            )
        return ""
    if kind == "task_stop":
        return str(display.get("task_description") or "")
    if kind == "plan_review":
        return str(display.get("plan") or "")
    if kind == "goal_start":
        return str(display.get("objective") or "")
    if kind == "generic":
        return str(display.get("summary") or "")
    return ""


def _parse_approval_request(raw: Any) -> ApprovalRequestView | None:
    """Parse one wire approvalRequestSchema item (requested event or snapshot)."""
    if not isinstance(raw, dict):
        return None
    approval_id = _optional_str(raw.get("approval_id"))
    if not approval_id:
        return None
    return ApprovalRequestView(
        approval_id=approval_id,
        turn_id=_optional_non_negative_int(raw.get("turn_id")),
        tool_call_id=str(raw.get("tool_call_id") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        action=str(raw.get("action") or ""),
        detail=_tool_display_detail(raw.get("tool_input_display")),
    )


def _parse_question_request(raw: Any) -> QuestionRequestView | None:
    """Parse one wire questionRequestSchema item (requested event or snapshot)."""
    if not isinstance(raw, dict):
        return None
    question_id = _optional_str(raw.get("question_id"))
    if not question_id:
        return None
    items: list[QuestionItemView] = []
    for raw_item in _as_list(raw.get("questions")):
        if not isinstance(raw_item, dict):
            continue
        options = tuple(
            QuestionOptionView(
                option_id=str(option.get("id") or ""),
                label=str(option.get("label") or ""),
                description=str(option.get("description") or ""),
            )
            for option in _as_list(raw_item.get("options"))
            if isinstance(option, dict) and _optional_str(option.get("id"))
        )
        items.append(
            QuestionItemView(
                item_id=str(raw_item.get("id") or ""),
                question=str(raw_item.get("question") or ""),
                header=str(raw_item.get("header") or ""),
                options=options,
                multi_select=bool(raw_item.get("multi_select")),
                allow_other=bool(raw_item.get("allow_other", True)),
            )
        )
    return QuestionRequestView(
        question_id=question_id,
        turn_id=_optional_non_negative_int(raw.get("turn_id")),
        items=tuple(items),
    )


def _parse_event_frame(frame: dict[str, Any]) -> KapEvent | None:
    frame_type = frame.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        return None
    seq = frame.get("seq")
    offset = frame.get("offset")
    return KapEvent(
        type=frame_type,
        session_id=_optional_str(frame.get("session_id")),
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        epoch=_optional_str(frame.get("epoch")),
        volatile=bool(frame.get("volatile")),
        offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
        timestamp=_optional_str(frame.get("timestamp")),
        payload=frame.get("payload"),
    )
