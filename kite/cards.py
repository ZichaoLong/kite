"""Feishu card model for KITE.

Pure builders: every function returns a Feishu card JSON dict (or plain text
for the MVP question pass-through); nothing here imports the transport layer
or knows kap wire schemas — the application layer extracts fields from
normalized adapter types (``KapEvent``/``SessionSnapshot``) and passes plain
values in.

Ported from FOCUS ``bot/cards.py`` but rewritten to kap event semantics
(docs/architecture/kite-design.md §5-6, docs/contracts/mvp-scope.md §3-4):

- Single-anchor execution card: at most one current execution card per chat,
  anchored by ``{chat_id, session_id, prompt_id, card_message_id}``
  (``ExecutionCardAnchor``). Prompt-scoped durable events (turn.started /
  tool.call.* / turn.ended / prompt.aborted / prompt.steered) may modify the
  card only when their prompt_id equals the anchor's; kap's prompt FIFO means
  queued prompts never create a card — only started ones do.
- Terminal result card: when a prompt finishes (completed/aborted/failed — the
  MVP observes completion via turn.ended / prompt.aborted; prompt.completed
  has no producer upstream) a separate terminal card is sent and the execution
  card is frozen. ``terminal_result_checksum`` / ``terminal_result_element_id``
  produce the id/checksum pair ``terminal_result_store`` persists for
  ``/last``-style reads.
- Approval card: approval.requested → three buttons (approve / reject /
  reject-with-feedback) carrying ``approval_id`` + ``prompt_id`` in the button
  value; after the REST response the card is patched to a frozen resolved
  card. A repeated click inside upstream's 60s idempotency window (40902)
  gets the ``APPROVAL_ALREADY_PROCESSED_NOTICE`` toast, not an error.
  Unrebuildable-after-restart / timed-out approvals are explicitly closed out
  with the expired card (fail-closed, mvp-scope §4.6).
- Question (MVP): text pass-through listing numbered options; reply with the
  option number; auto-dismiss on timeout. The rich form card is Phase 2.

Deliberately not ported from FOCUS (see module test file for the locked
surface): the card-history text projection (KITE's transport extracts no
text from interactive messages; terminal text lives in the local store),
goal/plan/model/group/thread-list/settings cards, help navigation
decoration, and the execution card's cancel button (MVP abort is the
permission-gated ``/abort`` command).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Sequence

from kite.card_limits import MAX_CARD_TABLES, count_card_tables, limit_card_tables
from kite.feishu_card_markdown import (
    sanitize_runtime_markdown_for_feishu_card,
    sanitize_terminal_result_markdown_for_feishu_json2,
)

# ---------------------------------------------------------------------------
# Card model constants
# ---------------------------------------------------------------------------

# Execution card states (design §6 + mvp-scope §4.2). "frozen" cards are never
# patched again except by an explicit terminal/unknown transition.
EXECUTION_STATE_RUNNING = "running"
EXECUTION_STATE_FROZEN_DONE = "frozen-done"
EXECUTION_STATE_FROZEN_UNKNOWN = "frozen-unknown"
ExecutionState = Literal["running", "frozen-done", "frozen-unknown"]

# Terminal outcomes (design §6). Completion is observed via turn.ended /
# prompt.aborted; a kap business error on the prompt REST call transitions the
# prompt straight to "failed" with the upstream msg (mvp-scope §4.5).
TERMINAL_COMPLETED = "completed"
TERMINAL_ABORTED = "aborted"
TERMINAL_FAILED = "failed"
TerminalOutcome = Literal["completed", "aborted", "failed"]

# Approval button value contract (consumed by the application layer's
# on_card_action). ``approval_resolve`` is the one-shot path whose decision
# maps 1:1 to kap's ApprovalResponse.decision; ``approval_reject_with_feedback``
# starts a two-step flow (the app collects feedback text in chat, then
# resolves rejected+feedback).
ACTION_APPROVAL_RESOLVE = "approval_resolve"
ACTION_APPROVAL_REJECT_WITH_FEEDBACK = "approval_reject_with_feedback"

# kap approval decisions (packages/protocol/src/approval.ts).
APPROVAL_DECISION_APPROVED = "approved"
APPROVAL_DECISION_REJECTED = "rejected"
APPROVAL_DECISION_CANCELLED = "cancelled"

# Toast for a repeated click inside upstream's 60s idempotency window (REST
# 40902 approval.already_resolved): a notice, not an error (design §6).
APPROVAL_ALREADY_PROCESSED_NOTICE = "该审批已处理，请勿重复操作。"

# Default interaction timeouts (mvp-scope §3: approval timeout default 5
# minutes, configurable; question auto-dismiss shares the MVP default).
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300
DEFAULT_QUESTION_TIMEOUT_SECONDS = 300

# element_id prefix stamped on terminal cards; pairs with the
# terminal_result_id/checksum persisted by terminal_result_store.
TERMINAL_RESULT_ELEMENT_ID_PREFIX = "kite_tr_"

_PROMPT_SNIPPET_MAX = 200

_EXECUTION_CARD_TITLE = "Kimi 执行过程"
_TERMINAL_CARD_TITLE = "Kimi 执行结果"
_APPROVAL_CARD_TITLE = "Kimi 审批请求"

_APPROVAL_DECISION_LABELS = {
    APPROVAL_DECISION_APPROVED: "已批准",
    APPROVAL_DECISION_REJECTED: "已拒绝",
    APPROVAL_DECISION_CANCELLED: "已取消",
}


# ---------------------------------------------------------------------------
# Anchor (design §6 single-anchor rule)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionCardAnchor:
    """Anchor of the one current execution card in a chat.

    The application layer keeps at most one anchor per chat; the anchor is
    created when a prompt *starts* (turn.started), never for a queued prompt,
    and is cleared when the terminal card for that prompt is sent.
    """

    chat_id: str
    session_id: str
    prompt_id: str
    card_message_id: str

    def matches_prompt(self, prompt_id: str | None) -> bool:
        """A prompt-scoped event may modify the anchored card iff its
        prompt_id equals the anchor's.

        Empty/missing prompt_id never matches (fail-closed): events that
        cannot be attributed to the anchored prompt must not touch the card.
        """
        candidate = str(prompt_id or "").strip()
        return bool(candidate) and candidate == self.prompt_id


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _card_config() -> dict:
    return {"wide_screen_mode": True, "update_multi": True}


def _card_config_v2() -> dict:
    return {"update_multi": True}


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _header(title: str, template: str) -> dict:
    return {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    }


def _markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def _session_line(session_title: str, session_id: str) -> str:
    title = str(session_title or "").strip() or "（无标题）"
    session_id = str(session_id or "").strip()
    if session_id:
        return f"会话：{title}（`{_shorten(session_id, 12)}`）"
    return f"会话：{title}"


def terminal_result_checksum(final_reply_text: str) -> str:
    """sha256 of the terminal text; stored beside the record for /last reads."""
    raw_text = str(final_reply_text or "")
    if not raw_text:
        return ""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def terminal_result_element_id(terminal_result_id: str, checksum: str) -> str:
    normalized_id = str(terminal_result_id or "").strip().lower()
    normalized_checksum = str(checksum or "").strip().lower()
    if not normalized_id or not normalized_checksum:
        return ""
    return f"{TERMINAL_RESULT_ELEMENT_ID_PREFIX}{normalized_id}_{normalized_checksum[:16]}"


# ---------------------------------------------------------------------------
# Execution card (design §6: single-anchor, one current card per chat)
# ---------------------------------------------------------------------------


def build_execution_card(
    *,
    session_title: str,
    session_id: str,
    prompt_text: str,
    state: ExecutionState = EXECUTION_STATE_RUNNING,
    elapsed_seconds: int = 0,
    queue_length: int = 0,
    tool_lines: Sequence[str] = (),
) -> dict:
    """Build the execution card for one started prompt.

    - running: live card, patched as durable events arrive; header carries the
      elapsed seconds and the body shows the FIFO queue length behind the
      active prompt (mvp-scope §3: no interrupt surface, the queue is only
      *shown*).
    - frozen-done: the prompt finished; the card stays as the frozen process
      record next to the separate terminal card.
    - frozen-unknown: the WS stream broke and the snapshot rebuild failed —
      the card shows "state unknown" plus a `kitectl session status` hint and
      is never patched again (mvp-scope §4.2: never guess the state).

    ``tool_lines`` are pre-rendered one-line summaries of tool.call.* /
    tool.result events, owned by the application layer; the builder only
    lays them out.
    """
    elapsed_seconds = max(int(elapsed_seconds), 0)
    queue_length = max(int(queue_length), 0)

    if state == EXECUTION_STATE_RUNNING:
        template = "turquoise"
        if elapsed_seconds > 0:
            title = f"{_EXECUTION_CARD_TITLE}（执行中 {elapsed_seconds}s）"
        else:
            title = f"{_EXECUTION_CARD_TITLE}（执行中）"
    elif state == EXECUTION_STATE_FROZEN_UNKNOWN:
        template = "orange"
        title = f"{_EXECUTION_CARD_TITLE}（状态未知）"
    else:
        template = "blue"
        title = f"{_EXECUTION_CARD_TITLE}（已结束）"

    lines = [_session_line(session_title, session_id)]
    prompt_snippet = _shorten(str(prompt_text or "").strip() or "（空）", _PROMPT_SNIPPET_MAX)
    if "\n" in prompt_snippet:
        # A multi-line prompt gets its own line so the list-continuation
        # hardener sees the list markers at line start.
        lines.append(f"指令：\n{prompt_snippet}")
    else:
        lines.append(f"指令：{prompt_snippet}")
    if queue_length > 0:
        lines.append(f"队列：还有 {queue_length} 条 prompt 排队中")
    if state == EXECUTION_STATE_FROZEN_UNKNOWN:
        lines.append(
            "⚠️ 事件流中断且快照重建失败，该 prompt 的实际状态未知。"
            "请使用 `kitectl session status` 排查。"
        )

    elements: list[dict] = [
        _markdown(sanitize_runtime_markdown_for_feishu_card("\n".join(lines)))
    ]

    rendered_tool_lines = [str(line).strip() for line in tool_lines if str(line).strip()]
    if rendered_tool_lines:
        tool_text = limit_card_tables("\n".join(rendered_tool_lines), MAX_CARD_TABLES)
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": True,
                "header": {
                    "title": {"tag": "plain_text", "content": f"工具调用（{len(rendered_tool_lines)}）"},
                    "icon": {
                        "tag": "standard_icon",
                        "token": "right-small-ccm_outlined",
                        "size": "16px 16px",
                    },
                    "icon_position": "left",
                    "icon_expanded_angle": 90,
                },
                "elements": [
                    _markdown(sanitize_runtime_markdown_for_feishu_card(tool_text))
                ],
            }
        )

    return {
        "schema": "2.0",
        "config": _card_config(),
        "header": _header(title, template),
        "body": {"elements": elements},
    }


# ---------------------------------------------------------------------------
# Terminal result card (design §6: separate card on prompt finish)
# ---------------------------------------------------------------------------


def build_terminal_card(
    *,
    outcome: TerminalOutcome,
    text: str,
    terminal_result_id: str = "",
    checksum: str = "",
) -> dict:
    """Build the terminal result card for a finished prompt.

    ``text`` is the terminal text (final reply for completed, the abort note
    for aborted, the upstream error msg for failed); it is what
    terminal_result_store persists for `/last`-style reads. When
    ``terminal_result_id`` is given, the text element is stamped with an
    element_id derived from it + the checksum, so card and store record stay
    cross-referable.
    """
    raw_text = str(text or "")
    if outcome == TERMINAL_ABORTED:
        template = "grey"
        title = f"{_TERMINAL_CARD_TITLE}（已中止）"
        fallback = "*已中止，无最终输出。*"
    elif outcome == TERMINAL_FAILED:
        template = "red"
        title = f"{_TERMINAL_CARD_TITLE}（失败）"
        fallback = "*执行失败，未收到错误详情。*"
    else:
        template = "green"
        title = _TERMINAL_CARD_TITLE
        fallback = "*无最终输出。*"

    content = sanitize_terminal_result_markdown_for_feishu_json2(raw_text) or fallback
    element: dict[str, str] = {"tag": "markdown", "content": content}
    element_id = terminal_result_element_id(
        terminal_result_id, checksum or terminal_result_checksum(raw_text)
    )
    if element_id:
        element["element_id"] = element_id

    return {
        "schema": "2.0",
        "config": _card_config_v2(),
        "header": _header(title, template),
        "body": {"elements": [element]},
    }


# ---------------------------------------------------------------------------
# Approval cards (design §6 + mvp-scope §3/§4.4-4.6)
# ---------------------------------------------------------------------------


def build_approval_card(
    *,
    approval_id: str,
    prompt_id: str,
    tool_name: str = "",
    action: str = "",
    detail: str = "",
    timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
) -> dict:
    """Build the three-button approval card for an approval.requested event.

    Every button carries ``approval_id`` + ``prompt_id`` in its value so the
    application layer can route the resolution to the right session and
    attribute it to the initiating prompt (approval cards go only to the
    prompt initiator's chat, mvp-scope §3).
    """
    approval_id = str(approval_id or "").strip()
    prompt_id = str(prompt_id or "").strip()

    lines: list[str] = []
    if str(tool_name or "").strip():
        lines.append(f"**工具**：`{str(tool_name).strip()}`")
    if str(action or "").strip():
        lines.append(f"**操作**：{str(action).strip()}")
    if str(detail or "").strip():
        lines.append(str(detail).strip())
    if not lines:
        lines.append("*上游未提供审批详情。*")
    timeout_seconds = max(int(timeout_seconds), 0)
    if timeout_seconds > 0:
        lines.append(
            f"超过 {timeout_seconds // 60} 分钟未处理将自动拒绝（不会自动批准）。"
        )

    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "批准"},
            "type": "primary",
            "value": {
                "action": ACTION_APPROVAL_RESOLVE,
                "decision": APPROVAL_DECISION_APPROVED,
                "approval_id": approval_id,
                "prompt_id": prompt_id,
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "拒绝"},
            "type": "danger",
            "value": {
                "action": ACTION_APPROVAL_RESOLVE,
                "decision": APPROVAL_DECISION_REJECTED,
                "approval_id": approval_id,
                "prompt_id": prompt_id,
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "拒绝并反馈"},
            "type": "default",
            "value": {
                "action": ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
                "approval_id": approval_id,
                "prompt_id": prompt_id,
            },
        },
    ]

    return {
        "config": _card_config(),
        "header": _header(_APPROVAL_CARD_TITLE, "orange"),
        "elements": [
            _markdown(sanitize_runtime_markdown_for_feishu_card("\n".join(lines))),
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
        ],
    }


def build_approval_resolved_card(
    *,
    decision: str,
    feedback: str = "",
) -> dict:
    """Build the frozen post-resolution approval card (patched in place after
    the REST response; buttons are gone, so no further clicks can race the
    60s idempotency window)."""
    label = _APPROVAL_DECISION_LABELS.get(str(decision or "").strip(), "已处理")
    content = f"{label}。"
    if str(feedback or "").strip():
        content += f"\n反馈：{str(feedback).strip()}"
    return {
        "config": _card_config(),
        "header": _header(f"{_APPROVAL_CARD_TITLE}（已处理）", "grey"),
        "elements": [_markdown(content)],
    }


def build_approval_expired_card(*, reason: str = "") -> dict:
    """Build the expired approval card (mvp-scope §4.6): used when a pending
    approval cannot be rebuilt after a kited restart, or when it timed out —
    closed out explicitly instead of left clickable."""
    content = "该审批已过期"
    if str(reason or "").strip():
        content += f"（{str(reason).strip()}）"
    content += "。\n请重新发起操作，或在本地直接处理。"
    return {
        "config": _card_config(),
        "header": _header(f"{_APPROVAL_CARD_TITLE}（已过期）", "grey"),
        "elements": [_markdown(content)],
    }


# ---------------------------------------------------------------------------
# Question (MVP: text pass-through; the rich form card is Phase 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuestionOptionSpec:
    """One selectable option of a question (label + optional description)."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class QuestionItemSpec:
    """One question to render in the MVP text pass-through."""

    question: str
    header: str = ""
    options: tuple[QuestionOptionSpec, ...] = ()
    multi_select: bool = False
    allow_other: bool = False


def build_question_text(
    items: Sequence[QuestionItemSpec],
    *,
    timeout_seconds: int = DEFAULT_QUESTION_TIMEOUT_SECONDS,
) -> str:
    """Build the MVP question pass-through text.

    Lists each question with numbered options and the reply convention; the
    application layer maps the user's numbered reply to the kap answers
    payload. Unanswered questions are auto-dismissed on timeout (design §6).
    """
    blocks: list[str] = []
    item_list = list(items)
    multi = len(item_list) > 1
    for index, item in enumerate(item_list, start=1):
        header = str(item.header or "").strip() or f"问题 {index}"
        lines = [f"**{header}**", str(item.question or "").strip() or "（空问题）"]
        rendered_options = [
            option for option in item.options if str(option.label or "").strip()
        ]
        if rendered_options:
            for option_index, option in enumerate(rendered_options, start=1):
                line = f"{option_index}. {option.label.strip()}"
                if str(option.description or "").strip():
                    line += f" — {option.description.strip()}"
                lines.append(line)
            if item.multi_select:
                lines.append("（可多选，回复如 `1,3`）")
        if item.allow_other:
            lines.append("（回复「其他：你的内容」可自定义回答）")
        blocks.append("\n".join(lines))

    if multi:
        instruction = "回复格式：`问题号:选项号`，每行一个（如 `1:2`）。"
    else:
        instruction = "回复选项编号即可（如 `1`）。"
    timeout_seconds = max(int(timeout_seconds), 0)
    if timeout_seconds > 0:
        instruction += f"{timeout_seconds // 60} 分钟内未回复将自动关闭（dismiss）。"
    blocks.append(instruction)
    return "\n\n".join(blocks)
