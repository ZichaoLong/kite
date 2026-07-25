"""systemd --user timer units for `kitectl schedule` (docs/contracts/scheduled-prompts.md).

A scheduled prompt is deliberately NOT a daemon subsystem: the schedule lives
only in the `kite-schedule-<hash>.timer` / `.service` pair under
`~/.config/systemd/user/`. The timer fires
`<kitectl> prompt send --chat <id> --text <text> --display <mode>` back into
the daemon through the loopback control plane, so the fired prompt rides the
normal submit path (ownership recorded to the bound chat, modes carried from
the binding). Linux `systemd --user` only.

The unit name hash comes from chat + schedule + text, so re-creating the same
scheduled prompt replaces the same unit pair instead of accumulating (stable
identity, contract §3).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

from kite.platform_paths import (
    default_data_root,
    default_systemd_user_dir,
    default_user_bin_dir,
)

UNIT_PREFIX = "kite-schedule"
DISPLAY_MODES = ("silent", "announce")

_HASH_LENGTH = 12
_UNIT_SCALAR_FORBIDDEN = {"\x00", "\r", "\n"}
_NAME_RE = re.compile(rf"^{UNIT_PREFIX}-[0-9a-f]{{{_HASH_LENGTH}}}$")

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


class ScheduleSystemctlError(RuntimeError):
    """A `systemctl --user` call failed (CLI exit 1)."""


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Everything rendered into one timer/service unit pair."""

    name: str
    chat_id: str
    text: str
    on_calendar: str
    recurring: bool
    display: str
    ctl_path: str

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
    """Both unit files of one schedule, for `kitectl schedule show`."""

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
    for suffix in (".timer", ".service"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    if not value.startswith(f"{UNIT_PREFIX}-"):
        value = f"{UNIT_PREFIX}-{value}"
    if not _NAME_RE.fullmatch(value):
        raise ScheduleError(
            f"invalid schedule name: {raw!r} (expected {UNIT_PREFIX}-<hash>)"
        )
    return value


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
    """Unit files are line-based: every rendered scalar must be one line."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ScheduleError(f"{field} must not be empty")
    if any(char in normalized for char in _UNIT_SCALAR_FORBIDDEN):
        raise ScheduleError(f"{field} must be a single line (no newline/NUL characters)")
    return normalized


def parse_at_on_calendar(raw: str, *, now: datetime | None = None) -> str:
    """`--at` → a one-shot OnCalendar timestamp; a past time is rejected
    before anything is written (fail-closed, contract §3)."""
    value = _unit_scalar(raw, "--at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError(f"--at must be an ISO 8601 timestamp, got: {raw!r}") from None
    if parsed.tzinfo is not None:
        # OnCalendar has no timezone component: systemd evaluates in local
        # time, so convert an offset-aware timestamp to local wall time.
        parsed = parsed.astimezone().replace(tzinfo=None)
    reference = now if now is not None else datetime.now()
    if parsed <= reference.replace(tzinfo=None):
        raise ScheduleError(f"--at is in the past: {parsed:%Y-%m-%d %H:%M:%S}")
    return f"{parsed:%Y-%m-%d %H:%M:%S}"


def parse_cron_on_calendar(raw: str) -> str:
    """`--cron` → a recurring OnCalendar expression.

    Accepted (contract §3): systemd's OnCalendar shorthand forms (daily,
    hourly, ...) passed through verbatim, and standard 5-field cron converted
    to OnCalendar. Everything else is rejected before anything is written.
    """
    value = _unit_scalar(raw, "--cron")
    lowered = value.lower()
    if lowered in _SYSTEMD_SHORTHANDS:
        return lowered
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
        # Vixie cron ORs a restricted dom with a restricted dow; systemd
        # OnCalendar ANDs them. Refuse the ambiguity instead of silently
        # changing the schedule's meaning.
        raise ScheduleError(
            "--cron restricts both day-of-month and day-of-week: cron ORs them "
            "but systemd OnCalendar ANDs them; split this into two schedules"
        )
    dow_prefix = ""
    if dows is not None:
        normalized_dows = sorted({0 if day == 7 else day for day in dows})
        dow_prefix = ",".join(_DOW_LABELS[day] for day in normalized_dows) + " "
    date_part = f"*-{_render_ints(months)}-{_render_ints(doms)}"
    time_part = f"{_render_ints(hours)}:{_render_ints(minutes)}:00"
    return f"{dow_prefix}{date_part} {time_part}"


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
# kitectl path resolution (stored in the service unit; contract §3)
# ---------------------------------------------------------------------------


def _is_executable_file(path: pathlib.Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_ctl_path(explicit: str = "") -> str:
    """Contract §3 order: explicit `--ctl-path` > `KITE_BIN_DIR/kitectl` (or
    `~/.local/bin/kitectl`) > `<data root>/.venv/bin/kitectl`. The resolved
    absolute path is stored in the unit — a timer has no PATH to search."""
    if str(explicit or "").strip():
        candidate = pathlib.Path(explicit).expanduser()
        if not _is_executable_file(candidate):
            raise ScheduleError(f"--ctl-path is not an executable file: {candidate}")
        return str(candidate.resolve())
    candidates = (
        default_user_bin_dir() / "kitectl",
        default_data_root() / ".venv" / "bin" / "kitectl",
    )
    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate.resolve())
    raise ScheduleError(
        "kitectl not found; checked "
        + " and ".join(str(candidate) for candidate in candidates)
        + "; pass --ctl-path explicitly"
    )


# ---------------------------------------------------------------------------
# Unit rendering
# ---------------------------------------------------------------------------


def _quote_unit_arg(arg: str) -> str:
    """systemd ExecStart quoting (same convention as kite.service_manager)."""
    escaped = str(arg).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_service_unit(spec: ScheduleSpec) -> str:
    description = _unit_scalar(
        f"KITE scheduled prompt (chat {spec.chat_id})", "description"
    )
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
    )
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
# systemctl --user driver
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


# ---------------------------------------------------------------------------
# create / list / show / remove / run-now
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
    name = schedule_name(
        _unit_scalar(chat_id, "chat_id"),
        _unit_scalar(on_calendar, "on_calendar"),
        _unit_scalar(text, "text"),
    )
    if display not in DISPLAY_MODES:
        raise ScheduleError(f"display must be one of {list(DISPLAY_MODES)}")
    spec = ScheduleSpec(
        name=name,
        chat_id=chat_id,
        text=text,
        on_calendar=on_calendar,
        recurring=recurring,
        display=display,
        ctl_path=_unit_scalar(ctl_path, "ctl_path"),
    )
    unit_dir = default_systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service_unit_path(name).write_text(render_service_unit(spec), encoding="utf-8")
    timer_unit_path(name).write_text(render_timer_unit(spec), encoding="utf-8")
    _run_systemctl("daemon-reload")
    _run_systemctl("enable", "--now", spec.timer_unit_name)
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
