"""
kitectl — the KITE local admin CLI.

Implemented slice (docs/contracts/mvp-scope.md §2 kitectl row, §6):
  - `kitectl config show`     — the effective config with secrets redacted
  - `kitectl service install|uninstall|start|stop|restart|status|log`
                              — the single-instance OS service over
                                kite.service_manager; install only writes the
                                definition, it does not start it (design §9)
  - `kitectl binding list`    — chat ↔ session bindings from the local store
  - `kitectl session list`    — sessions visible on kap-server
  - `kitectl session status`  — binding mapping, work state, queue depth,
                                WS connection age, last resync time
  - `kitectl prompt send`     — submit a prompt to a bound chat's session (or
                                a session id directly); the control-plane entry
                                for later scheduled capabilities. Permission
                                mode and plan mode are always carried
                                explicitly (kite-design.md §7).

Server address/token resolution: the `kap:` section of system.yaml, with the
instance registry as the source of the actual port (the server may have
bumped the requested port on conflict) and `<kap home>/server.token` as the
credential. kap wire schemas never appear here — only the adapter's
normalized types are consumed.
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
from kite import service_manager
from kite.adapters import kap_server
from kite.adapters.kap_server import KapRestClient
from kite.cli_table import render_table
from kite.platform_paths import default_data_root
from kite.process_utils import process_exists
from kite.runtime_status import read_runtime_status
from kite.stores.binding_store import (
    DEFAULT_PERMISSION_MODE,
    DEFAULT_PLAN_MODE,
    BindingStore,
)

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
    sessions = _checked(client.list_sessions)
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


def _cmd_service_stop(_args: argparse.Namespace) -> int:
    return _run_service_action(
        lambda manager, definition: manager.stop(definition), done="stopped"
    )


def _cmd_service_restart(_args: argparse.Namespace) -> int:
    return _run_service_action(
        lambda manager, definition: manager.restart(definition), done="restarted"
    )


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


def _prompt_model() -> str | None:
    """Model carried per prompt: kap.model, else config.toml default_model."""
    config = kite_config.load_config_file("system")
    kap = kite_config.kap_settings(config)
    home = kap_server.resolve_kap_home(kap.home)
    return kap_server.resolve_prompt_model(kap.model, home)


def _cmd_prompt_send(args: argparse.Namespace) -> int:
    text = args.text
    if not text.strip():
        _die("prompt text must not be empty")
    if args.chat is not None:
        binding = BindingStore(default_data_root()).load(args.chat)
        if binding is None:
            _die(f"no binding for chat {args.chat}; bind the chat from Feishu first")
        session_id = binding["session_id"]
        permission_mode = binding["permission_mode"]
        plan_mode = binding["plan_mode"]
    else:
        # No binding to inherit from: the store defaults, stated in --help.
        session_id = args.session
        permission_mode = DEFAULT_PERMISSION_MODE
        plan_mode = DEFAULT_PLAN_MODE

    client = _connect()
    # Lazy import: app_handler pulls in the Feishu transport stack, which this
    # local control-plane path does not otherwise need. KapSessionOps stays
    # the single application-side owner of the submit wire path/payload.
    from kite.app_handler import KapSessionOps

    result = _checked(
        lambda: KapSessionOps(client, model=_prompt_model()).submit_prompt(
            session_id,
            text,
            permission_mode=permission_mode,
            plan_mode=plan_mode,
        )
    )
    print(f"prompt_id: {result.prompt_id}")
    print(f"session_id: {session_id}")
    print(f"status: {result.status}")
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
    service_sub.add_parser("stop", help="stop the service").set_defaults(
        func=_cmd_service_stop
    )
    service_sub.add_parser("restart", help="restart the service").set_defaults(
        func=_cmd_service_restart
    )
    service_sub.add_parser("status", help="show installed/running state").set_defaults(
        func=_cmd_service_status
    )
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
    send_parser.set_defaults(func=_cmd_prompt_send)
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
