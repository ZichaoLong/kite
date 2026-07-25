"""OS timer backends for `kitectl schedule` (docs/contracts/scheduled-prompts.md).

A scheduled prompt is deliberately NOT a daemon subsystem: the schedule lives
only in OS-level timer definitions named `kite-schedule-<hash>`, which fire
`<kitectl> prompt send --chat <id> --text <text> --display <mode>` back into
the daemon through the loopback control plane, so the fired prompt rides the
normal submit path (ownership recorded to the bound chat, modes carried from
the binding).

Platform boundary (contract §2): dispatch mirrors kite.service_manager —
Linux `systemd --user` timers, macOS launchd (`StartCalendarInterval`),
Windows Task Scheduler (time/calendar triggers). The schedule expression is
parsed once into a backend-neutral `SchedulePlan`; each backend renders only
the forms it can express faithfully and rejects the rest (fail-closed — a
schedule's semantics are never silently degraded).

The unit name hash comes from chat + schedule + text, so re-creating the same
scheduled prompt replaces the same timer definition instead of accumulating
(stable identity, contract §3).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import plistlib
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kite.platform_paths import (
    default_data_root,
    default_launch_agent_dir,
    default_systemd_user_dir,
    default_user_bin_dir,
    is_linux,
    is_macos,
    is_windows,
)

UNIT_PREFIX = "kite-schedule"
DISPLAY_MODES = ("silent", "announce")

_HASH_LENGTH = 12
_UNIT_SCALAR_FORBIDDEN = {"\x00", "\r", "\n"}
_NAME_RE = re.compile(rf"^{UNIT_PREFIX}-[0-9a-f]{{{_HASH_LENGTH}}}$")

_LAUNCHD_LABEL_PREFIX = "io.kite.schedule"
_TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

_SYSTEMD_SHORTHANDS = frozenset(
    {
        "minutely",
        "hourly",
        "daily",
        "weekly",
        "monthly",
        "yearly",
        "annually",
        "quarterly",
        "semiannually",
    }
)
_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_DOW_NAMES = {
    name: index
    for index, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])
}
_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class ScheduleError(ValueError):
    """A user-facing schedule validation failure (CLI exit 2)."""


class ScheduleBackendError(RuntimeError):
    """The OS timer driver call failed (CLI exit 1)."""


class ScheduleSystemctlError(ScheduleBackendError):
    """A `systemctl --user` call failed (CLI exit 1)."""


@dataclass(frozen=True, slots=True)
class CronFields:
    """A normalized 5-field cron expression (None = unrestricted `*`).

    dows are normalized to 0=Sunday..6=Saturday (cron's 7 folds into 0), the
    same convention launchd's Weekday uses.
    """

    minutes: frozenset[int] | None
    hours: frozenset[int] | None
    doms: frozenset[int] | None
    months: frozenset[int] | None
    dows: frozenset[int] | None


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """Backend-neutral schedule semantics.

    `on_calendar` is the canonical systemd text form: it feeds the stable
    identity hash, the systemd timer unit, and the display column of
    `kitectl schedule list` on every platform. Exactly one of the structured
    variants is set: `one_shot_at` (--at), `shorthand`, or `cron` (--cron).
    """

    on_calendar: str
    recurring: bool
    one_shot_at: datetime | None = None
    shorthand: str | None = None
    cron: CronFields | None = None


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Everything one backend needs to install one schedule."""

    name: str
    chat_id: str
    text: str
    on_calendar: str
    recurring: bool
    display: str
    ctl_path: str
    plan: SchedulePlan | None = None

    @property
    def timer_unit_name(self) -> str:
        return f"{self.name}.timer"

    @property
    def service_unit_name(self) -> str:
        return f"{self.name}.service"


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One row of `kitectl schedule list`."""

    name: str
    on_calendar: str
    next_elapse: str


@dataclass(frozen=True, slots=True)
class ScheduleUnitFiles:
    """Both unit files of one systemd schedule, for `kitectl schedule show`."""

    name: str
    timer_path: pathlib.Path
    service_path: pathlib.Path
    timer_text: str
    service_text: str


# ---------------------------------------------------------------------------
# Naming and paths
# ---------------------------------------------------------------------------


def schedule_name(chat_id: str, on_calendar: str, text: str) -> str:
    """Stable identity (contract §3): chat + schedule + text → one hash, so
    re-creating the same scheduled prompt rewrites the same unit pair."""
    digest = hashlib.sha256(
        "\n".join([chat_id, on_calendar, text]).encode("utf-8")
    ).hexdigest()
    return f"{UNIT_PREFIX}-{digest[:_HASH_LENGTH]}"


def timer_unit_path(name: str) -> pathlib.Path:
    return default_systemd_user_dir() / f"{name}.timer"


def service_unit_path(name: str) -> pathlib.Path:
    return default_systemd_user_dir() / f"{name}.service"


def normalize_name(raw: str) -> str:
    """Accept `kite-schedule-<hash>`, the bare hash, or either with a unit
    suffix; anything else is rejected (fail-closed)."""
    value = str(raw or "").strip()
    for suffix in (".timer", ".service", ".plist", ".xml"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    if value.startswith(f"{_LAUNCHD_LABEL_PREFIX}."):
        value = f"{UNIT_PREFIX}-{value[len(_LAUNCHD_LABEL_PREFIX) + 1:]}"
    if not value.startswith(f"{UNIT_PREFIX}-"):
        value = f"{UNIT_PREFIX}-{value}"
    if not _NAME_RE.fullmatch(value):
        raise ScheduleError(
            f"invalid schedule name: {raw!r} (expected {UNIT_PREFIX}-<hash>)"
        )
    return value


def _name_hash(name: str) -> str:
    return name[len(UNIT_PREFIX) + 1 :]


def launchd_label(name: str) -> str:
    return f"{_LAUNCHD_LABEL_PREFIX}.{_name_hash(name)}"


def launchd_plist_path(name: str) -> pathlib.Path:
    return default_launch_agent_dir() / f"{launchd_label(name)}.plist"


def schedule_data_dir() -> pathlib.Path:
    """Per-schedule artifacts owned by KITE (launchd logs, Task Scheduler XML
    registry) live under the data root."""
    return default_data_root() / "schedules"


def task_xml_path(name: str) -> pathlib.Path:
    return schedule_data_dir() / f"{name}.xml"


def schedule_unit_paths(raw_name: str) -> tuple[str, pathlib.Path, pathlib.Path]:
    """Normalize a user-given name and require at least one unit file."""
    base = normalize_name(raw_name)
    timer_path = timer_unit_path(base)
    service_path = service_unit_path(base)
    if not timer_path.exists() and not service_path.exists():
        raise ScheduleError(f"no schedule named {base}")
    return base, timer_path, service_path


# ---------------------------------------------------------------------------
# Scalar validation + schedule expressions
# ---------------------------------------------------------------------------


def _unit_scalar(value: str, field: str) -> str:
    """Timer definitions are line/element based: every rendered scalar must be
    one line."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ScheduleError(f"{field} must not be empty")
    if any(char in normalized for char in _UNIT_SCALAR_FORBIDDEN):
        raise ScheduleError(f"{field} must be a single line (no newline/NUL characters)")
    return normalized


def parse_at_schedule(raw: str, *, now: datetime | None = None) -> SchedulePlan:
    """`--at` → a one-shot plan; a past time is rejected before anything is
    written (fail-closed, contract §3)."""
    value = _unit_scalar(raw, "--at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError(f"--at must be an ISO 8601 timestamp, got: {raw!r}") from None
    if parsed.tzinfo is not None:
        # None of the backends has a timezone component: they all evaluate in
        # local time, so convert an offset-aware timestamp to local wall time.
        parsed = parsed.astimezone().replace(tzinfo=None)
    reference = now if now is not None else datetime.now()
    if parsed <= reference.replace(tzinfo=None):
        raise ScheduleError(f"--at is in the past: {parsed:%Y-%m-%d %H:%M:%S}")
    return SchedulePlan(
        on_calendar=f"{parsed:%Y-%m-%d %H:%M:%S}",
        recurring=False,
        one_shot_at=parsed,
    )


def parse_at_on_calendar(raw: str, *, now: datetime | None = None) -> str:
    """`--at` → a one-shot OnCalendar timestamp (see parse_at_schedule)."""
    return parse_at_schedule(raw, now=now).on_calendar


def parse_cron_schedule(raw: str) -> SchedulePlan:
    """`--cron` → a recurring plan.

    Accepted (contract §3): systemd's OnCalendar shorthand forms (daily,
    hourly, ...) passed through verbatim, and standard 5-field cron. Backends
    that cannot express a parsed form faithfully reject it at render time
    (fail-closed); unparseable input is rejected here, before anything is
    written.
    """
    value = _unit_scalar(raw, "--cron")
    lowered = value.lower()
    if lowered in _SYSTEMD_SHORTHANDS:
        return SchedulePlan(on_calendar=lowered, recurring=True, shorthand=lowered)
    fields = value.split()
    if len(fields) != 5:
        raise ScheduleError(
            "--cron must be a systemd OnCalendar shorthand "
            f"({', '.join(sorted(_SYSTEMD_SHORTHANDS))}) or a standard "
            "5-field cron expression"
        )
    minutes = _parse_cron_field(fields[0], lo=0, hi=59, label="minute")
    hours = _parse_cron_field(fields[1], lo=0, hi=23, label="hour")
    doms = _parse_cron_field(fields[2], lo=1, hi=31, label="day-of-month")
    months = _parse_cron_field(fields[3], lo=1, hi=12, label="month", names=_MONTH_NAMES)
    dows = _parse_cron_field(fields[4], lo=0, hi=7, label="day-of-week", names=_DOW_NAMES)
    if doms is not None and dows is not None:
        # Vixie cron ORs a restricted dom with a restricted dow; every backend
        # here ANDs them. Refuse the ambiguity instead of silently changing
        # the schedule's meaning.
        raise ScheduleError(
            "--cron restricts both day-of-month and day-of-week: cron ORs them "
            "but calendar timers AND them; split this into two schedules"
        )
    normalized_dows: frozenset[int] | None = None
    dow_prefix = ""
    if dows is not None:
        normalized = sorted({0 if day == 7 else day for day in dows})
        normalized_dows = frozenset(normalized)
        dow_prefix = ",".join(_DOW_LABELS[day] for day in normalized) + " "
    date_part = f"*-{_render_ints(months)}-{_render_ints(doms)}"
    time_part = f"{_render_ints(hours)}:{_render_ints(minutes)}:00"
    return SchedulePlan(
        on_calendar=f"{dow_prefix}{date_part} {time_part}",
        recurring=True,
        cron=CronFields(
            minutes=minutes,
            hours=hours,
            doms=doms,
            months=months,
            dows=normalized_dows,
        ),
    )


def parse_cron_on_calendar(raw: str) -> str:
    """`--cron` → a recurring OnCalendar expression (see parse_cron_schedule)."""
    return parse_cron_schedule(raw).on_calendar


def _parse_cron_field(
    field: str,
    *,
    lo: int,
    hi: int,
    label: str,
    names: dict[str, int] | None = None,
) -> frozenset[int] | None:
    """One cron field → the matched value set; None means unrestricted (`*`)."""
    values: set[int] = set()
    for token in field.split(","):
        token = token.strip().lower()
        base, step = token, 1
        if "/" in token:
            base, _, step_text = token.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"--cron {label}: bad step in {token!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start = _cron_value(start_text, label=label, names=names)
            end = _cron_value(end_text, label=label, names=names)
            if start > end:
                raise ScheduleError(f"--cron {label}: inverted range {token!r}")
        else:
            start = _cron_value(base, label=label, names=names)
            # Vixie cron extension: "a/n" means a..hi with step n.
            end = hi if "/" in token else start
        if start < lo or end > hi:
            raise ScheduleError(
                f"--cron {label}: value out of range ({lo}-{hi}) in {token!r}"
            )
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"--cron {label}: empty field")
    if values == set(range(lo, hi + 1)):
        return None
    return frozenset(values)


def _cron_value(text: str, *, label: str, names: dict[str, int] | None) -> int:
    if names and text in names:
        return names[text]
    if text.isdigit():
        return int(text)
    raise ScheduleError(f"--cron {label}: unparseable value {text!r}")


def _render_ints(values: frozenset[int] | None) -> str:
    if values is None:
        return "*"
    return ",".join(f"{value:02d}" for value in sorted(values))


# ---------------------------------------------------------------------------
# kitectl path resolution (stored in the timer definition; contract §3)
# ---------------------------------------------------------------------------


def _is_executable_file(path: pathlib.Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _ctl_path_candidates() -> tuple[pathlib.Path, ...]:
    """Default kitectl locations, per platform (audit L20): the user bin dir
    wrapper, then the managed venv — ``bin/kitectl`` on POSIX, but
    ``Scripts/kitectl.exe`` on Windows (a venv has no ``bin/`` there)."""
    if is_windows():
        return (
            default_user_bin_dir() / "kitectl.exe",
            default_data_root() / ".venv" / "Scripts" / "kitectl.exe",
        )
    return (
        default_user_bin_dir() / "kitectl",
        default_data_root() / ".venv" / "bin" / "kitectl",
    )


def resolve_ctl_path(explicit: str = "") -> str:
    """Contract §3 order: explicit `--ctl-path` > `KITE_BIN_DIR/kitectl` (or
    `~/.local/bin/kitectl`) > the managed venv (`bin/kitectl`, or
    `Scripts/kitectl.exe` on Windows). The resolved absolute path is stored
    in the timer definition — an OS timer has no PATH to search."""
    if str(explicit or "").strip():
        candidate = pathlib.Path(explicit).expanduser()
        if not _is_executable_file(candidate):
            raise ScheduleError(f"--ctl-path is not an executable file: {candidate}")
        return str(candidate.resolve())
    candidates = _ctl_path_candidates()
    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate.resolve())
    raise ScheduleError(
        "kitectl not found; checked "
        + " and ".join(str(candidate) for candidate in candidates)
        + "; pass --ctl-path explicitly"
    )


# ---------------------------------------------------------------------------
# Spec building (shared validation, before any backend writes anything)
# ---------------------------------------------------------------------------


def build_schedule_spec(
    *,
    chat_id: str,
    text: str,
    plan: SchedulePlan,
    display: str,
    ctl_path: str,
) -> ScheduleSpec:
    """Validate every input and derive the stable name — before the first
    write on any backend (fail-closed, contract §3)."""
    name = schedule_name(
        _unit_scalar(chat_id, "chat_id"),
        _unit_scalar(plan.on_calendar, "on_calendar"),
        _unit_scalar(text, "text"),
    )
    if display not in DISPLAY_MODES:
        raise ScheduleError(f"display must be one of {list(DISPLAY_MODES)}")
    return ScheduleSpec(
        name=name,
        chat_id=chat_id,
        text=text,
        on_calendar=plan.on_calendar,
        recurring=plan.recurring,
        display=display,
        ctl_path=_unit_scalar(ctl_path, "ctl_path"),
        plan=plan,
    )


def _require_plan(spec: ScheduleSpec) -> SchedulePlan:
    if spec.plan is None:
        raise ScheduleError(f"schedule {spec.name} carries no structured schedule data")
    return spec.plan


# ---------------------------------------------------------------------------
# systemd unit rendering
# ---------------------------------------------------------------------------


def _quote_unit_arg(arg: str) -> str:
    """systemd ExecStart quoting (same convention as kite.service_manager).

    `%` must become `%%`: systemd specifier expansion would otherwise rewrite
    user text (`%h` → home path) or reject the unit outright (`bad-setting`)
    (audit H3).
    """
    escaped = str(arg).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_service_unit(spec: ScheduleSpec) -> str:
    description = _unit_scalar(
        f"KITE scheduled prompt (chat {spec.chat_id})", "description"
    ).replace("%", "%%")
    exec_start = " ".join(
        _quote_unit_arg(part)
        for part in [
            spec.ctl_path,
            "prompt",
            "send",
            "--chat",
            _unit_scalar(spec.chat_id, "chat_id"),
            "--text",
            _unit_scalar(spec.text, "text"),
            "--display",
            _unit_scalar(spec.display, "display"),
        ]
    )
    return "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={exec_start}",
            "",
        ]
    )


