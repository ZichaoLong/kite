"""
kited — the KITE daemon entrypoint.

kited is the parent process of kap-server (docs/architecture/kite-design.md
§2): it spawns and supervises `kimi web --no-open` (crash restart with bounded
backoff, graceful shutdown), owns the WS subscription client, assembles the
full bridge (Feishu transport -> AppHandler inbound path -> EventPipeline
outbound path), and publishes a best-effort runtime status for kitectl. On
SIGTERM/SIGINT it shuts timers, WS clients, and the managed child down
cleanly.

Restart recovery (mvp-scope §4.6): bindings/modes/cursors come back from the
stores; after the startup (re)subscribe, prompt ownership is rebuilt
best-effort and every bound session goes through a snapshot rebuild so
in-flight execution cards are re-anchored and unrebuildable approvals are
explicitly expired.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from kite import cards
from kite import config as kite_config
from kite import env_file
from kite.adapters import kap_server
from kite.adapters.kap_server import (
    BackoffPolicy,
    KapRestClient,
    KapServerProcess,
    KapWsClient,
    ResyncRequest,
)
from kite.event_pipeline import (
    EventPipeline,
    OutboundAppHandler,
    SwappableKapRest,
    WsSubscriptionHook,
)
from kite.feishu_transport import FeishuTransport, TransportHandler
from kite.feishu_ws_proxy import DEFAULT_FEISHU_WS_PROXY
from kite.logging_setup import configure_logging
from kite.platform_paths import default_data_root
from kite.prompt_ownership import PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.runtime_status import RuntimeStatusWriter
from kite.stores.binding_store import BindingStore
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.terminal_result_store import TerminalResultStore

logger = logging.getLogger("kite.kited")


class _TransportHandlerProxy(TransportHandler):
    """Lets the transport be constructed before the AppHandler it dispatches
    to (FeishuTransport takes the handler at construction; the handler takes
    the transport for outbound sends). Inbound events arriving before the
    delegate is installed are logged and dropped — the transport only starts
    after assembly completes, so this is a defensive path.
    """

    def __init__(self) -> None:
        self.impl: Optional[TransportHandler] = None

    def on_message(self, message: Any) -> None:
        if self.impl is not None:
            self.impl.on_message(message)

    def on_attachment(self, attachment: Any) -> None:
        if self.impl is not None:
            self.impl.on_attachment(attachment)

    def on_card_action(self, action: Any) -> Any:
        if self.impl is not None:
            return self.impl.on_card_action(action)
        from kite.feishu_transport import CardActionResponse

        return CardActionResponse()

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        if self.impl is not None:
            self.impl.on_message_recalled(chat_id, message_id)

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        if self.impl is not None:
            self.impl.on_chat_unavailable(chat_id, reason=reason)

    def on_bot_menu(self, open_id: str, event_key: str) -> None:
        if self.impl is not None:
            self.impl.on_bot_menu(open_id, event_key)


@dataclass(slots=True)
class OutboundRuntime:
    """The assembled bridge (everything except the per-incarnation kap pieces).

    ``rest_proxy`` / ``ws_hook`` are the swap points kited re-points on every
    kap-server incarnation; the Feishu transport and the handler/pipeline
    state live across kap restarts.
    """

    transport: FeishuTransport
    handler: OutboundAppHandler
    pipeline: EventPipeline
    runtime_loop: RuntimeLoop
    rest_proxy: SwappableKapRest
    ws_hook: WsSubscriptionHook
    transport_thread: threading.Thread


def build_outbound_runtime(
    *,
    config: Mapping[str, Any],
    data_dir: pathlib.Path,
    init_token: str,
) -> OutboundRuntime:
    """Assemble transport + handler + pipeline + stores (main()'s job)."""
    loop = RuntimeLoop(name="kite-runtime")
    rest_proxy = SwappableKapRest()
    ws_hook = WsSubscriptionHook()
    binding_store = BindingStore(data_dir)
    ownership = PromptOwnership()

    handler_proxy = _TransportHandlerProxy()
    transport = FeishuTransport(
        str(config["app_id"]),
        str(config["app_secret"]),
        handler_proxy,
        bot_open_id=str(config.get("bot_open_id") or ""),
        feishu_ws_proxy=str(config.get("feishu_ws_proxy") or DEFAULT_FEISHU_WS_PROXY),
    )
    pipeline = EventPipeline(
        transport=transport,
        rest=rest_proxy,
        binding_store=binding_store,
        terminal_store=TerminalResultStore(data_dir),
        ownership=ownership,
        runtime_loop=loop,
        cursor_store=EventCursorStore(data_dir),
        approval_timeout_seconds=kite_config.approval_timeout_seconds(config),
        question_timeout_seconds=cards.DEFAULT_QUESTION_TIMEOUT_SECONDS,
    )
    handler = OutboundAppHandler(
        event_pipeline=pipeline,
        transport=transport,
        rest=rest_proxy,
        binding_store=binding_store,
        runtime_loop=loop,
        config=config,
        init_token=init_token,
        prompt_ownership=ownership,
        on_session_bound=ws_hook,
    )
    handler_proxy.impl = handler

    # The lark WS client blocks in start() and exposes no stop(): the
    # transport lives on a daemon thread and is cleaned up by process exit.
    transport_thread = threading.Thread(
        target=transport.start, name="kite-feishu-ws", daemon=True
    )
    return OutboundRuntime(
        transport=transport,
        handler=handler,
        pipeline=pipeline,
        runtime_loop=loop,
        rest_proxy=rest_proxy,
        ws_hook=ws_hook,
        transport_thread=transport_thread,
    )


def run(
    *,
    kimi_bin: str,
    home: pathlib.Path,
    host: str,
    port: int,
    env_overlay: Mapping[str, str] | None,
    data_dir: pathlib.Path,
    stop_event: threading.Event,
    stale_seconds: float = 45.0,
    reconnect_delay_seconds: float = 2.0,
    backoff: BackoffPolicy | None = None,
    readiness_timeout_seconds: float = 60.0,
    outbound: OutboundRuntime | None = None,
) -> int:
    """Supervise kap-server until stop_event is set. Returns a process exit code."""
    backoff = backoff or BackoffPolicy()
    status = RuntimeStatusWriter(data_dir)
    cursor_store = EventCursorStore(data_dir)
    binding_store = BindingStore(data_dir)
    proc: KapServerProcess | None = None
    recovered = False
    if outbound is not None:
        outbound.runtime_loop.start()
        outbound.transport_thread.start()
        outbound.pipeline.set_snapshot_rebuilt_hook(
            lambda _sid, _snap: status.update(ws={"last_resync_at": time.time()})
        )
    try:
        while not stop_event.is_set():
            proc = KapServerProcess(
                kimi_bin=kimi_bin,
                home=home,
                host=host,
                requested_port=port,
                env_overlay=env_overlay,
                readiness_timeout_seconds=readiness_timeout_seconds,
                stdout_path=data_dir / "kap-server.stdout.log",
            )
            started_at = time.monotonic()
            proc.start()
            assert proc.port is not None and proc.token is not None
            logger.info("kap-server ready: pid=%s port=%d home=%s", proc.pid, proc.port, home)
            status.update(kap={"pid": proc.pid, "port": proc.port})

            rest = KapRestClient(host, proc.port, proc.token)
            _log_server_meta(rest)
            if outbound is not None:
                outbound.rest_proxy.set_client(rest)

            def on_resync(request: ResyncRequest) -> None:
                # Resync discipline: rebuild from a REST snapshot and adopt its
                # cursor (snapshot as_of_seq is a cursor source of truth).
                try:
                    snapshot = rest.get_snapshot(request.session_id)
                except Exception as exc:  # noqa: BLE001 - logged; WS retries
                    logger.error(
                        "snapshot rebuild failed for %s: %s", request.session_id, exc
                    )
                    return
                cursor_store.set(request.session_id, snapshot.cursor)
                status.update(ws={"last_resync_at": time.time()})
                logger.info(
                    "resync rebuilt session=%s as_of_seq=%d busy=%s",
                    request.session_id,
                    snapshot.as_of_seq,
                    snapshot.busy,
                )

            def on_connection_change(connected: bool) -> None:
                status.update(ws={"connected_at": time.time() if connected else None})

            def on_event(event: Any) -> None:
                logger.debug(
                    "event %s session=%s seq=%s", event.type, event.session_id, event.seq
                )

            ws = KapWsClient(
                host=host,
                port=proc.port,
                token=proc.token,
                rest_client=rest,
                cursor_store=cursor_store,
                stale_seconds=stale_seconds,
                reconnect_delay_seconds=reconnect_delay_seconds,
                on_event=outbound.pipeline.handle_event if outbound else on_event,
                on_resync_required=(
                    outbound.pipeline.handle_resync_required if outbound else on_resync
                ),
                on_connection_change=on_connection_change,
            )
            ws.start()
            if outbound is not None:
                outbound.ws_hook.set_client(ws)
            bound_sessions = [
                binding["session_id"] for binding in binding_store.load_all().values()
            ]
            for session_id in bound_sessions:
                ws.subscribe(session_id)

            if outbound is not None and not recovered:
                # Restart recovery (§4.6), serialized on the runtime loop:
                # ownership first (approval routing needs it), then a snapshot
                # rebuild per bound session (cards + approval expiry).
                recovered = True
                outbound.runtime_loop.submit(outbound.handler.rebuild_prompt_ownership)
                outbound.pipeline.startup_recovery(bound_sessions)

            crashed = False
            while not stop_event.is_set():
                returncode = proc.poll()
                if returncode is not None:
                    crashed = True
                    logger.warning("kap-server pid=%s exited rc=%s", proc.pid, returncode)
                    break
                stop_event.wait(0.5)
            ws.stop()
            if outbound is not None:
                outbound.ws_hook.set_client(None)
                outbound.rest_proxy.set_client(None)

            if not crashed:
                break
            delay = backoff.next_delay(time.monotonic() - started_at)
            logger.info("restarting kap-server in %.1fs", delay)
            if stop_event.wait(delay):
                break
        return 0
    finally:
        if outbound is not None:
            # Clean shutdown: cancel pipeline timers, stop the runtime loop;
            # the Feishu transport thread is daemonic (lark has no stop()).
            outbound.pipeline.shutdown()
            outbound.runtime_loop.stop()
        if proc is not None and proc.poll() is None:
            logger.info("stopping kap-server pid=%s", proc.pid)
            proc.stop()
        status.clear()
        logger.info("kited stopped")


def _log_server_meta(rest: KapRestClient) -> None:
    """Best-effort meta probe; a fresh server may need a moment to serve HTTP."""
    for attempt in range(5):
        try:
            meta = rest.meta()
        except Exception as exc:  # noqa: BLE001 - probe is best-effort
            logger.warning("kap-server /meta probe failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(0.5)
            continue
        logger.info(
            "kap-server meta: version=%s backend=%s server_id=%s",
            meta.server_version,
            meta.backend,
            meta.server_id,
        )
        return


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kited",
        description="KITE daemon: supervise kap-server and the WS event stream.",
    )
    parser.add_argument(
        "--config-dir",
        help="instance config directory (default: KITE_CONFIG_DIR or platform default)",
    )
    parser.add_argument(
        "--data-dir",
        help="instance data directory (default: KITE_DATA_ROOT or platform default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.config_dir:
        os.environ["KITE_CONFIG_DIR"] = args.config_dir
    if args.data_dir:
        os.environ["KITE_DATA_ROOT"] = args.data_dir

    log_path = configure_logging()
    logger.info("kited starting; log file: %s", log_path)

    env_overlay = env_file.load_env_file()
    try:
        config = kite_config.load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("instance config unusable: %s", exc)
        return 2
    kap = kite_config.kap_settings(config)

    kimi_bin = kap_server.resolve_kimi_bin(kap.kimi_bin)
    if not kimi_bin:
        logger.error("kimi binary not found (set kap.kimi_bin or KIMI_BIN, or install kimi-code)")
        return 2
    version = kap_server.detect_kimi_version(kimi_bin)
    if version != kap_server.VERIFIED_KIMI_VERSION:
        logger.warning(
            "kimi version %s differs from the verified version %s; continuing anyway "
            "(docs/architecture/kite-design.md §10: warn, don't block)",
            version or "<unknown>",
            kap_server.VERIFIED_KIMI_VERSION,
        )

    home = kap_server.resolve_kap_home(kap.home)
    data_dir = default_data_root()

    outbound = build_outbound_runtime(
        config=config,
        data_dir=data_dir,
        init_token=kite_config.ensure_init_token(),
    )

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("received signal %d; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    return run(
        kimi_bin=kimi_bin,
        home=home,
        host=kap.host,
        port=kap.port or kap_server.DEFAULT_KAP_PORT,
        env_overlay=env_overlay,
        data_dir=data_dir,
        stop_event=stop_event,
        stale_seconds=kap.stale_seconds or 45.0,
        reconnect_delay_seconds=kap.reconnect_delay_seconds or 2.0,
        backoff=BackoffPolicy(
            base_seconds=kap.backoff_base_seconds or 1.0,
            cap_seconds=kap.backoff_cap_seconds or 30.0,
        ),
        outbound=outbound,
    )


if __name__ == "__main__":
    raise SystemExit(main())
