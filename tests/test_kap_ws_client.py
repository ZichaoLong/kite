import tempfile
import time
import types
import unittest
import unittest.mock
import uuid

import fake_kap
import websockets.exceptions
from kite.adapters.kap_server import (
    KapError,
    KapEvent,
    KapRestClient,
    KapTransportError,
    KapWsClient,
    KapWsError,
)
from kite.stores.event_cursor_store import EventCursor, EventCursorStore


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class KapWsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = fake_kap.FakeKapState()
        self.rest_server = fake_kap.make_rest_server(self.state)
        self.addCleanup(self.rest_server.shutdown)
        self.ws_server = fake_kap.make_ws_server(self.state)
        self.addCleanup(self.ws_server.close)
        self.rest = KapRestClient("127.0.0.1", self.rest_server.server_address[1], self.state.token)
        self.cursors = EventCursorStore(self._tmp.name)
        self.events = []
        self.resyncs = []
        self.client: KapWsClient | None = None

    def _start_client(self, **overrides) -> KapWsClient:
        options = {
            "host": "127.0.0.1",
            "port": self.ws_server.port,
            "token": self.state.token,
            "rest_client": self.rest,
            "cursor_store": self.cursors,
            "stale_seconds": 0.3,
            "reconnect_delay_seconds": 0.1,
            "on_event": self.events.append,
            "on_resync_required": self.resyncs.append,
        }
        options.update(overrides)
        self.client = KapWsClient(**options)
        self.client.start()
        self.addCleanup(self.client.stop)
        return self.client

    def test_handshake_adopts_ack_cursor(self) -> None:
        session = self.state.create_session("s-1")
        for index in range(3):
            self.state.append_event(session, "session.meta.updated", {"i": index})
        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: client.connected))
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(3, session.epoch))
        )

    def test_replay_of_missed_events_before_live_stream(self) -> None:
        session = self.state.create_session("s-1")
        for index in range(3):
            self.state.append_event(session, "session.meta.updated", {"i": index})
        self.cursors.set("s-1", EventCursor(seq=1, epoch=session.epoch))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: len(self.events) >= 2))
        self.assertEqual([e.seq for e in self.events[:2]], [2, 3])
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(3, session.epoch))
        )
        self.assertEqual(self.resyncs, [])

    def test_live_event_dispatches_and_advances_cursor(self) -> None:
        session = self.state.create_session("s-1")
        client = self._start_client()
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: client.connected))

        self.state.append_event(session, "turn.started", {"turn": 1})

        self.assertTrue(wait_until(lambda: len(self.events) == 1))
        event = self.events[0]
        self.assertEqual(event.type, "turn.started")
        self.assertEqual(event.session_id, "s-1")
        self.assertEqual(event.payload, {"turn": 1})
        self.assertFalse(event.volatile)
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(1, session.epoch))
        )

    def test_buffer_overflow_fires_resync_from_pre_ack_frame(self) -> None:
        session = self.state.create_session("s-1")
        for index in range(self.state.replay_window + 5):
            self.state.append_event(session, "session.meta.updated", {"i": index})
        self.cursors.set("s-1", EventCursor(seq=1, epoch=session.epoch))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: len(self.resyncs) >= 1))
        reasons = [r.reason for r in self.resyncs]
        self.assertIn("buffer_overflow", reasons)
        request = next(r for r in self.resyncs if r.reason == "buffer_overflow")
        self.assertEqual(request.session_id, "s-1")
        self.assertEqual(request.current_seq, session.seq)
        self.assertEqual(request.epoch, session.epoch)
        # The ack cursor is adopted: later reconnects resume from the watermark.
        self.assertTrue(
            wait_until(
                lambda: self.cursors.get("s-1") == EventCursor(session.seq, session.epoch)
            )
        )

    def test_epoch_mismatch_fires_resync(self) -> None:
        session = self.state.create_session("s-1")
        self.state.append_event(session, "session.meta.updated", {})
        self.cursors.set("s-1", EventCursor(seq=0, epoch="ep_bogus"))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: any(r.reason == "epoch_changed" for r in self.resyncs)))

    def test_cold_session_is_warmed_before_subscribing(self) -> None:
        session = self.state.create_session("s-1")
        self.state.append_event(session, "session.meta.updated", {})
        self.assertFalse(session.warm)
        self.cursors.set("s-1", EventCursor(seq=0, epoch=session.epoch))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: client.connected))
        self.assertTrue(session.warm)
        log = list(self.state.log)
        self.assertIn("rest:GET sessions/s-1/prompts", log)
        self.assertLess(
            log.index("rest:GET sessions/s-1/prompts"), log.index("ws:client_hello")
        )
        self.assertEqual(self.resyncs, [])

    def test_stale_connection_triggers_reconnect(self) -> None:
        self._start_client()
        self.assertTrue(wait_until(lambda: self.state.hello_count >= 1))
        # No frames flow on an idle connection (kap has no heartbeat and does
        # not answer app-level pings): stale detection must cycle it.
        self.assertTrue(wait_until(lambda: self.state.hello_count >= 2, timeout=5.0))

    def test_error_frame_fires_callback(self) -> None:
        session = self.state.create_session("s-1")
        errors = []
        client = self._start_client(on_error_frame=errors.append)
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: client.connected))
        self.state.send_error_frame(session, "model.not_configured", "Model not set")
        self.assertTrue(wait_until(lambda: len(errors) >= 1))
        error = errors[0]
        self.assertEqual(error.code, "model.not_configured")
        self.assertEqual(error.message, "Model not set")
        self.assertEqual(error.session_id, "s-1")
        self.assertEqual(error.agent_id, "main")
        self.assertFalse(error.retryable)

    def test_ack_not_found_drops_session_from_wanted(self) -> None:
        session = self.state.create_session("s-1")
        client = self._start_client()
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: self.cursors.get("s-1") is not None))
        # A deleted session must leave the resubscribe loop (audit L33).
        client.subscribe("s-missing")
        self.assertTrue(wait_until(lambda: "s-missing" not in client._wanted_sessions))

    def test_send_and_wait_timeout_leaves_no_pending_ack(self) -> None:
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))
        with self.assertRaises(KapWsError):
            # The fake ignores unknown frame types, so no ack can arrive.
            client._send_and_wait(client._ws, "bogus", {"session_ids": []}, 0.3)
        self.assertEqual(client._pending_acks, {})

    def test_late_ack_after_timeout_is_dropped(self) -> None:
        # Audit R-4: an ack that arrives after its wait timed out used to be
        # stored unconditionally and leak in _pending_acks forever; only ids
        # with a live waiter may be stored now.
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))
        fixed = uuid.UUID(int=0)
        with unittest.mock.patch(
            "kite.adapters.kap_server.uuid.uuid4", return_value=fixed
        ):
            with self.assertRaises(KapWsError):
                # The fake ignores unknown frame types, so no ack can arrive.
                client._send_and_wait(client._ws, "bogus", {"session_ids": []}, 0.3)
        request_id = f"{client._client_id}-{fixed.hex[:8]}"
        client._dispatch_frame(
            client._ws, {"type": "ack", "id": request_id, "code": 0, "payload": {}}
        )
        self.assertEqual(client._pending_acks, {})
        self.assertNotIn(request_id, client._awaited_acks)

    def test_never_awaited_ack_is_dropped(self) -> None:
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))
        client._dispatch_frame(
            client._ws,
            {"type": "ack", "id": "kited-deadbeef", "code": 0, "payload": {}},
        )
        self.assertEqual(client._pending_acks, {})

    def test_is_auth_failure_classification(self) -> None:
        from kite.adapters.kap_server import _is_auth_failure

        self.assertTrue(_is_auth_failure(Exception("401 Unauthorized")))
        self.assertTrue(_is_auth_failure(type("E", (Exception,), {"status": 401})()))
        self.assertTrue(_is_auth_failure(type("E", (Exception,), {"code": 4401})()))
        # The WS upgrade rejection shape (audit R-5): websockets' InvalidStatus
        # carries the status on .response.status_code.
        rejected = websockets.exceptions.InvalidStatus(
            types.SimpleNamespace(status_code=401)
        )
        self.assertTrue(_is_auth_failure(rejected))
        # A REST 401 envelope raises KapError(http_status=401); a non-envelope
        # reply raises KapTransportError with the status in the message.
        self.assertTrue(_is_auth_failure(KapError(40001, "unauthorized", http_status=401)))
        self.assertTrue(_is_auth_failure(KapTransportError("HTTP 401: non-envelope reply")))
        self.assertFalse(_is_auth_failure(Exception("connection refused")))
        self.assertFalse(
            _is_auth_failure(
                websockets.exceptions.InvalidStatus(types.SimpleNamespace(status_code=404))
            )
        )

    def test_repeated_ws_auth_rejections_reach_the_alert_log(self) -> None:
        # Audit R-5: the upgrade-401 (InvalidStatus) must feed the auth
        # escalation counter, not the generic unexpected-error branch.
        rejection = websockets.exceptions.InvalidStatus(
            types.SimpleNamespace(status_code=401)
        )
        failing = KapWsClient(
            host="127.0.0.1",
            port=self.ws_server.port,
            token=self.state.token,
            rest_client=self.rest,
            reconnect_delay_seconds=0.01,
        )
        with unittest.mock.patch.object(KapWsClient, "_connect_once", side_effect=rejection):
            with self.assertLogs("kite.adapters.kap", level="ERROR") as captured:
                failing.start()
                try:
                    self.assertTrue(
                        wait_until(
                            lambda: any(
                                "auth failed 3 times" in record.getMessage()
                                for record in captured.records
                            ),
                            timeout=5.0,
                        )
                    )
                finally:
                    failing.stop()

    def test_warmup_auth_failure_propagates(self) -> None:
        # Audit R-5: a warmup REST 401 was swallowed as a warning and the
        # connect succeeded; it must raise so the run loop counts it.
        class _StubRest:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc

            def get_prompts(self, _session_id: str) -> None:
                raise self._exc

        auth_dead = KapWsClient(
            host="127.0.0.1",
            port=1,
            token="t",
            rest_client=_StubRest(KapError(40001, "unauthorized", http_status=401)),
        )
        with self.assertRaises(KapError):
            auth_dead._warm_session("s-1")
        transport_dead = KapWsClient(
            host="127.0.0.1",
            port=1,
            token="t",
            rest_client=_StubRest(KapTransportError("HTTP 401: non-envelope reply")),
        )
        with self.assertRaises(KapTransportError):
            transport_dead._warm_session("s-1")
        # Non-auth warmup failures stay non-fatal (the ack reports not_found).
        missing = KapWsClient(
            host="127.0.0.1",
            port=1,
            token="t",
            rest_client=_StubRest(KapError(40401, "session not found")),
        )
        with self.assertLogs("kite.adapters.kap", level="WARNING"):
            missing._warm_session("s-1")

    def test_reconnect_backoff_grows_exponentially(self) -> None:
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))
        client.stop()
        delays: list[float] = []
        failing = KapWsClient(
            host="127.0.0.1",
            port=self.ws_server.port,
            token=self.state.token,
            rest_client=self.rest,
            reconnect_delay_seconds=0.05,
        )
        failing._stop_event.wait = lambda timeout: (delays.append(timeout), False)[1]  # type: ignore[method-assign]
        with unittest.mock.patch.object(KapWsClient, "_connect_once", side_effect=OSError("down")):
            failing.start()
            while len(delays) < 6:
                time.sleep(0.01)
            failing.stop()
        self.assertLess(delays[0], delays[3])  # growing, not flat
        self.assertLessEqual(delays[-1], 60.0)

    def test_error_frame_advances_cursor_and_never_replays(self) -> None:
        # Audit M6: the error frame is a durable event; the cursor must
        # advance past it or a reconnect replays it and a healthy prompt
        # could be misjudged failed.
        session = self.state.create_session("s-1")
        errors = []
        client = self._start_client(on_error_frame=errors.append)
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: client.connected))

        self.state.send_error_frame(session, "provider.auth_error", "token expired")

        self.assertTrue(wait_until(lambda: len(errors) == 1))
        # The cursor moved past the durable error frame (seq 1).
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(1, session.epoch))
        )

        # Reconnect: the journal still holds the error frame, but the
        # resumed cursor is already past it — it must not fire again.
        first_port = self.ws_server.port
        self.ws_server.close()
        self.ws_server = fake_kap.make_ws_server(self.state, port=first_port)
        self.assertTrue(wait_until(lambda: self.state.hello_count >= 2, timeout=5.0))
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(1, session.epoch))
        )
        time.sleep(0.3)
        self.assertEqual(len(errors), 1)

    def test_ack_cursor_never_rewinds_stored_cursor(self) -> None:
        # Audit M4: the ack path gets the same monotonic guard as the
        # snapshot-adopt path — same epoch keeps the higher seq, a new
        # epoch is adopted.
        client = KapWsClient(
            host="127.0.0.1",
            port=1,
            token="t",
            rest_client=self.rest,
            cursor_store=self.cursors,
        )
        self.cursors.set("s-1", EventCursor(seq=111, epoch="e1"))

        # A stale ack cursor (same epoch, lower seq) must not rewind.
        client._handle_ack_payload({"cursors": {"s-1": {"seq": 110, "epoch": "e1"}}})
        self.assertEqual(self.cursors.get("s-1"), EventCursor(seq=111, epoch="e1"))

        # A newer ack cursor (same epoch, higher seq) is adopted.
        client._handle_ack_payload({"cursors": {"s-1": {"seq": 120, "epoch": "e1"}}})
        self.assertEqual(self.cursors.get("s-1"), EventCursor(seq=120, epoch="e1"))

        # A cross-epoch ack cursor is adopted even at a lower seq.
        client._handle_ack_payload({"cursors": {"s-1": {"seq": 3, "epoch": "e2"}}})
        self.assertEqual(self.cursors.get("s-1"), EventCursor(seq=3, epoch="e2"))

    def test_cold_session_resubscribe_guard_blocks_tight_loop(self) -> None:
        # Audit M7: the ack-listed resync for a cold session carries no
        # cursor (no server-side subscription); the post-rebuild re-subscribe
        # fires at most once per connection per session.
        session = self.state.create_session("s-1")
        session.warmable = False  # the warmup race is always lost
        self.state.append_event(session, "session.meta.updated", {})
        self.cursors.set("s-1", EventCursor(seq=0, epoch=session.epoch))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: len(self.resyncs) >= 1))
        subscribes = list(self.state.log).count("ws:subscribe")

        # The rebuild's re-subscribe attempt fires exactly once...
        self.assertTrue(client.resubscribe_after_rebuild("s-1"))
        self.assertTrue(wait_until(lambda: list(self.state.log).count("ws:subscribe") == subscribes + 1))
        # ...and the guard blocks the loop even though the session stayed
        # cold (its ack listed resync again).
        self.assertTrue(wait_until(lambda: len(self.resyncs) >= 2))
        self.assertFalse(client.resubscribe_after_rebuild("s-1"))
        time.sleep(0.2)
        self.assertEqual(list(self.state.log).count("ws:subscribe"), subscribes + 1)

    def test_resubscribe_after_rebuild_lands_subscription(self) -> None:
        # Audit M7 happy path: the rebuild warmed the session, so the
        # re-subscribe lands and live events flow without a reconnect.
        session = self.state.create_session("s-1")
        session.warmable = False
        self.cursors.set("s-1", EventCursor(seq=0, epoch=session.epoch))

        client = self._start_client()
        client.subscribe("s-1")

        self.assertTrue(wait_until(lambda: len(self.resyncs) >= 1))
        self.state.append_event(session, "turn.started", {"turnId": 1})
        time.sleep(0.2)
        self.assertEqual(self.events, [])  # no server-side subscription yet

        # The snapshot rebuild's REST touch activates the session...
        session.warmable = True
        self.assertTrue(client.resubscribe_after_rebuild("s-1"))

        # ...and the live stream now flows on the same connection.
        self.state.append_event(session, "turn.started", {"turnId": 2})
        self.assertTrue(wait_until(lambda: any(e.type == "turn.started" for e in self.events)))
        # The subscription landed: no further re-subscribe attempt fires.
        self.assertFalse(client.resubscribe_after_rebuild("s-1"))

    def test_server_restart_resubscribes_with_cursor_and_replays(self) -> None:
        session = self.state.create_session("s-1")
        for index in range(2):
            self.state.append_event(session, "session.meta.updated", {"i": index})
        client = self._start_client()
        client.subscribe("s-1")
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(2, session.epoch))
        )

        # Simulate a kap-server restart: WS down, journal preserved, warmth lost.
        first_port = self.ws_server.port
        self.ws_server.close()
        self.state.simulate_server_restart()
        self.state.append_event(session, "session.meta.updated", {"i": 2})
        self.ws_server = fake_kap.make_ws_server(self.state, port=first_port)

        self.assertTrue(wait_until(lambda: len(self.events) >= 1, timeout=5.0))
        self.assertEqual(self.events[0].seq, 3)
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(3, session.epoch))
        )
        # Warmup before resubscribe avoided the cold-session resync.
        self.assertEqual(self.resyncs, [])

    def test_subscribe_after_connect_returns_ack(self) -> None:
        session = self.state.create_session("s-1")
        self.state.append_event(session, "session.meta.updated", {})
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))

        ack = client.subscribe("s-1")

        self.assertIsNotNone(ack)
        self.assertIn("s-1", ack.get("accepted") or ack.get("accepted_subscriptions") or [])
        self.state.append_event(session, "turn.started", {})
        self.assertTrue(wait_until(lambda: any(e.type == "turn.started" for e in self.events)))

    def test_unknown_session_reports_not_found_without_resync(self) -> None:
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))

        ack = client.subscribe("missing")

        self.assertIsNotNone(ack)
        self.assertIn("missing", ack.get("not_found") or [])
        self.assertEqual(self.resyncs, [])

    def test_stop_shuts_thread_down(self) -> None:
        client = self._start_client()
        self.assertTrue(wait_until(lambda: client.connected))
        client.stop()
        thread = client._thread
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertFalse(client.connected)

    def test_volatile_assistant_delta_fires_on_volatile_without_advancing_cursor(self) -> None:
        session = self.state.create_session("s-1")
        self.state.append_event(session, "turn.started", {"turnId": 1})
        deltas = []
        client = self._start_client(on_volatile=deltas.append)
        client.subscribe("s-1")
        self.assertTrue(
            wait_until(lambda: self.cursors.get("s-1") == EventCursor(1, session.epoch))
        )

        self.state.append_volatile_event(
            session, "assistant.delta", {"turnId": 1, "delta": "你好"}, offset=0
        )
        self.state.append_volatile_event(
            session, "assistant.delta", {"turnId": 1, "delta": "世界"}, offset=3
        )

        self.assertTrue(wait_until(lambda: len(deltas) == 2))
        self.assertEqual(deltas[0].session_id, "s-1")
        self.assertEqual(deltas[0].offset, 0)
        self.assertEqual(deltas[0].text_delta, "你好")
        self.assertEqual(deltas[1].offset, 3)
        # Volatile frames never advance the durable cursor and never reach
        # the durable on_event path.
        self.assertEqual(self.cursors.get("s-1"), EventCursor(1, session.epoch))
        self.assertEqual([e.type for e in self.events], [])

    def test_volatile_delta_without_offset_is_dropped(self) -> None:
        session = self.state.create_session("s-1")
        deltas = []
        client = self._start_client(on_volatile=deltas.append)
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: client.connected))

        self.state.append_volatile_event(session, "assistant.delta", {"turnId": 1, "delta": "x"})
        self.state.append_volatile_event(
            session, "thinking.delta", {"turnId": 1, "delta": "y"}, offset=0
        )

        # No offset (cannot gap-check) and out-of-scope types are dropped;
        # thinking.delta still flows to on_event, which ignores it upstream.
        time.sleep(0.3)
        self.assertEqual(deltas, [])

    def test_snapshot_parses_in_flight_assistant_text(self) -> None:
        session = self.state.create_session("s-1")
        session.busy = True
        session.in_flight_turn = {
            "turn_id": 7,
            "assistant_text": "半截回复",
            "thinking_text": "",
            "running_tools": [],
            "current_prompt_id": "p-1",
        }

        snapshot = self.rest.get_snapshot("s-1")

        self.assertTrue(snapshot.in_flight)
        self.assertEqual(snapshot.in_flight_turn_id, 7)
        self.assertEqual(snapshot.in_flight_assistant_text, "半截回复")

    def test_snapshot_parses_pending_question_wire_shape(self) -> None:
        # Audit T4: the fake's question records mirror toWireQuestion
        # (questions[] + synthesized q_<i>/opt_<i>_<o> ids + allow_other), so
        # adapter parsing can never silently yield zero items.
        session = self.state.create_session("s-1")
        self.state.add_pending_question(session, "q-1")

        snapshot = self.rest.get_snapshot("s-1")

        self.assertEqual(len(snapshot.pending_questions), 1)
        view = snapshot.pending_questions[0]
        self.assertEqual(view.question_id, "q-1")
        self.assertEqual(len(view.items), 1)
        item = view.items[0]
        self.assertEqual(item.item_id, "q_0")
        self.assertTrue(item.allow_other)
        self.assertEqual([option.option_id for option in item.options], ["opt_0_0", "opt_0_1"])
        self.assertEqual([option.label for option in item.options], ["是", "否"])

    def test_epoch_rotation_with_lower_seq_fires_resync_immediately(self) -> None:
        # Audit L11: the seq guard used to run before the epoch check, so an
        # epoch rotation whose new seq was below the stored high-water mark
        # never triggered the defensive resync.
        session = self.state.create_session("s-1")
        self.cursors.set("s-1", EventCursor(seq=100, epoch="e-old"))
        client = KapWsClient(
            host="127.0.0.1",
            port=self.ws_server.port,
            token=self.state.token,
            rest_client=self.rest,
            cursor_store=self.cursors,
            on_resync_required=self.resyncs.append,
        )

        client._advance_cursor(
            KapEvent(
                type="turn.started",
                session_id="s-1",
                seq=5,  # lower than the stored watermark, but a NEW epoch
                epoch=session.epoch,
                volatile=False,
                offset=None,
                timestamp="2026-01-01T00:00:00Z",
                payload={},
            )
        )

        self.assertEqual(len(self.resyncs), 1)
        self.assertEqual(self.resyncs[0].session_id, "s-1")
        self.assertEqual(self.resyncs[0].reason, "epoch_changed")
        # The cursor was not advanced into the new epoch's low seq.
        self.assertEqual(self.cursors.get("s-1"), EventCursor(seq=100, epoch="e-old"))

    def test_same_epoch_lower_seq_still_skips(self) -> None:
        # The L11 reorder must not weaken the monotonic guard within one epoch.
        session = self.state.create_session("s-1")
        self.cursors.set("s-1", EventCursor(seq=100, epoch=session.epoch))
        client = KapWsClient(
            host="127.0.0.1",
            port=self.ws_server.port,
            token=self.state.token,
            rest_client=self.rest,
            cursor_store=self.cursors,
            on_resync_required=self.resyncs.append,
        )

        client._advance_cursor(
            KapEvent(
                type="turn.started",
                session_id="s-1",
                seq=5,
                epoch=session.epoch,
                volatile=False,
                offset=None,
                timestamp="2026-01-01T00:00:00Z",
                payload={},
            )
        )

        self.assertEqual(self.resyncs, [])
        self.assertEqual(self.cursors.get("s-1"), EventCursor(seq=100, epoch=session.epoch))

    def test_unsolicited_work_changed_is_ignored(self) -> None:
        # Audit U1 (upstream 0.29.1 d751b6796): event.session.work_changed is
        # fanned out to EVERY connection without a subscription — only the
        # wanted set may advance cursors or feed event tracking.
        subscribed = self.state.create_session("s-1")
        other = self.state.create_session("s-2")
        client = self._start_client()
        client.subscribe("s-1")
        self.assertTrue(wait_until(lambda: client.connected))

        self.state.append_global_event(other, "event.session.work_changed", {"busy": True})

        # The unsolicited session never reaches on_event and never writes a
        # cursor; the wanted session still flows afterwards.
        time.sleep(0.3)
        self.assertEqual(self.events, [])
        self.assertIsNone(self.cursors.get("s-2"))
        self.state.append_event(subscribed, "turn.started", {"turn": 1})
        self.assertTrue(wait_until(lambda: len(self.events) == 1))
        self.assertEqual(self.events[0].session_id, "s-1")


class ToolDisplayDetailTests(unittest.TestCase):
    def test_todo_list_branch(self) -> None:
        from kite.adapters.kap_server import _tool_display_detail

        self.assertEqual(
            _tool_display_detail(
                {
                    "kind": "todo_list",
                    "items": [
                        {"title": "修 bug", "status": "in_progress"},
                        {"title": "写测试", "status": "pending"},
                    ],
                }
            ),
            "修 bug, 写测试",
        )
        self.assertEqual(_tool_display_detail({"kind": "todo_list", "items": "nope"}), "")


if __name__ == "__main__":
    unittest.main()