def render_timer_unit(spec: ScheduleSpec) -> str:
    description = _unit_scalar(
        f"KITE scheduled prompt (chat {spec.chat_id})", "description"
    ).replace("%", "%%")
    on_calendar = _unit_scalar(spec.on_calendar, "on_calendar")
    return "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "",
            "[Timer]",
            f"OnCalendar={on_calendar}",
            # A missed fire (daemon/host down) fires once on recovery instead
            # of vanishing (contract §1.3).
            "Persistent=true",
            f"Unit={spec.service_unit_name}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# launchd plist rendering (macOS)
# ---------------------------------------------------------------------------

# systemd shorthand → StartCalendarInterval. The field semantics match
# systemd's definitions exactly (systemd.time(7)): weekly = Monday 00:00,
# monthly = the 1st 00:00, quarterly = Jan/Apr/Jul/Oct, and so on. An empty
# dict fires every minute (launchd's wildcard), which is `minutely`.
_LAUNCHD_SHORTHAND_INTERVALS: dict[str, dict[str, Any]] = {
    "minutely": {},
    "hourly": {"Minute": 0},
    "daily": {"Hour": 0, "Minute": 0},
    "weekly": {"Weekday": 1, "Hour": 0, "Minute": 0},
    "monthly": {"Day": 1, "Hour": 0, "Minute": 0},
    "yearly": {"Month": 1, "Day": 1, "Hour": 0, "Minute": 0},
    "annually": {"Month": 1, "Day": 1, "Hour": 0, "Minute": 0},
    "quarterly": {"Month": [1, 4, 7, 10], "Day": 1, "Hour": 0, "Minute": 0},
    "semiannually": {"Month": [1, 7], "Day": 1, "Hour": 0, "Minute": 0},
}


