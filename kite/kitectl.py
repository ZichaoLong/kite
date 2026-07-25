"""
kitectl — the KITE local admin CLI.

Implemented slice (docs/contracts/mvp-scope.md §2 kitectl row, §6):
  - `kitectl config show`     — the effective config with secrets redacted
  - `kitectl service install|uninstall|start|stop|restart|status|log`
                              — the single-instance OS service over
                                kite.service_manager; install only writes the
                                definition, it does not start it (design §9).
                                stop/restart run a destructive-op preview gate:
                                busy sessions or pending interactions (or an
                                unverifiable live state) require --force
  - `kitectl binding list`    — chat ↔ session bindings from the local store
  - `kitectl session list`    — sessions visible on kap-server
  - `kitectl session status`  — binding mapping, work state, queue depth,
                                WS connection age, last resync time
  - `kitectl prompt send`     — submit a prompt to a bound chat's session (or
                                a session id directly); the control-plane entry
                                for later scheduled capabilities. The submit
                                goes through kited's loopback control plane
                                (docs/decisions/control-plane.md), so ownership
                                is recorded exactly as for a Feishu-originated
                                prompt. Exit codes: 2 daemon down / setup, 3
                                outcome unknown (may be delivered — verify
                                before retrying), 1 business error.
  - `kitectl image send`      — upload a local image once and send it to every
                                attached chat bound to the same session as
                                --chat, through the same control plane
                                (docs/contracts/images.md §3). Same exit-code
                                taxonomy; a partial fan-out failure exits 1
                                with the per-chat report.
  - `kitectl schedule create|list|show|remove|run-now`
                              — scheduled prompts (docs/contracts/scheduled-prompts.md):
                                OS timers (Linux systemd --user, macOS launchd,
                                Windows Task Scheduler) that fire
                                `kitectl prompt send` back into the daemon.
                                Validation is fail-closed before anything is
                                written (past --at, unparseable --cron,
                                unknown chat); remove requires --yes.

Read-only commands talk to kap-server REST directly: the `kap:` section of
system.yaml provides address/home, the instance registry is the source of
the actual port (the server may have bumped the requested port on conflict),
and `<kap home>/server.token` is the credential. kap wire schemas never
appear here — only the adapter's normalized types are consumed. The control
plane is discovered from `control_plane.json` in the data dir (pid liveness
checked) and authenticated with `control.token` from the config dir.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time
from typing import Any, Callable, NoReturn

import yaml

from kite import config as kite_config
from kite import preflights
from kite import schedule_units
from kite import service_manager
from kite.adapters import kap_server
from kite.adapters.kap_server import KapRestClient
from kite.cli_table import render_table
from kite.control_plane import (
    ControlClient,
    ControlError,
    ControlOutcomeUnknownError,
    ControlRefusedError,
    discover_live_control_metadata,
)
from kite.platform_paths import default_data_root
from kite.process_utils import process_exists
from kite.runtime_status import read_runtime_status
from kite.stores.binding_store import BindingStore

_NO_VALUE = "-"
_DEFAULT_LOG_LINES = 50

_SECRET_KEY_PATTERN = re.compile(r"secret|token|password|api[_-]?key", re.IGNORECASE)
_REDACTED_VALUE = "********"


class CliError(Exception):
    """A user-facing fatal error (printed without a traceback)."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _die(message: str, *, exit_code: int = 2) -> NoReturn:
    raise CliError(message, exit_code=exit_code)


def _connect() -> KapRestClient:
    """Build a REST client for the running kap-server (fail-closed)."""
    config = kite_config.load_config_file("system")
    kap = kite_config.kap_settings(config)
    home = kap_server.resolve_kap_home(kap.home)
    try:
        token = kap_server.read_server_token(home)
    except OSError:
        _die(
            f"no kap-server token at {kap_server.server_token_path(home)}; "
            "is kited running?"
        )
    live = kap_server.find_live_server(home)
    port = live.port if live is not None else (kap.port or kap_server.DEFAULT_KAP_PORT)
    return KapRestClient(kap.host, port, token)


def _checked(call: Any) -> Any:
    """Run a REST call, mapping failures onto fail-closed CLI errors."""
    try:
        return call()
    except kap_server.KapTransportError as exc:
        _die(f"cannot reach kap-server: {exc}")
    except kap_server.KapError as exc:
        _die(f"kap-server error {exc.code}: {exc.msg}", exit_code=1)


