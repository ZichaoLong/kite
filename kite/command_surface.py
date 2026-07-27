"""Feishu slash-command surface: parsing + help text.

Pure functions and data only — no transport, kap, or store imports. The
dispatch table that binds these names to behavior lives in
kite/app_handler.py; this module is the shared, fully-tested description of
the MVP command surface itself (docs/contracts/mvp-scope.md §2).

User-visible strings are Chinese (FOCUS tone). Feishu renders ASCII
``<arg>`` placeholders as tags even inside code spans, so usage strings use
the full-width ``〈arg〉`` form instead (same convention as FOCUS
feishu_command_syntax).
"""

from __future__ import annotations

from dataclasses import dataclass

from kite.stores.binding_store import VALID_EFFORTS, VALID_PERMISSION_MODES
from kite.stores.group_config_store import VALID_GROUP_MODES

_PLAN_ON = "on"
_PLAN_OFF = "off"

# /goal keywords that are NOT objective text (mvp-scope §2 /goal row):
# pause/resume/cancel are the kap one-shot ``goal_control`` values; ``off``
# clears the persisted objective (never sent upstream).
GOAL_CONTROL_PAUSE = "pause"
GOAL_CONTROL_RESUME = "resume"
GOAL_CONTROL_CANCEL = "cancel"
VALID_GOAL_CONTROLS = frozenset(
    {GOAL_CONTROL_PAUSE, GOAL_CONTROL_RESUME, GOAL_CONTROL_CANCEL}
)
_GOAL_OFF = "off"
_GOAL_KEYWORDS = VALID_GOAL_CONTROLS | {_GOAL_OFF}


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """One parsed slash command (``name`` keeps the leading slash)."""

    name: str
    arg: str
    raw: str


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """User-visible description of one command (help text + usage errors)."""

    name: str
    usage: str
    summary: str


# The MVP command table (docs/contracts/mvp-scope.md §2), in help order.
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "/new",
        "/new",
        "新建会话并绑定；旧会话保留在 kap-server 上。",
    ),
    CommandSpec(
        "/sessions",
        "/sessions",
        "列出 kap-server 上的会话，可点按钮切换绑定。",
    ),
    CommandSpec(
        "/switch",
        "/switch 〈id〉",
        "切换绑定到指定会话（自动恢复推送）。",
    ),
    CommandSpec(
        "/detach",
        "/detach",
        "暂停当前绑定的飞书推送；绑定本身保留。",
    ),
    CommandSpec(
        "/attach",
        "/attach",
        "恢复当前绑定的飞书推送。",
    ),
    CommandSpec(
        "/mode",
        "/mode 〈auto|manual|yolo〉",
        "查看或设置权限模式；yolo 需管理员，开启后操作自动批准。",
    ),
    CommandSpec(
        "/plan",
        "/plan [on|off]",
        "切换计划模式（plan mode）；不带参数时取反。",
    ),
    CommandSpec(
        "/effort",
        "/effort 〈off|low|medium|high|xhigh|max〉",
        "查看或设置思考强度（thinking effort）；随每条 prompt 显式携带。",
    ),
    CommandSpec(
        "/goal",
        "/goal [text|pause|resume|cancel|off]",
        "查看、设置或控制目标（goal）；目标保存在上游会话侧，pause/resume/cancel/off 为控制操作。",
    ),
    CommandSpec(
        "/compact",
        "/compact",
        "压缩当前会话的上下文（kap compact 透传）。",
    ),
    CommandSpec(
        "/rename",
        "/rename 〈title〉",
        "重命名当前会话的标题。",
    ),
    CommandSpec(
        "/archive",
        "/archive",
        "归档当前会话；绑定保留，归档后发送消息会提示切换会话。",
    ),
    CommandSpec(
        "/restore",
        "/restore",
        "恢复已归档的当前会话。",
    ),
    CommandSpec(
        "/group",
        "/group 〈activate|deactivate〉",
        "激活/停用当前群聊（仅管理员，在群聊中使用）；激活后成员 @机器人 发送文字即可提交 prompt。",
    ),
    CommandSpec(
        "/group-mode",
        "/group-mode 〈mention_only|assistant|all〉",
        "查看或切换群聊模式（仅管理员，在已激活的群聊中使用）；assistant 模式记录群成员消息并在 @机器人 时携带群聊上下文，all 模式下每条成员消息直接触发 prompt（本群独占会话）。",
    ),
    CommandSpec(
        "/status",
        "/status",
        "查看绑定、会话工作状态与队列。",
    ),
    CommandSpec(
        "/last",
        "/last",
        "重发当前会话最近一次终态答复文本。",
    ),
    CommandSpec(
        "/abort",
        "/abort",
        "中止当前执行中的 prompt；仅发起者或管理员可用。",
    ),
    CommandSpec(
        "/btw",
        "/btw 〈text〉",
        "把 text 发给旁路 agent（不排队、不打断当前执行）。",
    ),
    CommandSpec(
        "/init",
        "/init 〈token〉",
        "注册管理员；token 由 kited 首次启动时生成（见 `kitectl config init-token`）。",
    ),
    CommandSpec(
        "/help",
        "/help",
        "显示本命令导航。",
    ),
    CommandSpec(
        "/whoami",
        "/whoami",
        "查看你的身份与当前 chat/绑定状态（非管理员也可用）。",
    ),
)