def _launchd_int_or_list(values: frozenset[int]) -> int | list[int]:
    ordered = sorted(values)
    return ordered[0] if len(ordered) == 1 else ordered


def launchd_start_calendar_interval(plan: SchedulePlan) -> dict[str, Any]:
    """SchedulePlan → StartCalendarInterval. launchd accepts an int or a list
    of ints per key and ANDs the keys, so every form the cron parser accepts
    maps faithfully (the dom+dow OR ambiguity is already rejected upstream)."""
    if plan.one_shot_at is not None:
        # launchd.plist(5) StartCalendarInterval defines only
        # Minute/Hour/Day/Weekday/Month — there is no Year key, so a one-shot
        # --at cannot be expressed faithfully (an illegal Year key is
        # ignored and the job silently degrades to a yearly repeat, audit
        # M13). Fail closed instead of rendering a lie.
        raise ScheduleError(
            "launchd (macOS) cannot express one-shot --at schedules; "
            "use a cron expression, or create the schedule on Linux (systemd) "
            "or Windows (Task Scheduler)"
        )
    if plan.shorthand is not None:
        return dict(_LAUNCHD_SHORTHAND_INTERVALS[plan.shorthand])
    cron = plan.cron
    if cron is None:
        raise ScheduleError("schedule has no launchd StartCalendarInterval mapping")
    interval: dict[str, Any] = {}
    if cron.minutes is not None:
        interval["Minute"] = _launchd_int_or_list(cron.minutes)
    if cron.hours is not None:
        interval["Hour"] = _launchd_int_or_list(cron.hours)
    if cron.doms is not None:
        interval["Day"] = _launchd_int_or_list(cron.doms)
    if cron.months is not None:
        interval["Month"] = _launchd_int_or_list(cron.months)
    if cron.dows is not None:
        interval["Weekday"] = _launchd_int_or_list(cron.dows)
    return interval


