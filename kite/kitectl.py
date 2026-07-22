"""
kitectl — the KITE local admin CLI.

Implemented slice (docs/contracts/mvp-scope.md §6):
  - `kitectl session list`    — sessions visible on kap-server
  - `kitectl session status`  — binding mapping, work state, queue depth,
                                WS connection age, last resync time

Server address/token resolution: the `kap:` section of system.yaml, with the
instance registry as the source of the actual port (the server may have
bumped the requested port on conflict) and `<kap home>/server.token` as the
credential. kap wire schemas never appear here — only the adapter's
normalized types are consumed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, NoReturn

from kite import config as kite_config
from kite.adapters import kap_server
from kite.adapters.kap_server import KapRestClient
from kite.cli_table import render_table
from kite.platform_paths import default_data_root
from kite.process_utils import process_exists
from kite.runtime_status import read_runtime_status
from kite.stores.binding_store import BindingStore

_NO_VALUE = "-"


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

    session_parser = subparsers.add_parser("session", help="session inspection")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("list", help="list sessions on kap-server").set_defaults(
        func=_cmd_session_list
    )
    session_sub.add_parser(
        "status", help="bindings, work state, queue depth, WS/resync observability"
    ).set_defaults(func=_cmd_session_status)
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