def _format_age(started_at: Any, *, now: float) -> str:
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return _NO_VALUE
    seconds = max(int(now - started_at), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _print_lines(lines: list[str]) -> None:
    for line in lines:
        print(line)


def _cmd_session_list(_args: argparse.Namespace) -> int:
    client = _connect()
    sessions = sorted(
        _checked(client.list_sessions), key=lambda s: s.updated_at, reverse=True
    )
    rows = [
        [
            session.session_id,
            session.title or _NO_VALUE,
            session.cwd or _NO_VALUE,
            "yes" if session.busy else "no",
        ]
        for session in sessions
    ]
    if not rows:
        print("(no sessions)")
        return 0
    _print_lines(render_table(["SESSION_ID", "TITLE", "CWD", "BUSY"], rows))
    return 0


def _cmd_session_status(_args: argparse.Namespace) -> int:
    client = _connect()
    data_dir = default_data_root()
    bindings = BindingStore(data_dir).load_all()
    sessions = {s.session_id: s for s in _checked(client.list_sessions)}

    print("Bindings:")
    if bindings:
        rows = [
            [
                chat_id,
                binding["session_id"],
                "yes" if binding["attached"] else "no",
                binding["permission_mode"],
                "on" if binding["plan_mode"] else "off",
            ]
            for chat_id, binding in sorted(bindings.items())
        ]
        _print_lines(render_table(["CHAT_ID", "SESSION_ID", "ATTACHED", "MODE", "PLAN"], rows))
    else:
        print("  (no bindings)")

    print("Sessions:")
    bound_ids = sorted({binding["session_id"] for binding in bindings.values()})
    rows = []
    for session_id in bound_ids:
        summary = sessions.get(session_id)
        queue = _checked(lambda: client.get_prompts(session_id))
        rows.append(
            [
                session_id,
                (summary.title if summary else None) or _NO_VALUE,
                ("yes" if summary.busy else "no") if summary else "unknown",
                str(queue.queue_depth),
                queue.active_prompt_id or _NO_VALUE,
                (summary.pending_interaction if summary else None) or _NO_VALUE,
            ]
        )
    if rows:
        _print_lines(
            render_table(
                ["SESSION_ID", "TITLE", "BUSY", "QUEUE", "ACTIVE_PROMPT", "PENDING"], rows
            )
        )
    else:
        print("  (no bound sessions)")

    print("Daemon:")
    now = time.time()
    status = read_runtime_status(data_dir)
    kited_pid = status.get("kited_pid") if status else None
    kited_alive = isinstance(kited_pid, int) and process_exists(kited_pid)
    if not status or not kited_alive:
        print("  kited: not running (no live runtime status)")
    else:
        print(f"  kited: running (pid {kited_pid})")
        kap_info = status.get("kap") if isinstance(status.get("kap"), dict) else {}
        kap_pid = kap_info.get("pid")
        kap_port = kap_info.get("port")
        print(f"  kap-server: pid {kap_pid or _NO_VALUE} port {kap_port or _NO_VALUE}")
        ws_info = status.get("ws") if isinstance(status.get("ws"), dict) else {}
        connected_at = ws_info.get("connected_at")
        if isinstance(connected_at, (int, float)):
            print(f"  WS: connected (age {_format_age(connected_at, now=now)})")
        else:
            print("  WS: not connected")
        last_resync = ws_info.get("last_resync_at")
        if isinstance(last_resync, (int, float)):
            print(f"  last resync: {_format_age(last_resync, now=now)} ago")
        else:
            print("  last resync: never")
    return 0


def _redact_secrets(value: Any) -> Any:
    """Mask secret-looking keys (app_secret, tokens, api keys) recursively."""
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED_VALUE
                if _SECRET_KEY_PATTERN.search(str(key))
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _cmd_config_show(_args: argparse.Namespace) -> int:
    config = kite_config.load_config_file("system")
    print(f"# {kite_config.system_config_path()} (secrets redacted)")
    if not config:
        print("(no config file)")
        return 0
    print(yaml.safe_dump(_redact_secrets(config), sort_keys=True, allow_unicode=True), end="")
    return 0


def _service_definition() -> service_manager.ServiceDefinition:
    """The single-instance service definition, honoring --config-dir/--data-dir."""
    return service_manager.build_service_definition(
        config_dir=kite_config.config_dir(),
        data_dir=default_data_root(),
    )


def _service_manager() -> service_manager.ServiceManager:
    try:
        return service_manager.current_service_manager()
    except service_manager.ServiceManagerError as exc:
        _die(str(exc), exit_code=1)


def _run_service_action(
    action: Callable[
        [service_manager.ServiceManager, service_manager.ServiceDefinition], None
    ],
    *,
    done: str,
) -> int:
    """Run one ServiceManager mutation, mapping failures onto CLI errors."""
    manager = _service_manager()
    definition = _service_definition()
    try:
        action(manager, definition)
    except service_manager.ServiceManagerError as exc:
        _die(str(exc), exit_code=1)
    print(f"service '{manager.display_name(definition)}' {done}")
    return 0


def _cmd_service_install(_args: argparse.Namespace) -> int:
    # Writes the definition only; starting is an explicit separate step
    # (docs/architecture/kite-design.md §9).
    return _run_service_action(
        lambda manager, definition: manager.ensure_service(definition),
        done="installed (definition written, not started)",
    )


def _cmd_service_uninstall(_args: argparse.Namespace) -> int:
    return _run_service_action(
        lambda manager, definition: manager.uninstall(definition), done="uninstalled"
    )


def _cmd_service_start(_args: argparse.Namespace) -> int:
    return _run_service_action(
        lambda manager, definition: manager.start(definition), done="started"
    )


def _cmd_service_stop(args: argparse.Namespace) -> int:
    _service_in_flight_gate(args, verb="stopping")
    return _run_service_action(
        lambda manager, definition: manager.stop(definition), done="stopped"
    )


def _cmd_service_restart(args: argparse.Namespace) -> int:
    _service_in_flight_gate(args, verb="restarting")
    return _run_service_action(
        lambda manager, definition: manager.restart(definition), done="restarted"
    )


def _service_in_flight_gate(args: argparse.Namespace, *, verb: str) -> None:
    """Destructive-op preview gate for service stop/restart.

    Queries live state (runtime_status.json + kap sessions busy/pending via
    REST): any busy session or pending interaction makes the operation
    --force-only with a printed preview; an unverifiable live state (kap
    unreachable) is also --force-only — never silently available (FOCUS
    runtime_admin discipline, docs/research/focus-assets-map.md §0 item 4).
    """
    sessions: list[kap_server.SessionSummary] | None = None
    verified_pending: int | None = None
    try:
        client = _connect()
        sessions = client.list_sessions()
        # Stale-flag correction: the session-level pending_interaction flag
        # can linger after the approval itself expired upstream. Verify
        # flagged sessions against the real pending lists; a per-session
        # fetch failure counts that session as pending (conservative).
        flags = [s for s in sessions if s.pending_interaction]
        if flags:
            verified_pending = 0
            for summary in flags:
                try:
                    approvals, questions = _pending_interactions(client, summary.session_id)
                    verified_pending += len(approvals) + len(questions)
                except (kap_server.KapError, kap_server.KapTransportError):
                    verified_pending += 1
    except (CliError, kap_server.KapError, kap_server.KapTransportError):
        sessions = None
        verified_pending = None
    status = read_runtime_status(default_data_root())
    kited_pid = status.get("kited_pid") if status else None
    preview = preflights.preview_service_stop(
        sessions,
        kited_running=isinstance(kited_pid, int) and process_exists(kited_pid),
        verified_pending=verified_pending,
    )
    if not preview.force_only:
        return
    text = preview.preview_text(verb)
    if not getattr(args, "force", False):
        _die(f"{text}\nre-run with --force to proceed")
    print(f"warning: {text}; proceeding anyway (--force)")


def _cmd_service_status(_args: argparse.Namespace) -> int:
    manager = _service_manager()
    definition = _service_definition()
    try:
        status = manager.status(definition)
    except service_manager.ServiceManagerError as exc:
        _die(str(exc), exit_code=1)
    print(f"service: {manager.display_name(definition)}")
    print(f"installed: {'yes' if status.installed else 'no'}")
    print(f"running: {'yes' if status.running else 'no'}")
    if status.source:
        print(f"source: {status.source}")
    if status.detail:
        print(f"detail: {status.detail}")
    return 0


def _cmd_service_autostart(args: argparse.Namespace) -> int:
    manager = _service_manager()
    definition = _service_definition()
    try:
        if args.autostart_command == "enable":
            manager.autostart_enable(definition)
            print(f"service '{manager.display_name(definition)}' autostart enabled")
            return 0
        if args.autostart_command == "disable":
            manager.autostart_disable(definition)
            print(f"service '{manager.display_name(definition)}' autostart disabled")
            return 0
        status = manager.autostart_status(definition)
    except service_manager.ServiceManagerError as exc:
        _die(str(exc), exit_code=1)
    print(f"service: {manager.display_name(definition)}")
    print(f"autostart: {'enabled' if status.enabled else 'disabled'}")
    if status.source:
        print(f"source: {status.source}")
    if status.detail:
        print(f"detail: {status.detail}")
    return 0


def _tail_lines(path: pathlib.Path, count: int, *, block_size: int = 8192) -> list[str]:
    """The last `count` lines of a text file, read back-to-front in blocks."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        offset = handle.tell()
        buffer = b""
        while offset > 0 and buffer.count(b"\n") <= count:
            step = min(block_size, offset)
            offset -= step
            handle.seek(offset)
            buffer = handle.read(step) + buffer
    return buffer.decode(errors="replace").splitlines()[-count:]


def _cmd_service_log(args: argparse.Namespace) -> int:
    lines = args.lines
    if lines <= 0:
        _die("log line count must be a positive integer")
    path = _service_definition().stdout_log_path
    try:
        tailed = _tail_lines(path, lines)
    except OSError:
        _die(f"no service stdout log at {path}; is the service installed and running?")
    _print_lines(tailed)
    return 0


def _cmd_binding_list(_args: argparse.Namespace) -> int:
    bindings = BindingStore(default_data_root()).load_all()
    if not bindings:
        print("(no bindings)")
        return 0
    rows = [
        [
            chat_id,
            binding["session_id"],
            "yes" if binding["attached"] else "no",
            binding["permission_mode"],
            "on" if binding["plan_mode"] else "off",
        ]
        for chat_id, binding in sorted(bindings.items())
    ]
    _print_lines(render_table(["CHAT_ID", "SESSION_ID", "ATTACHED", "MODE", "PLAN"], rows))
    return 0


def _control_client() -> ControlClient:
    """Discover the live daemon's control plane and build an authenticated client."""
    metadata = discover_live_control_metadata(default_data_root())
    if metadata is None:
        _die("kited is not running (no live control plane); start it with `kitectl service start`")
    token_path = kite_config.control_token_path()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        _die(f"no control-plane token at {token_path}; is kited running?")
    return ControlClient(port=metadata.port, token=token)


def _cmd_prompt_send(args: argparse.Namespace) -> int:
    text = args.text
    if not text.strip():
        _die("prompt text must not be empty")
    # kitectl is a client of the daemon here (docs/decisions/control-plane.md):
    # the submit must serialize through kited so prompt ownership is recorded
    # in the daemon's map. Never fall back to direct kap REST — that would
    # reintroduce the second writer on the ownership axis.
    params: dict[str, Any] = {"text": text}
    if args.chat is not None:
        params["chat_id"] = args.chat
    else:
        params["session_id"] = args.session
    if getattr(args, "display", None) is not None:
        params["display"] = args.display
    client = _control_client()
    try:
        data = client.request("prompt/submit", params)
    except ControlRefusedError:
        # The daemon went away between discovery and connect; the submit was
        # definitely not delivered.
        _die("kited is not running (control plane refused the connection); the prompt was not submitted")
    except ControlOutcomeUnknownError as exc:
        _die(
            f"{exc}\nthe prompt may have been delivered; "
            "verify with `kitectl session status` before retrying",
            exit_code=3,
        )
    except ControlError as exc:
        _die(f"kited error {exc.code}: {exc.msg}", exit_code=1)
    if not isinstance(data, dict):
        _die(
            "kited accepted the prompt but returned a malformed response; "
            "verify with `kitectl session status`",
            exit_code=3,
        )
    print(f"prompt_id: {data.get('prompt_id', '')}")
    print(f"session_id: {data.get('session_id', '')}")
    print(f"status: {data.get('status', '')}")
    print(f"owner_recorded: {'yes' if data.get('owner_recorded') else 'no'}")
    return 0


def _cmd_image_send(args: argparse.Namespace) -> int:
    raw_path = str(args.path or "").strip()
    if not raw_path:
        _die("image path must not be empty")
    # Absolutize client-side: the daemon's cwd differs from kitectl's, so a
    # relative path would resolve against the wrong directory. Existence and
    # the byte cap are validated daemon-side (one authoritative check).
    path = str(pathlib.Path(raw_path).expanduser().resolve())
    client = _control_client()
    try:
        data = client.request("image/send", {"chat_id": args.chat, "path": path})
    except ControlRefusedError:
        # The daemon went away between discovery and connect; nothing was sent.
        _die("kited is not running (control plane refused the connection); the image was not sent")
    except ControlOutcomeUnknownError as exc:
        _die(
            f"{exc}\nthe image may have been delivered; "
            "check the target chats before retrying",
            exit_code=3,
        )
    except ControlError as exc:
        _die(f"kited error {exc.code}: {exc.msg}", exit_code=1)
    if not isinstance(data, dict):
        _die(
            "kited accepted the image but returned a malformed response; "
            "check the target chats before retrying",
            exit_code=3,
        )
    delivered = data.get("delivered") if isinstance(data.get("delivered"), list) else []
    failed = data.get("failed") if isinstance(data.get("failed"), list) else []
    print(f"session_id: {data.get('session_id', '')}")
    print(f"image_key: {data.get('image_key', '')}")
    if delivered:
        print(
            "delivered: "
            + ", ".join(
                f"{item.get('chat_id', '')} ({item.get('message_id', '')})"
                for item in delivered
                if isinstance(item, dict)
            )
        )
    else:
        print("delivered: (none)")
    if failed:
        print(
            "failed: "
            + ", ".join(
                f"{item.get('chat_id', '')} ({item.get('error', '')})"
                for item in failed
                if isinstance(item, dict)
            )
        )
    # Per-chat failures are data in the report (contract §3.1); a partial
    # delivery still exits non-zero so scripts notice.
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# schedule — OS timer backends (docs/contracts/scheduled-prompts.md §2):
# Linux systemd --user timers, macOS launchd, Windows Task Scheduler
# ---------------------------------------------------------------------------


def _schedule_backend() -> schedule_units.ScheduleBackend:
    try:
        return schedule_units.current_schedule_backend()
    except schedule_units.ScheduleError as exc:
        _die(str(exc))


def _schedule_error(exc: Exception) -> NoReturn:
    if isinstance(exc, schedule_units.ScheduleBackendError):
        _die(str(exc), exit_code=1)
    _die(str(exc))


def _cmd_schedule_create(args: argparse.Namespace) -> int:
    backend = _schedule_backend()
    text = str(args.text or "").strip()
    if not text:
        _die("prompt text must not be empty")
    chat_id = str(args.chat or "").strip()
    # Contract §3/§4.3: the target chat must already be bound — reject before
    # writing anything (at fire time the control plane's no_binding error is
    # the fail-closed outcome; creating anyway would build a dead timer).
    if BindingStore(default_data_root()).load(chat_id) is None:
        _die(f"no binding for chat {chat_id}; bind the chat from Feishu first")
    try:
        if args.at is not None:
            plan = schedule_units.parse_at_schedule(args.at)
        else:
            plan = schedule_units.parse_cron_schedule(args.cron)
        ctl_path = schedule_units.resolve_ctl_path(args.ctl_path)
        spec = schedule_units.build_schedule_spec(
            chat_id=chat_id,
            text=text,
            plan=plan,
            display=args.display,
            ctl_path=ctl_path,
        )
        artifacts = backend.install(spec)
    except (schedule_units.ScheduleError, schedule_units.ScheduleBackendError) as exc:
        _schedule_error(exc)
    print(f"name: {spec.name}")
    print(f"chat_id: {spec.chat_id}")
    print(f"on_calendar: {spec.on_calendar}")
    print(f"display: {spec.display}")
    print(f"ctl_path: {spec.ctl_path}")
    for label, path in artifacts:
        print(f"{label}: {path}")
    if plan.recurring:
        # Contract §4.4: a recurring timer has no natural end and kitectl
        # cannot tell whether the prompt carries a termination strategy, so
        # every --cron create warns.
        print(
            "warning: recurring schedule without a verified termination strategy; "
            "the OS timer keeps firing until the schedule is removed — give the "
            f"prompt a self-removal condition (`kitectl schedule remove {spec.name} --yes`) "
            "or a one-shot cleanup prompt (docs/contracts/scheduled-prompts.md §4.4)",
            file=sys.stderr,
        )
    return 0


def _cmd_schedule_list(_args: argparse.Namespace) -> int:
    backend = _schedule_backend()
    try:
        entries = backend.list()
    except (schedule_units.ScheduleError, schedule_units.ScheduleBackendError) as exc:
        _schedule_error(exc)
    if not entries:
        print("(no schedules)")
        return 0
    rows = [[entry.name, entry.on_calendar, entry.next_elapse] for entry in entries]
    _print_lines(render_table(["NAME", "ON_CALENDAR", "NEXT"], rows))
    return 0


def _cmd_schedule_show(args: argparse.Namespace) -> int:
    backend = _schedule_backend()
    try:
        files = backend.show(args.name)
    except schedule_units.ScheduleError as exc:
        _schedule_error(exc)
    for path, text in files:
        print(f"# {path}")
        print(text, end="")
    return 0


def _cmd_schedule_remove(args: argparse.Namespace) -> int:
    backend = _schedule_backend()
    try:
        base = backend.resolve_name(args.name)
    except schedule_units.ScheduleError as exc:
        _schedule_error(exc)
    if not args.yes:
        _die(f"re-run with --yes to disable and delete schedule '{base}'")
    try:
        backend.remove(base)
    except (schedule_units.ScheduleError, schedule_units.ScheduleBackendError) as exc:
        _schedule_error(exc)
    print(f"schedule '{base}' removed")
    return 0


def _cmd_schedule_run_now(args: argparse.Namespace) -> int:
    backend = _schedule_backend()
    try:
        base = backend.run_now(args.name)
    except (schedule_units.ScheduleError, schedule_units.ScheduleBackendError) as exc:
        _schedule_error(exc)
    print(f"schedule '{base}' started")
    return 0


def _quote_path(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


# Upstream quirk: a successful question dismiss replies with code 40909
# (the *success* envelope, routes/questions.ts) — not an error.
_KAP_QUESTION_DISMISSED = 40909


def _pending_interactions(client: KapRestClient, session_id: str) -> tuple[list[str], list[str]]:
    """Pending approval/question ids of one session (empty lists when clean)."""
    approvals = client.get(f"/sessions/{_quote_path(session_id)}/approvals?status=pending")
    questions = client.get(f"/sessions/{_quote_path(session_id)}/questions?status=pending")
    approval_ids = [
        str(item.get("approval_id") or "")
        for item in (approvals.get("items") or [])
        if isinstance(item, dict) and str(item.get("approval_id") or "").strip()
    ]
    question_ids = [
        str(item.get("question_id") or "")
        for item in (questions.get("items") or [])
        if isinstance(item, dict) and str(item.get("question_id") or "").strip()
    ]
    return approval_ids, question_ids


def _cmd_interaction_sweep(args: argparse.Namespace) -> int:
    """Reject/dismiss stale pending approvals/questions upstream.

    These are UPSTREAM kap resources (not daemon-owned state), so this talks
    to kap REST directly, like the service gate. Dry-run by default;
    `--yes` performs the sweep.
    """
    client = _connect()
    if args.session:
        session_ids = [args.session]
    else:
        sessions = _checked(client.list_sessions)
        session_ids = [summary.session_id for summary in sessions]

    plan: list[tuple[str, list[str], list[str]]] = []
    for session_id in session_ids:
        try:
            approval_ids, question_ids = _pending_interactions(client, session_id)
        except kap_server.KapError as exc:
            if exc.code == 40401:
                print(f"{session_id}: session not found; skipped")
                continue
            _die(f"cannot list pending interactions for {session_id}: {exc.msg}", exit_code=1)
        if approval_ids or question_ids:
            plan.append((session_id, approval_ids, question_ids))

    if not plan:
        print("(no pending interactions)")
        return 0
    for session_id, approval_ids, question_ids in plan:
        print(f"{session_id}: {len(approval_ids)} approval(s), {len(question_ids)} question(s) pending")
    if not args.yes:
        print("dry-run: re-run with --yes to reject the approvals and dismiss the questions")
        return 0

    skipped = 0
    for session_id, approval_ids, question_ids in plan:
        for approval_id in approval_ids:
            try:
                client.call(
                    "POST",
                    f"/sessions/{_quote_path(session_id)}/approvals/{_quote_path(approval_id)}",
                    {"decision": "rejected"},
                )
            except kap_server.KapError as exc:
                skipped += 1
                print(f"  {session_id} approval {approval_id}: {exc.code} {exc.msg} (skipped)")
        for question_id in question_ids:
            try:
                client.call(
                    "POST",
                    f"/sessions/{_quote_path(session_id)}/questions/{_quote_path(question_id)}:dismiss",
                )
            except kap_server.KapError as exc:
                if exc.code == _KAP_QUESTION_DISMISSED:
                    continue  # the dismiss success envelope (upstream quirk)
                skipped += 1
                print(f"  {session_id} question {question_id}: {exc.code} {exc.msg} (skipped)")
        print(f"{session_id}: swept {len(approval_ids)} approval(s), {len(question_ids)} question(s)")
    if skipped:
        print(f"note: {skipped} item(s) were already resolved upstream (skipped)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kitectl", description="KITE local admin CLI.")
    parser.add_argument(
        "--config-dir",
        help="instance config directory (default: KITE_CONFIG_DIR or platform default)",
    )
    parser.add_argument(
        "--data-dir",
        help="instance data directory (default: KITE_DATA_ROOT or platform default)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="instance config inspection")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser(
        "show", help="show the effective config with secrets redacted"
    ).set_defaults(func=_cmd_config_show)

    service_parser = subparsers.add_parser("service", help="OS service management")
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser(
        "install", help="write the service definition (does not start it)"
    ).set_defaults(func=_cmd_service_install)
    service_sub.add_parser("uninstall", help="remove the service definition").set_defaults(
        func=_cmd_service_uninstall
    )
    service_sub.add_parser("start", help="start the service").set_defaults(
        func=_cmd_service_start
    )
    stop_parser = service_sub.add_parser("stop", help="stop the service")
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="stop even when sessions are busy or the live state is unverifiable",
    )
    stop_parser.set_defaults(func=_cmd_service_stop)
    restart_parser = service_sub.add_parser("restart", help="restart the service")
    restart_parser.add_argument(
        "--force",
        action="store_true",
        help="restart even when sessions are busy or the live state is unverifiable",
    )
    restart_parser.set_defaults(func=_cmd_service_restart)
    service_sub.add_parser("status", help="show installed/running state").set_defaults(
        func=_cmd_service_status
    )
    autostart_parser = service_sub.add_parser(
        "autostart", help="manage start-on-login"
    )
    autostart_sub = autostart_parser.add_subparsers(
        dest="autostart_command", required=True
    )
    for _name in ("enable", "disable", "status"):
        autostart_sub.add_parser(_name).set_defaults(func=_cmd_service_autostart)
    log_parser = service_sub.add_parser("log", help="tail the daemon stdout log")
    log_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=_DEFAULT_LOG_LINES,
        help=f"number of lines to show (default: {_DEFAULT_LOG_LINES})",
    )
    log_parser.set_defaults(func=_cmd_service_log)

    binding_parser = subparsers.add_parser("binding", help="chat binding inspection")
    binding_sub = binding_parser.add_subparsers(dest="binding_command", required=True)
    binding_sub.add_parser("list", help="list chat ↔ session bindings").set_defaults(
        func=_cmd_binding_list
    )

    session_parser = subparsers.add_parser("session", help="session inspection")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("list", help="list sessions on kap-server").set_defaults(
        func=_cmd_session_list
    )
    session_sub.add_parser(
        "status", help="bindings, work state, queue depth, WS/resync observability"
    ).set_defaults(func=_cmd_session_status)

    prompt_parser = subparsers.add_parser("prompt", help="prompt submission (control plane)")
    prompt_sub = prompt_parser.add_subparsers(dest="prompt_command", required=True)
    send_parser = prompt_sub.add_parser(
        "send", help="submit a prompt to a bound chat's session (or a session directly)"
    )
    send_target = send_parser.add_mutually_exclusive_group(required=True)
    send_target.add_argument(
        "--chat",
        metavar="CHAT_ID",
        help="bound chat; the binding's permission_mode/plan_mode are carried explicitly",
    )
    send_target.add_argument(
        "--session",
        metavar="SESSION_ID",
        help="target session directly (permission_mode=auto, plan_mode=off)",
    )
    send_parser.add_argument("--text", required=True, help="prompt text")
    send_parser.add_argument(
        "--display",
        choices=schedule_units.DISPLAY_MODES,
        default=None,
        help="announce makes the daemon send a scheduled-trigger notice to the "
        "chat before submitting (default: silent)",
    )
    send_parser.set_defaults(func=_cmd_prompt_send)

    image_parser = subparsers.add_parser("image", help="image delivery (control plane)")
    image_sub = image_parser.add_subparsers(dest="image_command", required=True)
    image_send_parser = image_sub.add_parser(
        "send",
        help="upload a local image once and send it to every attached chat "
        "bound to the same session as --chat (docs/contracts/images.md §3)",
    )
    image_send_parser.add_argument(
        "--chat",
        required=True,
        metavar="CHAT_ID",
        help="bound chat; the image goes to every attached chat of its session",
    )
    image_send_parser.add_argument(
        "--path",
        required=True,
        help="local image file (the daemon validates existence and the byte cap)",
    )
    image_send_parser.set_defaults(func=_cmd_image_send)

    interaction_parser = subparsers.add_parser(
        "interaction", help="stale approval/question cleanup (upstream)"
    )
    interaction_sub = interaction_parser.add_subparsers(
        dest="interaction_command", required=True
    )
    sweep_parser = interaction_sub.add_parser(
        "sweep",
        help="reject stale pending approvals and dismiss stale pending questions "
        "(dry-run unless --yes is given)",
    )
    sweep_parser.add_argument(
        "--session",
        metavar="SESSION_ID",
        help="limit the sweep to one session (default: all visible sessions)",
    )
    sweep_parser.add_argument(
        "--yes",
        action="store_true",
        help="actually reject/dismiss; without it only the plan is printed",
    )
    sweep_parser.set_defaults(func=_cmd_interaction_sweep)

    schedule_parser = subparsers.add_parser(
        "schedule",
        help="scheduled prompts via OS timers (systemd --user / launchd / Task Scheduler)",
    )
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command", required=True)
    create_parser = schedule_sub.add_parser(
        "create", help="create a scheduled prompt (writes + enables the OS timer)"
    )
    create_parser.add_argument(
        "--chat",
        required=True,
        metavar="CHAT_ID",
        help="bound chat the prompt fires into (must have a binding)",
    )
    create_parser.add_argument(
        "--text", required=True, help="prompt text (single line)"
    )
    create_when = create_parser.add_mutually_exclusive_group(required=True)
    create_when.add_argument(
        "--at",
        metavar="ISO_TIMESTAMP",
        help="one-shot fire time (local wall time unless an offset is given)",
    )
    create_when.add_argument(
        "--cron",
        metavar="EXPR",
        help="recurring: a systemd OnCalendar shorthand (daily, hourly, ...) "
        "or a standard 5-field cron expression",
    )
    create_parser.add_argument(
        "--display",
        choices=schedule_units.DISPLAY_MODES,
        default="silent",
        help="announce sends a trigger notice to the chat before submitting "
        "(default: silent)",
    )
    create_parser.add_argument(
        "--ctl-path",
        default="",
        help="explicit kitectl path stored in the service unit "
        "(default: KITE_BIN_DIR or ~/.local/bin, then the managed venv)",
    )
    create_parser.set_defaults(func=_cmd_schedule_create)
    schedule_sub.add_parser(
        "list", help="list scheduled prompts with their next elapse"
    ).set_defaults(func=_cmd_schedule_list)
    show_parser = schedule_sub.add_parser(
        "show", help="print the stored timer definition(s) of one schedule"
    )
    show_parser.add_argument("name", help="schedule name (kite-schedule-<hash> or the hash)")
    show_parser.set_defaults(func=_cmd_schedule_show)
    remove_parser = schedule_sub.add_parser(
        "remove", help="disable + delete a schedule"
    )
    remove_parser.add_argument("name", help="schedule name (kite-schedule-<hash> or the hash)")
    remove_parser.add_argument(
        "--yes",
        action="store_true",
        help="actually disable and delete; without it the command refuses",
    )
    remove_parser.set_defaults(func=_cmd_schedule_remove)
    run_now_parser = schedule_sub.add_parser(
        "run-now", help="fire a schedule once immediately"
    )
    run_now_parser.add_argument("name", help="schedule name (kite-schedule-<hash> or the hash)")
    run_now_parser.set_defaults(func=_cmd_schedule_run_now)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.config_dir:
        os.environ["KITE_CONFIG_DIR"] = args.config_dir
    if args.data_dir:
        os.environ["KITE_DATA_ROOT"] = args.data_dir
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"kitectl: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except ValueError as exc:
        print(f"kitectl: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