def render_launchd_plist(spec: ScheduleSpec) -> bytes:
    plan = _require_plan(spec)
    payload = {
        "Label": launchd_label(spec.name),
        # plist arrays carry each argument verbatim — no shell quoting layer.
        "ProgramArguments": [
            _unit_scalar(spec.ctl_path, "ctl_path"),
            "prompt",
            "send",
            "--chat",
            _unit_scalar(spec.chat_id, "chat_id"),
            "--text",
            _unit_scalar(spec.text, "text"),
            "--display",
            _unit_scalar(spec.display, "display"),
        ],
        "StartCalendarInterval": launchd_start_calendar_interval(plan),
        "RunAtLoad": False,
        "StandardOutPath": str(schedule_data_dir() / f"{spec.name}.stdout.log"),
        "StandardErrorPath": str(schedule_data_dir() / f"{spec.name}.stderr.log"),
    }
    return plistlib.dumps(payload)


def _describe_launchd_interval(value: object) -> str:
    """Display-only inverse of launchd_start_calendar_interval for
    `kitectl schedule list`."""
    if not isinstance(value, dict):
        return ""

    def _fmt(key: str) -> str:
        item = value.get(key)
        if item is None:
            return "*"
        if isinstance(item, list):
            return ",".join(str(part) for part in item)
        return str(item)

    if "Year" in value:
        return (
            f"at {_fmt('Year')}-{_fmt('Month')}-{_fmt('Day')} "
            f"{_fmt('Hour')}:{_fmt('Minute')}"
        )
    if not value:
        return "* * * * * (every minute)"
    return f"{_fmt('Minute')} {_fmt('Hour')} {_fmt('Day')} {_fmt('Month')} {_fmt('Weekday')}"


# ---------------------------------------------------------------------------
# Task Scheduler XML rendering (Windows)
# ---------------------------------------------------------------------------

_WEEKDAY_XML_TAGS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTH_XML_TAGS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@dataclass(frozen=True, slots=True)
class _TaskTriggerShape:
    """One Task Scheduler calendar trigger: a single time of day plus a
    day/week/month grid (the only shapes schtasks expresses faithfully)."""

    kind: str  # "day" | "week" | "month"
    hour: int
    minute: int
    dows: frozenset[int] | None = None  # 0=Sunday..6=Saturday
    doms: frozenset[int] | None = None
    months: frozenset[int] | None = None
    repetition: str | None = None  # ISO 8601 duration, e.g. "PT1H"


