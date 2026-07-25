"""
kited — the KITE daemon entrypoint.

kited is the parent process of kap-server (docs/architecture/kite-design.md
§2): it spawns and supervises `kimi web --no-open` (crash restart with bounded
backoff, graceful shutdown), owns the WS subscription client, assembles the
full bridge (Feishu transport -> AppHandler inbound path -> EventPipeline
outbound path), serves the loopback control plane kitectl mutates the daemon
through (docs/decisions/control-plane.md), and publishes a best-effort
runtime status for kitectl. On SIGTERM/SIGINT it first fail-close sweeps
pending approvals/questions (responded upstream while kap is still up, cards
patched expired locally), then shuts timers, WS clients, the control plane,
and the managed child down cleanly.

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
from typing import Any, Callable, Mapping, Optional

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
from kite.control_plane import ControlError, ControlPlaneServer
from kite.event_pipeline import (
    EventPipeline,
    OutboundAppHandler,
    SwappableKapRest,
    WsSubscriptionHook,
)
from kite.feishu_transport import FeishuTransport, TransportHandler
from kite.feishu_ws_proxy import DEFAULT_FEISHU_WS_PROXY
from kite.group_history import GroupHistoryRecovery
from kite.identity_names import IdentityNames
from kite.logging_setup import configure_logging
from kite.platform_paths import default_data_root
from kite.prompt_ownership import PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.runtime_status import RuntimeStatusWriter
from kite.stores.binding_store import BindingStore
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.group_config_store import GroupConfigStore
from kite.stores.group_log_store import GroupLogStore
from kite.stores.pending_attachment_store import PendingAttachmentStore
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

    def on_merge_forward(self, message: Any) -> None:
        if self.impl is not None:
            self.impl.on_merge_forward(message)

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
    kap = kite_config.kap_settings(config)
    prompt_model = kap_server.resolve_prompt_model(
        kap.model, kap_server.resolve_kap_home(kap.home)
    )
    if prompt_model:
        logger.info("prompt model carried per prompt: %s", prompt_model)
    else:
        logger.warning(
            "no prompt model resolvable (kap.model unset and config.toml "
            "default_model missing); prompts will fail upstream and surface "
            "as failed terminal cards"
        )
    terminal_store = TerminalResultStore(data_dir)
    # One shared display-name cache: the pipeline's routing hints and the
    # forward aggregator's transcript sender names read through it.
    names = IdentityNames(transport.fetch_user_name)
    # Assistant-mode group context (group-chat contract §3.3): the per-chat
    # log store (axis 6) plus the recovery port merging it with the Feishu
    # REST history backfill. The render port reuses the transport's text
    # extraction/mention normalization so history items read exactly like
    # live inbound messages.
    group_log_store = GroupLogStore(data_dir)

    def _render_group_history_text(
        msg_type: str, content_dict: dict, mentions: list
    ) -> str:
        text = transport.extract_text(msg_type, content_dict)
        if text and mentions:
            text = transport.normalize_mentions(text, list(mentions))
        return text

    group_history = GroupHistoryRecovery(
        list_messages=transport.list_messages,
        render_text=_render_group_history_text,
        name_of=names.name_of,
        log_store=group_log_store,
        app_id=str(config["app_id"]),
        fetch_limit=kite_config.group_history_fetch_limit(config),
        lookback_seconds=kite_config.group_history_fetch_lookback_seconds(config),
    )
    group_config_store = GroupConfigStore(data_dir)

    def _group_mode_of(chat_id: str) -> str | None:
        config = group_config_store.load(chat_id)
        if config is None or not config.get("activated"):
            return None
        return str(config.get("mode") or "") or None

    pipeline = EventPipeline(
        transport=transport,
        rest=rest_proxy,
        binding_store=binding_store,
        terminal_store=terminal_store,
        ownership=ownership,
        runtime_loop=loop,
        cursor_store=EventCursorStore(data_dir),
        approval_timeout_seconds=kite_config.approval_timeout_seconds(config),
        question_timeout_seconds=cards.DEFAULT_QUESTION_TIMEOUT_SECONDS,
        names=names,
        group_mode_of=_group_mode_of,
    )
    handler = OutboundAppHandler(
        event_pipeline=pipeline,
        transport=transport,
        rest=rest_proxy,
        binding_store=binding_store,
        attachment_store=PendingAttachmentStore(data_dir),
        group_config_store=group_config_store,
        runtime_loop=loop,
        config=config,
        init_token=init_token,
        prompt_model=prompt_model,
        prompt_ownership=ownership,
        on_session_bound=ws_hook,
        terminal_store=terminal_store,
        names=names,
        group_log_store=group_log_store,
        group_history=group_history,
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


def _control_dispatch(outbound: OutboundRuntime) -> Callable[[str, dict[str, Any]], Any]:
    """The control-plane method table (docs/decisions/control-plane.md).

    Only mutations that must serialize through the daemon live here;
    read-only kitectl queries keep talking to kap REST / the stores directly.
    """

    def dispatch(method: str, params: dict[str, Any]) -> Any:
        if method == "prompt/submit":
            return outbound.handler.submit_prompt_control(params)
        if method == "image/send":
            return outbound.handler.send_image_control(params)
        raise ControlError(f"unknown control method: {method}", code="unknown_method")

    return dispatch


def _start_kap_child(proc: KapServerProcess, stop_event: threading.Event) -> bool:
    """Spawn kap-server, aborting the blocking readiness wait on shutdown.

    ``KapServerProcess.start()`` blocks in its readiness wait with no stop
    hook (up to ``readiness_timeout_seconds``, default 60s), so a SIGTERM
    arriving during startup would otherwise drag the full window (audit
    L24). The spawn runs on a helper thread; when ``stop_event`` fires
    mid-startup the child is SIGTERMed, which fails the wait promptly.
    Returns False when startup was aborted by the stop event; genuine start
    failures re-raise on the calling thread, exactly as before.
    """
    error: list[BaseException] = []

    def _target() -> None:
        try:
            proc.start()
        except BaseException as exc:  # re-raised on the main thread below
            error.append(exc)

    starter = threading.Thread(target=_target, name="kite-kap-start", daemon=True)
    starter.start()
    while starter.is_alive():
        if stop_event.wait(0.2):
            logger.info("shutdown requested during kap-server startup; stopping child")
            proc.stop()
            starter.join(timeout=10)
            return False
    starter.join()
    if error:
        raise error[0]
    return True


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
    control_token: str | None = None,
) -> int:
    """Supervise kap-server until stop_event is set. Returns a process exit code."""
    backoff = backoff or BackoffPolicy()
    status = RuntimeStatusWriter(data_dir)
    cursor_store = EventCursorStore(data_dir)
    binding_store = BindingStore(data_dir)
    proc: KapServerProcess | None = None
    control_plane: ControlPlaneServer | None = None
    recovered = False
    if outbound is not None:
        outbound.runtime_loop.start()
        # Group mention triggering needs the bot's own open_id; discover it
        # before any inbound message can arrive (fail-closed when missing).
        discovered_open_id = outbound.transport.fetch_bot_open_id()
        if discovered_open_id:
            outbound.transport.set_bot_open_id(discovered_open_id)
        else:
            logger.error(
                "bot identity discovery failed; group mention triggering stays disabled"
            )
        outbound.transport_thread.start()
        def on_snapshot_rebuilt(session_id: str, _snapshot: object) -> None:
            status.update(ws={"last_resync_at": time.time()})
            # M7: an ack-listed resync may have established no server-side
            # subscription (cold/deleted session); the successful rebuild
            # re-subscribes once (guarded inside the WS client).
            outbound.ws_hook.resubscribe_after_rebuild(session_id)

        outbound.pipeline.set_snapshot_rebuilt_hook(on_snapshot_rebuilt)
        # The control plane starts with the outbound runtime and publishes
        # its endpoint (control_plane.json) for kitectl discovery.
        if control_token is None:
            control_token = kite_config.ensure_control_token()
        control_plane = ControlPlaneServer(
            data_dir=data_dir,
            dispatch=_control_dispatch(outbound),
            auth_token=lambda: str(control_token),
        )
        control_plane.start()
        logger.info("control plane listening on 127.0.0.1:%d", control_plane.port)
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
            if not _start_kap_child(proc, stop_event):
                # Shutdown requested while the child was still becoming
                # ready; it has already been stopped — exit cleanly.
                break
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
                on_error_frame=(
                    outbound.pipeline.handle_error_frame if outbound else None
                ),
                on_volatile=(
                    outbound.pipeline.handle_volatile if outbound else None
                ),
            )
            ws.start()
            if outbound is not None:
                outbound.ws_hook.set_client(ws)
            bound_sessions = [
                binding["session_id"] for binding in binding_store.load_all().values()
            ]
            for session_id in bound_sessions:
                try:
                    ws.subscribe(session_id)
                except Exception:  # noqa: BLE001 - logged; the loop continues
                    # A single failed startup subscribe (ack timeout, kap
                    # hiccup) must not crash run() (audit L25): the session
                    # is re-subscribed on the next WS reconnect, and the
                    # startup recovery below still rebuilds its state.
                    logger.exception(
                        "startup subscribe failed session=%s; continuing supervision",
                        session_id,
                    )

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
                if crashed:
                    # Crash window: drop the dead incarnation's client so
                    # commands fail closed until the next one is up. On a
                    # clean stop the client stays live so the pipeline's
                    # shutdown sweep can still respond upstream (kap-server
                    # is stopped only afterwards, in the finally below).
                    outbound.rest_proxy.set_client(None)

            if not crashed:
                break
            delay = backoff.next_delay(time.monotonic() - started_at)
            logger.info("restarting kap-server in %.1fs", delay)
            if stop_event.wait(delay):
                break
        return 0
    finally:
        if control_plane is not None:
            # Stop the control plane first so no request can arrive after the
            # runtime loop below has stopped (it also unpublishes the
            # metadata file, marking the daemon undiscoverable).
            control_plane.stop()
        if outbound is not None:
            # Clean shutdown: the pipeline's fail-close sweep responds to
            # pending approvals/questions upstream — the rest proxy must stay
            # live until it has run (the barrier drains the loop), then the
            # timers are cancelled and the loop stops. The Feishu transport
            # thread is daemonic (lark has no stop()). The handler's close()
            # first cancels pending forward-aggregation timers so no flush
            # can dispatch into a stopping loop.
            outbound.handler.close()
            outbound.pipeline.shutdown()
            outbound.runtime_loop.call(lambda: None)
            outbound.rest_proxy.set_client(None)
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
        kap = kite_config.kap_settings(config)
    except (FileNotFoundError, ValueError) as exc:
        # Invalid `kap:` values (bad port, non-loopback host, ...) get the
        # same clean exit as a missing/unusable config — never a traceback
        # (audit L23).
        logger.error("instance config unusable: %s", exc)
        return 2

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