_SPECS_BY_NAME = {spec.name: spec for spec in COMMAND_SPECS}


def parse_slash_command(text: str) -> SlashCommand | None:
    """Parse ``/name arg...``; None for plain (non-slash) text.

    The name is lowercased and a trailing ``@BotName`` mention suffix (group
    convention) is stripped; the argument keeps its original casing with
    surrounding whitespace trimmed. A bare ``/`` (or ``/ xxx``, where the
    head is just the slash) still parses — with name ``/`` — so it is
    answered as an unknown command instead of leaking into the prompt path
    (FOCUS parity, audit L13).
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    name = head.lower()
    if "@" in name:
        name = name.split("@", 1)[0]
    if len(name) <= 1:
        name = "/"
    return SlashCommand(name=name, arg=tail.strip(), raw=stripped)


def get_command_spec(name: str) -> CommandSpec | None:
    """The spec for a command name (with leading slash), or None."""
    return _SPECS_BY_NAME.get(str(name or "").strip().lower())


def build_usage_text(name: str) -> str:
    """The ``用法：...`` reply for argument errors (FOCUS tone)."""
    spec = get_command_spec(name)
    if spec is None:
        return "发送 /help 查看命令导航。"
    return f"用法：`{spec.usage}`\n说明：{spec.summary}"


def build_help_text() -> str:
    """The /help command navigation (mvp-scope §2 command table)."""
    lines = ["KITE 命令导航", ""]
    for spec in COMMAND_SPECS:
        lines.append(f"`{spec.usage}` — {spec.summary}")
    lines.append("")
    lines.append("直接发送文字即作为 prompt 提交给当前绑定的会话；群聊中需先 /group activate，并 @机器人 发送。")
    return "\n".join(lines)


def parse_permission_mode_arg(arg: str) -> str | None:
    """Normalize a /mode argument; None when not a valid permission mode."""
    normalized = str(arg or "").strip().lower()
    if normalized in VALID_PERMISSION_MODES:
        return normalized
    return None


def parse_plan_mode_arg(arg: str) -> bool | None:
    """Normalize a /plan argument (on/off); None when invalid."""
    normalized = str(arg or "").strip().lower()
    if normalized == _PLAN_ON:
        return True
    if normalized == _PLAN_OFF:
        return False
    return None


def parse_effort_arg(arg: str) -> str | None:
    """Normalize a /effort argument; None when not a valid effort level."""
    normalized = str(arg or "").strip().lower()
    if normalized in VALID_EFFORTS:
        return normalized
    return None


def parse_goal_keyword_arg(arg: str) -> str | None:
    """Normalize a /goal keyword (pause/resume/cancel/off); None when the
    argument is objective text instead of a keyword."""
    normalized = str(arg or "").strip().lower()
    if normalized in _GOAL_KEYWORDS:
        return normalized
    return None


def parse_group_mode_arg(arg: str) -> str | None:
    """Normalize a /group-mode argument; None when not a valid group mode.

    Spelling tolerance (FOCUS ``codex_group_domain`` parity, audit L14):
    ``-`` reads as ``_`` (``mention-only`` → ``mention_only``) and the
    shorthand ``mention`` reads as ``mention_only``.
    """
    normalized = str(arg or "").strip().lower().replace("-", "_")
    if normalized == "mention":
        normalized = "mention_only"
    if normalized in VALID_GROUP_MODES:
        return normalized
    return None