# systemd shorthand → trigger shape. daily/weekly/monthly/yearly/quarterly/
# semiannually map 1:1 onto the calendar grids; minutely/hourly map onto a
# daily grid with a repetition interval (the honest "every N" form).
_SCHTASKS_SHORTHAND_SHAPES: dict[str, _TaskTriggerShape] = {
    "minutely": _TaskTriggerShape("day", 0, 0, repetition="PT1M"),
    "hourly": _TaskTriggerShape("day", 0, 0, repetition="PT1H"),
    "daily": _TaskTriggerShape("day", 0, 0),
    "weekly": _TaskTriggerShape("week", 0, 0, dows=frozenset({1})),
    "monthly": _TaskTriggerShape("month", 0, 0, doms=frozenset({1})),
    "yearly": _TaskTriggerShape("month", 0, 0, doms=frozenset({1}), months=frozenset({1})),
    "annually": _TaskTriggerShape("month", 0, 0, doms=frozenset({1}), months=frozenset({1})),
    "quarterly": _TaskTriggerShape("month", 0, 0, doms=frozenset({1}), months=frozenset({1, 4, 7, 10})),
    "semiannually": _TaskTriggerShape("month", 0, 0, doms=frozenset({1}), months=frozenset({1, 7})),
}


def _schtasks_single_value(values: frozenset[int] | None, label: str) -> int:
    """Task Scheduler carries the time of day in StartBoundary, so only a
    single minute/hour value maps faithfully. Sets, steps, and wildcards are
    rejected instead of degraded (fail-closed, contract §2)."""
    if values is None or len(values) != 1:
        raise ScheduleError(
            f"--cron {label}: Task Scheduler maps exactly one time of day per "
            "trigger; wildcards, lists, and steps in minute/hour have no "
            "faithful mapping"
        )
    return next(iter(values))


def _schtasks_trigger_shape(plan: SchedulePlan) -> _TaskTriggerShape:
    if plan.shorthand is not None:
        return _SCHTASKS_SHORTHAND_SHAPES[plan.shorthand]
    cron = plan.cron
    if cron is None:
        raise ScheduleError("schedule has no Task Scheduler trigger mapping")
    minute = _schtasks_single_value(cron.minutes, "minute")
    hour = _schtasks_single_value(cron.hours, "hour")
    if cron.dows is not None:
        if cron.months is not None:
            raise ScheduleError(
                "--cron restricts both month and day-of-week: Task Scheduler's "
                "month+weekday trigger counts weeks of the month, it does not "
                "mean 'every such weekday in these months'; no faithful mapping"
            )
        return _TaskTriggerShape("week", hour, minute, dows=cron.dows)
    if cron.doms is not None:
        return _TaskTriggerShape("month", hour, minute, doms=cron.doms, months=cron.months)
    if cron.months is not None:
        # Every day at H:M within the listed months: DaysOfMonth 1..31 skips
        # nonexistent days exactly like cron's dom=* does.
        return _TaskTriggerShape(
            "month", hour, minute, doms=frozenset(range(1, 32)), months=cron.months
        )
    return _TaskTriggerShape("day", hour, minute)


def _quote_windows_arg(arg: str) -> str:
    """CommandLineToArgvW quoting for the task's <Arguments> string:
    backslashes are literal unless they precede a quote, embedded quotes are
    backslash-escaped, and the whole argument is wrapped in quotes."""
    parts: list[str] = []
    backslashes = 0
    for char in str(arg):
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            parts.append("\\" * (backslashes * 2 + 1))
            parts.append('"')
        else:
            parts.append("\\" * backslashes)
            parts.append(char)
        backslashes = 0
    parts.append("\\" * (backslashes * 2))
    return '"' + "".join(parts) + '"'


