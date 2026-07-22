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

from kite.stores.binding_store import VALID_PERMISSION_MODES

_PLAN_ON = "on"
_PLAN_OFF = "off"


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
        "/status",
        "/status",
        "查看绑定、会话工作状态与队列。",
    ),
    CommandSpec(
        "/abort",
        "/abort",
        "中止当前执行中的 prompt；仅发起者或管理员可用。",
    ),
    CommandSpec(
        "/init",
        "/init 〈token〉",
        "注册管理员；token 在安装时生成。",
    ),
    CommandSpec(
        "/help",
        "/help",
        "显示本命令导航。",
    ),
)

_SPECS_BY_NAME = {spec.name: spec for spec in COMMAND_SPECS}


def parse_slash_command(text: str) -> SlashCommand | None:
    """Parse ``/name arg...``; None for plain text or a bare ``/``.

    The name is lowercased and a trailing ``@BotName`` mention suffix (group
    convention) is stripped; the argument keeps its original casing with
    surrounding whitespace trimmed.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    name = head.lower()
    if "@" in name:
        name = name.split("@", 1)[0]
    if len(name) <= 1:
        return None
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
    lines.append("直接发送文字即作为 prompt 提交给当前绑定的会话。")
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