def render_task_xml(spec: ScheduleSpec, *, now: datetime | None = None) -> bytes:
    """SchedulePlan → a schtasks /Create /XML task definition."""
    plan = _require_plan(spec)
    reference = now if now is not None else datetime.now()
    ET.register_namespace("", _TASK_XML_NAMESPACE)

    def tag(name: str) -> str:
        return f"{{{_TASK_XML_NAMESPACE}}}{name}"

    task = ET.Element(tag("Task"), {"version": "1.3"})
    registration = ET.SubElement(task, tag("RegistrationInfo"))
    ET.SubElement(registration, tag("Description")).text = _unit_scalar(
        f"KITE scheduled prompt (chat {spec.chat_id})", "description"
    )
    triggers = ET.SubElement(task, tag("Triggers"))
    if plan.one_shot_at is not None:
        trigger = ET.SubElement(triggers, tag("TimeTrigger"))
        ET.SubElement(trigger, tag("Enabled")).text = "true"
        ET.SubElement(trigger, tag("StartBoundary")).text = (
            f"{plan.one_shot_at:%Y-%m-%dT%H:%M:%S}"
        )
    else:
        shape = _schtasks_trigger_shape(plan)
        trigger = ET.SubElement(triggers, tag("CalendarTrigger"))
        ET.SubElement(trigger, tag("Enabled")).text = "true"
        # The calendar grids take their time of day from StartBoundary; the
        # date component only needs to be a valid start (install day).
        ET.SubElement(trigger, tag("StartBoundary")).text = (
            f"{reference:%Y-%m-%d}T{shape.hour:02d}:{shape.minute:02d}:00"
        )
        if shape.repetition is not None:
            repetition = ET.SubElement(trigger, tag("Repetition"))
            ET.SubElement(repetition, tag("Interval")).text = shape.repetition
            ET.SubElement(repetition, tag("StopAtDurationEnd")).text = "false"
        if shape.kind == "day":
            schedule = ET.SubElement(trigger, tag("ScheduleByDay"))
            ET.SubElement(schedule, tag("DaysInterval")).text = "1"
        elif shape.kind == "week":
            schedule = ET.SubElement(trigger, tag("ScheduleByWeek"))
            days = ET.SubElement(schedule, tag("DaysOfWeek"))
            for dow in sorted(shape.dows or ()):
                ET.SubElement(days, tag(_WEEKDAY_XML_TAGS[dow]))
            ET.SubElement(schedule, tag("WeeksInterval")).text = "1"
        else:
            schedule = ET.SubElement(trigger, tag("ScheduleByMonth"))
            days = ET.SubElement(schedule, tag("DaysOfMonth"))
            for day in sorted(shape.doms or ()):
                ET.SubElement(days, tag("Day")).text = str(day)
            months = ET.SubElement(schedule, tag("Months"))
            for month in sorted(shape.months or range(1, 13)):
                ET.SubElement(months, tag(_MONTH_XML_TAGS[month - 1]))
    principals = ET.SubElement(task, tag("Principals"))
    principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(task, tag("Settings"))
    for key, value in (
        ("AllowStartOnDemand", "true"),  # required for `schedule run-now`
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        # Persistent=true analog: a missed fire runs once on recovery.
        ("StartWhenAvailable", "true"),
        ("Enabled", "true"),
    ):
        ET.SubElement(settings, tag(key)).text = value
    actions = ET.SubElement(task, tag("Actions"), {"Context": "Author"})
    exec_action = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(exec_action, tag("Command")).text = _unit_scalar(spec.ctl_path, "ctl_path")
    ET.SubElement(exec_action, tag("Arguments")).text = " ".join(
        _quote_windows_arg(part)
        for part in [
            "prompt",
            "send",
            "--chat",
            _unit_scalar(spec.chat_id, "chat_id"),
            "--text",
            _unit_scalar(spec.text, "text"),
            "--display",
            _unit_scalar(spec.display, "display"),
        ]
    )
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def _describe_task_xml(root: ET.Element) -> str:
    """Display-only summary of the first trigger, for `kitectl schedule list`."""

    def tag(name: str) -> str:
        return f"{{{_TASK_XML_NAMESPACE}}}{name}"

    triggers = root.find(tag("Triggers"))
    if triggers is None or len(triggers) == 0:
        return ""
    trigger = triggers[0]
    start = trigger.findtext(tag("StartBoundary"), default="")
    kind = trigger.tag.rsplit("}", 1)[-1]
    if kind == "TimeTrigger":
        repetition = trigger.find(f"{tag('Repetition')}/{tag('Interval')}")
        if repetition is not None and repetition.text:
            return f"every {repetition.text} from {start}"
        return f"at {start}"
    time_part = start[11:16] if len(start) >= 16 else start
    if trigger.find(tag("ScheduleByDay")) is not None:
        return f"daily {time_part}".strip()
    if trigger.find(tag("ScheduleByWeek")) is not None:
        days = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(f"{tag('ScheduleByWeek')}/{tag('DaysOfWeek')}/*")
        ]
        return f"weekly {','.join(days)} {time_part}".strip()
    if trigger.find(tag("ScheduleByMonth")) is not None:
        months = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(f"{tag('ScheduleByMonth')}/{tag('Months')}/*")
        ]
        doms = [
            element.text or ""
            for element in trigger.findall(f"{tag('ScheduleByMonth')}/{tag('DaysOfMonth')}/*")
        ]
        return f"monthly {','.join(months)} day {','.join(doms)} {time_part}".strip()
    return kind


# ---------------------------------------------------------------------------
# OS timer drivers
# ---------------------------------------------------------------------------


def _run_systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            check=check,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ScheduleSystemctlError(
            "systemctl is not available; kitectl schedule needs systemd --user (Linux)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ScheduleSystemctlError(message) from exc


def _run_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *args],
            check=check,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ScheduleBackendError(
            "launchctl is not available; kitectl schedule needs launchd (macOS)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ScheduleBackendError(message) from exc


def _run_schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["schtasks", *args],
            check=check,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ScheduleBackendError(
            "schtasks is not available; kitectl schedule needs Task Scheduler (Windows)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise ScheduleBackendError(message) from exc


# ---------------------------------------------------------------------------
# Backends (contract §2 platform boundary)
# ---------------------------------------------------------------------------


class ScheduleBackend:
    """One OS timer backend: install/list/show/remove/run-now over the
    platform's own scheduler. Files are the state KITE owns; the OS driver
    calls register, unregister, and fire."""

    def install(self, spec: ScheduleSpec) -> list[tuple[str, pathlib.Path]]:
        """Write the timer definition and register it with the OS scheduler;
        returns the labeled artifacts written. The schedule is never fired
        manually for the future time (the OS owns firing, contract §3)."""
        raise NotImplementedError

    def list(self) -> list[ScheduleEntry]:
        raise NotImplementedError

    def show(self, raw_name: str) -> list[tuple[pathlib.Path, str]]:
        """(path, text) pairs of the stored definition(s)."""
        raise NotImplementedError

    def resolve_name(self, raw_name: str) -> str:
        """Normalize a user-given name and require the schedule to exist."""
        raise NotImplementedError

    def remove(self, raw_name: str) -> str:
        raise NotImplementedError

    def run_now(self, raw_name: str) -> str:
        raise NotImplementedError


class SystemdScheduleBackend(ScheduleBackend):
    """Linux `systemd --user` timers (the original backend, unchanged)."""

    def install(self, spec: ScheduleSpec) -> list[tuple[str, pathlib.Path]]:
        unit_dir = default_systemd_user_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_unit_path(spec.name).write_text(render_service_unit(spec), encoding="utf-8")
        timer_unit_path(spec.name).write_text(render_timer_unit(spec), encoding="utf-8")
        _run_systemctl("daemon-reload")
        _run_systemctl("enable", "--now", spec.timer_unit_name)
        return [
            ("timer_unit", timer_unit_path(spec.name)),
            ("service_unit", service_unit_path(spec.name)),
        ]

    def list(self) -> list[ScheduleEntry]:
        return list_schedules()

    def show(self, raw_name: str) -> list[tuple[pathlib.Path, str]]:
        shown = show_schedule(raw_name)
        return [
            (shown.timer_path, shown.timer_text),
            (shown.service_path, shown.service_text),
        ]

    def resolve_name(self, raw_name: str) -> str:
        base, _timer_path, _service_path = schedule_unit_paths(raw_name)
        return base

    def remove(self, raw_name: str) -> str:
        return remove_schedule(raw_name)

    def run_now(self, raw_name: str) -> str:
        return run_schedule_now(raw_name)


class LaunchdScheduleBackend(ScheduleBackend):
    """macOS launchd agents with StartCalendarInterval.

    Unlike systemd's Persistent=true, launchd drops a missed fire while the
    job was not loaded — an honest platform limitation, surfaced via the
    plist logs rather than hidden.
    """

    def _uid_domain(self) -> str:
        return f"gui/{os.getuid()}"

    def install(self, spec: ScheduleSpec) -> list[tuple[str, pathlib.Path]]:
        plist_path = launchd_plist_path(spec.name)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        schedule_data_dir().mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(render_launchd_plist(spec))
        domain = self._uid_domain()
        label = launchd_label(spec.name)
        # Re-creating an existing schedule replaces it (stable identity).
        _run_launchctl("bootout", domain, label, check=False)
        result = _run_launchctl("bootstrap", domain, str(plist_path), check=False)
        if result.returncode != 0:
            # Pre-bootstrap macOS releases only know load.
            _run_launchctl("load", str(plist_path))
        return [("plist", plist_path)]

    def list(self) -> list[ScheduleEntry]:
        entries: list[ScheduleEntry] = []
        agent_dir = default_launch_agent_dir()
        for plist_path in sorted(agent_dir.glob(f"{_LAUNCHD_LABEL_PREFIX}.*.plist")):
            digest = plist_path.name[len(_LAUNCHD_LABEL_PREFIX) + 1 : -len(".plist")]
            base = f"{UNIT_PREFIX}-{digest}"
            if not _NAME_RE.fullmatch(base):
                continue
            on_calendar = "-"
            try:
                payload = plistlib.loads(plist_path.read_bytes())
                on_calendar = (
                    _describe_launchd_interval(payload.get("StartCalendarInterval")) or "-"
                )
            except (OSError, ValueError):
                pass
            # launchd publishes no next-fire time; the only state worth
            # surfacing is whether the job is actually loaded.
            result = _run_launchctl("list", launchd_label(base), check=False)
            next_elapse = "-" if result.returncode == 0 else "(not loaded)"
            entries.append(
                ScheduleEntry(name=base, on_calendar=on_calendar, next_elapse=next_elapse)
            )
        return entries

    def resolve_name(self, raw_name: str) -> str:
        base = normalize_name(raw_name)
        if not launchd_plist_path(base).exists():
            raise ScheduleError(f"no schedule named {base}")
        return base

    def show(self, raw_name: str) -> list[tuple[pathlib.Path, str]]:
        base = self.resolve_name(raw_name)
        plist_path = launchd_plist_path(base)
        try:
            text = plist_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            text = f"(missing: {plist_path})\n"
        if not text.endswith("\n"):
            text += "\n"
        return [(plist_path, text)]

    def remove(self, raw_name: str) -> str:
        base = self.resolve_name(raw_name)
        plist_path = launchd_plist_path(base)
        result = _run_launchctl("bootout", self._uid_domain(), launchd_label(base), check=False)
        if result.returncode != 0:
            _run_launchctl("unload", str(plist_path), check=False)
        try:
            plist_path.unlink()
        except FileNotFoundError:
            pass
        return base

    def run_now(self, raw_name: str) -> str:
        base = self.resolve_name(raw_name)
        _run_launchctl("start", launchd_label(base))
        return base


class TaskSchedulerScheduleBackend(ScheduleBackend):
    """Windows Task Scheduler calendar triggers.

    The XML files under `<data root>/schedules/` are the registry KITE owns
    (list/show read them); schtasks registers and fires the tasks.
    """

    def install(self, spec: ScheduleSpec) -> list[tuple[str, pathlib.Path]]:
        xml_path = task_xml_path(spec.name)
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(render_task_xml(spec))
        _run_schtasks("/Create", "/TN", spec.name, "/XML", str(xml_path), "/F")
        return [("task_xml", xml_path)]

    def resolve_name(self, raw_name: str) -> str:
        base = normalize_name(raw_name)
        if not task_xml_path(base).exists():
            raise ScheduleError(f"no schedule named {base}")
        return base

    def list(self) -> list[ScheduleEntry]:
        entries: list[ScheduleEntry] = []
        registry = schedule_data_dir()
        for xml_path in sorted(registry.glob(f"{UNIT_PREFIX}-*.xml")):
            base = xml_path.name[: -len(".xml")]
            if not _NAME_RE.fullmatch(base):
                continue
            on_calendar = "-"
            try:
                on_calendar = _describe_task_xml(ET.fromstring(xml_path.read_bytes())) or "-"
            except (OSError, ET.ParseError):
                pass
            entries.append(
                ScheduleEntry(
                    name=base,
                    on_calendar=on_calendar,
                    next_elapse=self._next_run_time(base),
                )
            )
        return entries

    def _next_run_time(self, base: str) -> str:
        result = _run_schtasks("/Query", "/TN", base, "/FO", "LIST", "/V", check=False)
        if result.returncode != 0:
            return "-"
        for line in result.stdout.splitlines():
            if line.strip().startswith("Next Run Time:"):
                return line.split(":", 1)[1].strip() or "-"
        return "-"

    def show(self, raw_name: str) -> list[tuple[pathlib.Path, str]]:
        base = self.resolve_name(raw_name)
        xml_path = task_xml_path(base)
        try:
            text = xml_path.read_bytes().decode("utf-16", errors="replace")
        except OSError:
            text = f"(missing: {xml_path})\n"
        if not text.endswith("\n"):
            text += "\n"
        return [(xml_path, text)]

    def remove(self, raw_name: str) -> str:
        base = self.resolve_name(raw_name)
        xml_path = task_xml_path(base)
        _run_schtasks("/Delete", "/TN", base, "/F", check=False)
        try:
            xml_path.unlink()
        except FileNotFoundError:
            pass
        return base

    def run_now(self, raw_name: str) -> str:
        base = self.resolve_name(raw_name)
        _run_schtasks("/Run", "/TN", base)
        return base


def current_schedule_backend() -> ScheduleBackend:
    """Platform dispatch (contract §2), same rule as service_manager."""
    if is_windows():
        return TaskSchedulerScheduleBackend()
    if is_macos():
        return LaunchdScheduleBackend()
    if is_linux():
        return SystemdScheduleBackend()
    raise ScheduleError(
        "kitectl schedule is not supported on this platform "
        "(need systemd --user, launchd, or Task Scheduler)"
    )


# ---------------------------------------------------------------------------
# systemd module-level API (the Linux path; the backend delegates here)
# ---------------------------------------------------------------------------


def create_schedule(
    *,
    chat_id: str,
    text: str,
    on_calendar: str,
    recurring: bool,
    display: str,
    ctl_path: str,
) -> ScheduleSpec:
    """Write the unit pair and enable the timer (systemd owns firing; the
    service is never started manually for the future time, contract §3).
    Every input is validated before the first write."""
    spec = build_schedule_spec(
        chat_id=chat_id,
        text=text,
        plan=SchedulePlan(on_calendar=str(on_calendar), recurring=bool(recurring)),
        display=display,
        ctl_path=ctl_path,
    )
    SystemdScheduleBackend().install(spec)
    return spec


def _read_on_calendar(timer_path: pathlib.Path) -> str:
    try:
        for line in timer_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("OnCalendar="):
                return line[len("OnCalendar=") :].strip()
    except OSError:
        pass
    return ""


def _list_timers_next() -> dict[str, str] | None:
    """{unit base name: NEXT column} from `systemctl --user list-timers`;
    None when systemctl is unavailable or errors (unit files are the
    fallback, contract §3)."""
    try:
        result = _run_systemctl(
            "list-timers",
            "--all",
            "--no-legend",
            "--no-pager",
            f"{UNIT_PREFIX}-*.timer",
            check=False,
        )
    except ScheduleSystemctlError:
        return None
    if result.returncode != 0:
        return None
    next_map: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Columns: NEXT LEFT LAST PASSED UNIT ACTIVATES, padded by 2+ spaces
        # (timestamps contain single spaces, the delta columns never two).
        parts = re.split(r"\s{2,}", stripped, maxsplit=5)
        if len(parts) != 6:
            continue
        next_text, _left, _last, _passed, unit, _activates = parts
        base = unit.removesuffix(".timer")
        if base.startswith(f"{UNIT_PREFIX}-"):
            next_map[base] = next_text
    return next_map


def list_schedules() -> list[ScheduleEntry]:
    """Enumerate `kite-schedule-*.timer` unit files; the next-elapse column
    comes from `systemctl --user list-timers` when available."""
    unit_dir = default_systemd_user_dir()
    next_map = _list_timers_next()
    entries: list[ScheduleEntry] = []
    for timer_path in sorted(unit_dir.glob(f"{UNIT_PREFIX}-*.timer")):
        base = timer_path.name[: -len(".timer")]
        entries.append(
            ScheduleEntry(
                name=base,
                on_calendar=_read_on_calendar(timer_path) or "-",
                next_elapse=(next_map or {}).get(base, "-"),
            )
        )
    return entries


def show_schedule(raw_name: str) -> ScheduleUnitFiles:
    base, timer_path, service_path = schedule_unit_paths(raw_name)

    def _read(path: pathlib.Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return f"(missing: {path})\n"

    return ScheduleUnitFiles(
        name=base,
        timer_path=timer_path,
        service_path=service_path,
        timer_text=_read(timer_path),
        service_text=_read(service_path),
    )


def remove_schedule(raw_name: str) -> str:
    """Disable + delete the unit pair. systemctl is best-effort here: the
    files are the state we own and must go away regardless."""
    base, timer_path, service_path = schedule_unit_paths(raw_name)
    _run_systemctl("disable", "--now", timer_path.name, check=False)
    for path in (timer_path, service_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _run_systemctl("daemon-reload", check=False)
    return base


def run_schedule_now(raw_name: str) -> str:
    """Fire the service unit once immediately (contract §3)."""
    base, _timer_path, service_path = schedule_unit_paths(raw_name)
    if not service_path.exists():
        raise ScheduleError(f"schedule {base} has no service unit: {service_path}")
    _run_systemctl("start", service_path.name)
    return base
