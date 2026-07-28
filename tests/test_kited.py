import os
import pathlib
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fake_kap
from kite import file_lock
from kite import kited
from kite.adapters.kap_server import BackoffPolicy, KapError, KapTransportError
from kite.app_handler import AppHandler
from kite.control_plane import (
    ControlClient,
    ControlError,
    control_metadata_path,
    discover_live_control_metadata,
)
from kite.event_pipeline import EventPipeline
from kite.process_utils import process_exists
from kite.prompt_ownership import CERTAINTY_CERTAIN, PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.runtime_status import read_runtime_status
from kite.stores.binding_store import BindingStore
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.group_config_store import GroupConfigStore
from kite.stores.pending_attachment_store import PendingAttachmentStore
from kite.stores.terminal_result_store import TerminalResultStore
from test_app_handler import FakeKapRestClient, FakeTransport

FAKE_KAP_PY = pathlib.Path(fake_kap.__file__).resolve()


def write_fake_kimi(directory: pathlib.Path) -> str:
    shim = directory / "kimi"
    shim.write_text(
        f"#!/bin/sh\nexec {sys.executable} {FAKE_KAP_PY} \"$@\"\n", encoding="utf-8"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(shim)


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class KitedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.kimi_bin = write_fake_kimi(self.root)
        self.home = self.root / "kap-home"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.stop_event = threading.Event()
        self.result: int | None = None
        self.thread = threading.Thread(target=self._run_kited, daemon=True)
        self.child_pids: list[int] = []

    def _run_kited(self) -> None:
        self.result = kited.run(
            kimi_bin=self.kimi_bin,
            home=self.home,
            host="127.0.0.1",
            port=0,
            env_overlay=None,
            data_dir=self.data_dir,
            stop_event=self.stop_event,
            stale_seconds=0.5,
            reconnect_delay_seconds=0.1,
            backoff=BackoffPolicy(base_seconds=0.1, cap_seconds=0.5),
            readiness_timeout_seconds=15.0,
        )

    def _start(self) -> None:
        self.thread.start()
        self.addCleanup(self._stop_and_join)

    def _stop_and_join(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=20)
        for pid in self.child_pids:
            if process_exists(pid):
                os.kill(pid, signal.SIGKILL)

    def _current_kap_pid(self) -> int | None:
        status = read_runtime_status(self.data_dir)
        if not status:
            return None
        kap = status.get("kap")
        pid = kap.get("pid") if isinstance(kap, dict) else None
        return pid if isinstance(pid, int) else None

    def test_supervises_ws_and_shuts_down_cleanly(self) -> None:
        BindingStore(self.data_dir).save(
            "chat-1",
            {"session_id": "s-1", "attached": True,
             "permission_mode": "auto", "plan_mode": False},
        )
        self._start()

        self.assertTrue(wait_until(lambda: self._current_kap_pid() is not None))
        pid = self._current_kap_pid()
        assert pid is not None
        self.child_pids.append(pid)
        self.assertTrue(process_exists(pid))

        # The fake kap serves real WS: kited connects and publishes it.
        self.assertTrue(
            wait_until(
                lambda: bool(
                    (read_runtime_status(self.data_dir) or {})
                    .get("ws", {})
                    .get("connected_at")
                )
            )
        )

        self.stop_event.set()
        self.thread.join(timeout=20)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, 0)
        self.assertFalse(process_exists(pid))
        self.assertIsNone(read_runtime_status(self.data_dir))

    def test_crashed_child_is_restarted_with_a_new_pid(self) -> None:
        self._start()
        self.assertTrue(wait_until(lambda: self._current_kap_pid() is not None))
        first_pid = self._current_kap_pid()
        assert first_pid is not None
        self.child_pids.append(first_pid)
        os.kill(first_pid, signal.SIGKILL)

        self.assertTrue(
            wait_until(
                lambda: self._current_kap_pid() is not None
                and self._current_kap_pid() != first_pid
            )
        )
        second_pid = self._current_kap_pid()
        assert second_pid is not None
        self.child_pids.append(second_pid)
        self.assertTrue(process_exists(second_pid))

        self.stop_event.set()
        self.thread.join(timeout=20)
        self.assertEqual(self.result, 0)
        self.assertFalse(process_exists(second_pid))


class _BlockingStartProc:
    """KapServerProcess double whose readiness wait blocks until stop().

    Simulates the worst case of audit L24: the child never becomes ready
    inside the test window, so the only way startup can finish promptly is
    the stop event aborting the wait.
    """

    def __init__(self, **_kwargs) -> None:
        self.port = None
        self.token = None
        self.pid = None
        self.started = threading.Event()
        self.stop_called = threading.Event()

    def start(self) -> "_BlockingStartProc":
        self.started.set()
        if self.stop_called.wait(timeout=30):
            raise RuntimeError("kimi web exited early rc=-15")
        raise RuntimeError("kimi web not ready within 30s")

    def poll(self) -> int | None:
        return -15 if self.stop_called.is_set() else None

    def stop(self, grace_seconds: float = 10.0) -> int:
        self.stop_called.set()
        return -15


class _InstantProc:
    """KapServerProcess double that is immediately ready."""

    def __init__(self, **_kwargs) -> None:
        self.port = 1
        self.token = "tok"
        self.pid = os.getpid()

    def start(self) -> "_InstantProc":
        return self

    def poll(self) -> None:
        return None

    def stop(self, grace_seconds: float = 10.0) -> int:
        return 0


class _FakeWsClient:
    """KapWsClient double recording subscribes; scripted failures."""

    def __init__(self, **_kwargs) -> None:
        self.subscribed: list[str] = []
        self.stopped = False
        self.fail_on: set[str] = set()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def subscribe(self, session_id: str) -> None:
        self.subscribed.append(session_id)
        if session_id in self.fail_on:
            raise RuntimeError(f"subscribe boom: {session_id}")


class KitedStartupRobustnessTests(KitedRunTests):
    """Audits L24/L25: startup must honor the stop event and survive a
    failing subscribe."""

    def test_readiness_wait_aborts_promptly_on_stop(self) -> None:
        proc = _BlockingStartProc()
        with patch("kite.kited.KapServerProcess", lambda **_kw: proc):
            self._start()
            self.assertTrue(proc.started.wait(timeout=5))
            time.sleep(0.3)  # let run() settle into the readiness wait
            started_at = time.monotonic()
            self.stop_event.set()
            self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, 0)
        self.assertTrue(proc.stop_called.is_set())
        # Prompt, not the (fake) 30s readiness window.
        self.assertLess(time.monotonic() - started_at, 10)

    def test_failed_startup_subscribe_does_not_crash_run(self) -> None:
        store = BindingStore(self.data_dir)
        for chat_id, session_id in (("chat-1", "s-1"), ("chat-2", "s-bad")):
            store.save(
                chat_id,
                {"session_id": session_id, "attached": True,
                 "permission_mode": "auto", "plan_mode": False},
            )
        ws = _FakeWsClient()
        ws.fail_on.add("s-bad")
        with patch("kite.kited.KapServerProcess", lambda **_kw: _InstantProc()), patch(
            "kite.kited.KapWsClient", lambda **_kw: ws
        ), patch("kite.kited._log_server_meta", lambda _rest: None):
            self._start()
            self.assertTrue(wait_until(lambda: "s-bad" in ws.subscribed))
            self.stop_event.set()
            self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())
        # Both subscribes were attempted (the failure was logged, not
        # raised through run()), and the supervision loop exited cleanly.
        self.assertEqual(set(ws.subscribed), {"s-1", "s-bad"})
        self.assertEqual(self.result, 0)
        self.assertTrue(ws.stopped)


class KitedMainTests(unittest.TestCase):
    def test_main_invalid_kap_config_exits_2_cleanly(self) -> None:
        # Audit L23: an invalid `kap:` value (here a non-loopback host) gets
        # the same clean exit-2 as an unusable config, never a traceback.
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config_dir = root / "cfg"
            data_dir = root / "data"
            config_dir.mkdir()
            data_dir.mkdir()
            (config_dir / "system.yaml").write_text(
                "app_id: cli_x\napp_secret: sec\nkap:\n  host: 0.0.0.0\n",
                encoding="utf-8",
            )
            saved_env = {
                key: os.environ.get(key) for key in ("KITE_CONFIG_DIR", "KITE_DATA_ROOT")
            }
            try:
                rc = kited.main(
                    ["--config-dir", str(config_dir), "--data-dir", str(data_dir)]
                )
            finally:
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(rc, 2)


class LegacyConfigLockProbeTests(unittest.TestCase):
    """The lease moved from <config>/kited.lock to <data>/kited.lock
    (46297f3): acquire_instance_lease probes the legacy location so an
    old-version daemon still running in the upgrade window conflicts
    instead of briefly double-driving the instance (audit B7)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.config_dir = self.root / "cfg"
        self.data_dir = self.root / "data"
        self.config_dir.mkdir()
        self.data_dir.mkdir()
        self.legacy_lock = self.config_dir / kited.KITED_LOCK_FILE_NAME
        patcher = patch.dict(os.environ, {"KITE_CONFIG_DIR": str(self.config_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hold_legacy_lock(self):
        handle = open(self.legacy_lock, "a+", encoding="utf-8")
        file_lock.acquire_file_lock(handle, blocking=False)
        self.addCleanup(handle.close)
        return handle

    def test_held_legacy_lock_fails_closed(self) -> None:
        legacy = self._hold_legacy_lock()

        with self.assertRaises(kited.InstanceLeaseError) as ctx:
            kited.acquire_instance_lease(self.data_dir)

        message = str(ctx.exception)
        self.assertIn("old-version kited", message)
        self.assertIn("stop the old daemon", message)
        self.assertIn(str(self.legacy_lock), message)
        # The failed probe closed its own data-dir lease handle: once the
        # old daemon is gone, the start proceeds (and cleans up the file).
        legacy.close()
        handle = kited.acquire_instance_lease(self.data_dir)
        self.addCleanup(handle.close)
        self.assertFalse(self.legacy_lock.exists())

    def test_stale_legacy_lock_is_acquired_and_cleaned_up(self) -> None:
        self.legacy_lock.write_text("999999\n", encoding="utf-8")

        handle = kited.acquire_instance_lease(self.data_dir)
        self.addCleanup(handle.close)

        self.assertFalse(self.legacy_lock.exists())
        # The data-dir lease itself is held normally.
        with self.assertRaises(kited.InstanceLeaseError):
            kited.acquire_instance_lease(self.data_dir)

    def test_no_legacy_lock_file_is_a_noop(self) -> None:
        handle = kited.acquire_instance_lease(self.data_dir)
        self.addCleanup(handle.close)

        self.assertFalse(self.legacy_lock.exists())
        self.assertTrue((self.data_dir / kited.KITED_LOCK_FILE_NAME).exists())


CONTROL_TOKEN = "test-control-token"


class _FakePipeline:
    """Duck-typed stand-in for EventPipeline in run() wiring tests."""

    def __init__(self) -> None:
        self.shutdown_called = False
        self.recovered: list[str] | None = None

    def set_snapshot_rebuilt_hook(self, hook) -> None:
        self.snapshot_hook = hook

    def startup_recovery(self, sessions) -> None:
        self.recovered = list(sessions)

    def shutdown(self) -> None:
        self.shutdown_called = True

    def handle_event(self, event) -> None:
        pass

    def handle_resync_required(self, request) -> None:
        pass

    def handle_error_frame(self, frame) -> None:
        pass

    def handle_volatile(self, delta) -> None:
        pass


class _FakeSwap:
    """Duck-typed stand-in for SwappableKapRest / WsSubscriptionHook."""

    def __init__(self) -> None:
        self.client = None

    def set_client(self, client) -> None:
        self.client = client


class _FakeControlHandler:
    """Duck-typed handler: records control-plane submits, returns canned data."""

    def __init__(self, submit_calls: list[dict]) -> None:
        self.submit_calls = submit_calls

    def rebuild_prompt_ownership(self) -> None:
        pass

    def close(self) -> None:
        pass

    def submit_prompt_control(self, params: dict) -> dict:
        self.submit_calls.append(dict(params))
        return {
            "prompt_id": "p-ctl",
            "session_id": params.get("session_id") or "s-x",
            "status": "running",
            "owner_recorded": True,
        }


class KitedControlPlaneTests(KitedRunTests):
    """run() starts the loopback control plane with the outbound runtime and
    stops it (unpublishing the metadata) on shutdown."""

    def setUp(self) -> None:
        super().setUp()
        self.submit_calls: list[dict] = []
        self.fake_outbound = SimpleNamespace(
            runtime_loop=RuntimeLoop(name="test-runtime"),
            transport_thread=threading.Thread(target=lambda: None, daemon=True),
            transport=SimpleNamespace(
                fetch_bot_open_id=lambda: "ou-fake-bot",
                set_bot_open_id=lambda open_id: None,
            ),
            pipeline=_FakePipeline(),
            rest_proxy=_FakeSwap(),
            ws_hook=_FakeSwap(),
            handler=_FakeControlHandler(self.submit_calls),
        )

    def _run_kited(self) -> None:
        self.result = kited.run(
            kimi_bin=self.kimi_bin,
            home=self.home,
            host="127.0.0.1",
            port=0,
            env_overlay=None,
            data_dir=self.data_dir,
            stop_event=self.stop_event,
            stale_seconds=0.5,
            reconnect_delay_seconds=0.1,
            backoff=BackoffPolicy(base_seconds=0.1, cap_seconds=0.5),
            readiness_timeout_seconds=15.0,
            outbound=self.fake_outbound,
            control_token=CONTROL_TOKEN,
        )

    def _wait_for_control_plane(self) -> int:
        self.assertTrue(
            wait_until(lambda: discover_live_control_metadata(self.data_dir) is not None)
        )
        metadata = discover_live_control_metadata(self.data_dir)
        assert metadata is not None
        return metadata.port

    def test_control_plane_serves_prompt_submit_while_running(self) -> None:
        self._start()
        port = self._wait_for_control_plane()

        data = ControlClient(port=port, token=CONTROL_TOKEN).request(
            "prompt/submit", {"session_id": "s-9", "text": "hi"}
        )

        self.assertEqual(data["prompt_id"], "p-ctl")
        self.assertEqual(data["owner_recorded"], True)
        self.assertEqual(self.submit_calls, [{"session_id": "s-9", "text": "hi"}])

        self.stop_event.set()
        self.thread.join(timeout=20)
        self.assertEqual(self.result, 0)
        # Shutdown unpublishes the endpoint: a stopped daemon is undiscoverable.
        self.assertFalse(control_metadata_path(self.data_dir).exists())
        self.assertIsNone(discover_live_control_metadata(self.data_dir))

    def test_control_plane_rejects_wrong_token(self) -> None:
        self._start()
        port = self._wait_for_control_plane()

        with self.assertRaises(ControlError) as ctx:
            ControlClient(port=port, token="wrong-token").request(
                "prompt/submit", {"session_id": "s-9", "text": "hi"}
            )

        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertEqual(self.submit_calls, [])


class ControlPlaneSubmitTests(unittest.TestCase):
    """The kited-side prompt/submit endpoint discipline.

    A real AppHandler + BindingStore + RuntimeLoop + PromptOwnership over the
    scriptable fake kap REST: the same submit discipline as the
    Feishu-originated path, minus the Feishu ack.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = BindingStore(self.data_dir)
        self.transport = FakeTransport()
        self.rest = FakeKapRestClient()
        self.ownership = PromptOwnership()
        self.loop = RuntimeLoop(name="test-runtime")
        self.addCleanup(self.loop.stop)
        self.handler = AppHandler(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=GroupConfigStore(self.data_dir),
            runtime_loop=self.loop,
            config={"admin_open_ids": ["ou_admin"], "default_working_dir": "/work"},
            init_token="test-init-token",
            prompt_model="k2-think",
            prompt_ownership=self.ownership,
            persist_admins=lambda ids: None,
        )
        self.dispatch = kited._control_dispatch(SimpleNamespace(handler=self.handler))

    def _bind(
        self,
        chat_id: str,
        session_id: str,
        *,
        attached: bool = True,
        permission_mode: str = "auto",
        plan_mode: bool = False,
    ) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
            },
        )

    def _submit(self, params: dict) -> dict:
        return self.dispatch("prompt/submit", params)

    def _last_submission(self) -> dict:
        assert self.rest.submissions, "expected a prompt submission"
        return self.rest.submissions[-1]

    def test_chat_path_submits_with_binding_modes_and_records_ownership(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1", permission_mode="manual", plan_mode=True)

        result = self._submit({"chat_id": "chat-1", "text": "hello kite"})

        self.assertEqual(
            result,
            {
                "prompt_id": "p-1",
                "session_id": "s-1",
                "status": "running",
                "owner_recorded": True,
            },
        )
        submission = self._last_submission()
        self.assertEqual(submission["session_id"], "s-1")
        body = submission["body"]
        self.assertEqual(body["content"], [{"type": "text", "text": "hello kite"}])
        self.assertEqual(body["permission_mode"], "manual")
        self.assertEqual(body["plan_mode"], True)
        # The daemon's resolved model is carried per prompt (kite-design §7).
        self.assertEqual(body["model"], "k2-think")
        # Ownership lands as certain, exactly like the Feishu path.
        self.assertEqual(self.ownership.owner_of("p-1"), "chat-1")
        self.assertEqual(self.ownership.certainty_of("p-1"), CERTAINTY_CERTAIN)
        # No Feishu ack: the response traveled back over the control channel.
        self.assertEqual(self.transport.replies, [])

    def test_session_path_uses_defaults_and_records_no_ownership(self) -> None:
        self.rest.add_session("s-1")

        result = self._submit({"session_id": "s-1", "text": "hi"})

        self.assertEqual(result["owner_recorded"], False)
        body = self._last_submission()["body"]
        self.assertEqual(body["permission_mode"], "auto")
        self.assertEqual(body["plan_mode"], False)
        self.assertEqual(body["model"], "k2-think")
        self.assertEqual(len(self.ownership), 0)

    def test_explicit_mode_params_override_the_defaults(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1", permission_mode="manual", plan_mode=False)

        self._submit(
            {
                "chat_id": "chat-1",
                "text": "hi",
                "permission_mode": "yolo",
                "plan_mode": True,
            }
        )

        body = self._last_submission()["body"]
        self.assertEqual(body["permission_mode"], "yolo")
        self.assertEqual(body["plan_mode"], True)

    def test_validation_errors(self) -> None:
        self.rest.add_session("s-1")
        cases = [
            ({"session_id": "s-1", "text": "  "}, "invalid_params"),
            ({"text": "hi"}, "invalid_params"),
            ({"chat_id": "c", "session_id": "s-1", "text": "hi"}, "invalid_params"),
            ({"session_id": "s-1", "text": "hi", "permission_mode": "bogus"}, "invalid_params"),
            ({"session_id": "s-1", "text": "hi", "plan_mode": "yes"}, "invalid_params"),
        ]
        for params, code in cases:
            with self.subTest(params=params):
                with self.assertRaises(ControlError) as ctx:
                    self._submit(params)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.rest.submissions, [])

    def test_unbound_chat_fails_closed(self) -> None:
        with self.assertRaises(ControlError) as ctx:
            self._submit({"chat_id": "chat-ghost", "text": "hi"})
        self.assertEqual(ctx.exception.code, "no_binding")
        self.assertEqual(self.rest.submissions, [])

    def test_detached_chat_fails_closed(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1", attached=False)

        with self.assertRaises(ControlError) as ctx:
            self._submit({"chat_id": "chat-1", "text": "hi"})
        self.assertEqual(ctx.exception.code, "chat_detached")
        self.assertEqual(self.rest.submissions, [])

    def test_archived_session_is_never_resurrected(self) -> None:
        self.rest.add_session("s-1", archived=True)
        self._bind("chat-1", "s-1")

        with self.assertRaises(ControlError) as ctx:
            self._submit({"chat_id": "chat-1", "text": "hi"})
        self.assertEqual(ctx.exception.code, "session_archived")
        self.assertEqual(self.rest.submissions, [])

    def test_unknown_session_surfaces_the_kap_error_code(self) -> None:
        with self.assertRaises(ControlError) as ctx:
            self._submit({"session_id": "s-ghost", "text": "hi"})
        self.assertEqual(ctx.exception.code, "40401")
        self.assertIn("does not exist", ctx.exception.msg)

    def test_unreachable_kap_is_a_structured_error(self) -> None:
        self.rest.get_session_error = KapTransportError("connection refused")

        with self.assertRaises(ControlError) as ctx:
            self._submit({"session_id": "s-1", "text": "hi"})
        self.assertEqual(ctx.exception.code, "kap_unreachable")

    def test_submit_business_error_passes_through(self) -> None:
        self.rest.add_session("s-1")
        self.rest.submit_error = KapError(40001, "bad prompt")

        with self.assertRaises(ControlError) as ctx:
            self._submit({"session_id": "s-1", "text": "hi"})
        self.assertEqual(ctx.exception.code, "40001")
        self.assertEqual(ctx.exception.msg, "bad prompt")
        self.assertEqual(len(self.ownership), 0)

    def test_blocked_submit_is_an_error_and_records_no_ownership(self) -> None:
        self.rest.add_session("s-1")
        self.rest.submit_status = "blocked"
        self._bind("chat-1", "s-1")

        with self.assertRaises(ControlError) as ctx:
            self._submit({"chat_id": "chat-1", "text": "hi"})
        self.assertEqual(ctx.exception.code, "submit_blocked")
        self.assertEqual(len(self.ownership), 0)

    def test_announce_sends_a_trigger_notice_before_submitting(self) -> None:
        """Scheduled-prompts contract §4.2: display=announce sends one short
        Chinese trigger notice to the target chat BEFORE the prompt submit."""
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1")
        events: list[str] = []
        original_reply = self.transport.reply

        def reply_spy(chat_id, text, **kwargs):
            events.append("notice")
            return original_reply(chat_id, text, **kwargs)

        original_call = self.rest.call

        def call_spy(method, path, body=None):
            if str(path).endswith("/prompts"):
                events.append("submit")
            return original_call(method, path, body)

        self.transport.reply = reply_spy
        self.rest.call = call_spy

        result = self._submit({"chat_id": "chat-1", "text": "hello kite", "display": "announce"})

        self.assertEqual(events, ["notice", "submit"])
        self.assertEqual(result["owner_recorded"], True)
        self.assertEqual(len(self.transport.replies), 1)
        notice = self.transport.replies[0]
        self.assertEqual(notice["chat_id"], "chat-1")
        self.assertIn("⏰ 定时任务触发", notice["text"])
        self.assertIn("hello kite", notice["text"])

    def test_announce_notice_collapses_and_truncates_the_snippet(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1")
        text = "  multi\n\n spaced   " + "x" * 80

        self._submit({"chat_id": "chat-1", "text": text, "display": "announce"})

        notice = self.transport.replies[0]["text"]
        self.assertTrue(notice.startswith("⏰ 定时任务触发：multi spaced "))
        self.assertTrue(notice.endswith("…"))
        self.assertLessEqual(len(notice), len("⏰ 定时任务触发：") + 50)

    def test_silent_display_sends_no_notice(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1")

        self._submit({"chat_id": "chat-1", "text": "hi", "display": "silent"})

        self.assertEqual(self.transport.replies, [])
        self.assertEqual(len(self.rest.submissions), 1)

    def test_announce_requires_a_chat_target(self) -> None:
        self.rest.add_session("s-1")

        with self.assertRaises(ControlError) as ctx:
            self._submit({"session_id": "s-1", "text": "hi", "display": "announce"})
        self.assertEqual(ctx.exception.code, "invalid_params")
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual(self.transport.replies, [])

    def test_display_validation_rejects_garbage(self) -> None:
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1")
        for display in ("loud", True, 1):
            with self.subTest(display=display):
                with self.assertRaises(ControlError) as ctx:
                    self._submit({"chat_id": "chat-1", "text": "hi", "display": display})
                self.assertEqual(ctx.exception.code, "invalid_params")
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual(self.transport.replies, [])

    def test_unknown_method_is_a_structured_error(self) -> None:
        with self.assertRaises(ControlError) as ctx:
            self.dispatch("bogus/method", {})
        self.assertEqual(ctx.exception.code, "unknown_method")

    def test_cli_sent_prompt_lands_in_the_ownership_map_the_pipeline_reads(self) -> None:
        """Regression: before the control plane, `kitectl prompt send` wrote
        straight to kap REST, so the daemon's ownership map never learned the
        owner and approvals from CLI-sent prompts took the fail-closed
        expired-card path. The control-plane submit must record into the SAME
        PromptOwnership instance the outbound pipeline reads (the split-map
        hazard of 2026-07-22 applies here too: one shared instance, wired
        exactly like build_outbound_runtime).
        """
        pipeline = EventPipeline(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            terminal_store=TerminalResultStore(self.data_dir),
            ownership=self.ownership,
            runtime_loop=self.loop,
            cursor_store=EventCursorStore(self.data_dir),
            approval_timeout_seconds=300,
            question_timeout_seconds=300,
        )
        self.rest.add_session("s-1")
        self._bind("chat-1", "s-1")

        result = self._submit({"chat_id": "chat-1", "text": "hello kite"})

        self.assertIs(pipeline._ownership, self.ownership)
        entry = pipeline._ownership.entry_of(result["prompt_id"])
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.chat_id, "chat-1")
        self.assertEqual(entry.certainty, CERTAINTY_CERTAIN)


class ControlPlaneImageSendTests(unittest.TestCase):
    """The kited-side image/send endpoint discipline (images contract §3).

    A real AppHandler + BindingStore + RuntimeLoop over the scriptable fake
    transport: upload-once fan-out to every attached chat of the session,
    per-chat failure isolation, and fail-closed validation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = BindingStore(self.data_dir)
        self.transport = FakeTransport()
        self.rest = FakeKapRestClient()
        self.loop = RuntimeLoop(name="test-runtime")
        self.addCleanup(self.loop.stop)
        self.handler = self._make_handler()
        self.dispatch = kited._control_dispatch(SimpleNamespace(handler=self.handler))
        self.image_path = self.data_dir / "img.png"
        self.image_path.write_bytes(b"\x89PNG-fake")

    def _make_handler(self, **config_overrides) -> AppHandler:
        config = {"admin_open_ids": ["ou_admin"], "default_working_dir": "/work"}
        config.update(config_overrides)
        return AppHandler(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=GroupConfigStore(self.data_dir),
            runtime_loop=self.loop,
            config=config,
            init_token="test-init-token",
            persist_admins=lambda ids: None,
        )

    def _bind(self, chat_id: str, session_id: str, *, attached: bool = True) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": "auto",
                "plan_mode": False,
            },
        )

    def _send(self, params: dict) -> dict:
        return self.dispatch("image/send", params)

    def test_fanout_uploads_once_and_delivers_to_attached_chats(self) -> None:
        self._bind("chat-1", "s-1")
        self._bind("chat-2", "s-1")
        self._bind("chat-3", "s-1", attached=False)  # detached: not a target
        self._bind("chat-4", "s-2")  # other session: not a target

        result = self._send({"chat_id": "chat-1", "path": str(self.image_path)})

        self.assertEqual(result["session_id"], "s-1")
        self.assertEqual(result["image_key"], "img_key_fake")
        self.assertEqual(result["failed"], [])
        self.assertEqual(
            [item["chat_id"] for item in result["delivered"]], ["chat-1", "chat-2"]
        )
        # Upload happened exactly once; sends fanned out per chat.
        self.assertEqual(self.transport.uploads, [str(self.image_path)])
        self.assertEqual(
            self.transport.sent_images,
            [("chat-1", "img_key_fake"), ("chat-2", "img_key_fake")],
        )

    def test_one_failing_chat_is_isolated_in_the_result(self) -> None:
        self._bind("chat-1", "s-1")
        self._bind("chat-2", "s-1")
        self.transport.fail_image_chats.add("chat-2")

        result = self._send({"chat_id": "chat-1", "path": str(self.image_path)})

        self.assertEqual([item["chat_id"] for item in result["delivered"]], ["chat-1"])
        self.assertEqual(
            result["failed"], [{"chat_id": "chat-2", "error": "send_failed"}]
        )

    def test_upload_failure_raises_and_sends_nothing(self) -> None:
        self._bind("chat-1", "s-1")
        self.transport.upload_result = None

        with self.assertRaises(ControlError) as ctx:
            self._send({"chat_id": "chat-1", "path": str(self.image_path)})

        self.assertEqual(ctx.exception.code, "upload_failed")
        self.assertEqual(self.transport.sent_images, [])

    def test_validation_errors(self) -> None:
        self._bind("chat-1", "s-1")
        cases = [
            ({"path": str(self.image_path)}, "invalid_params"),
            ({"chat_id": "chat-1"}, "invalid_params"),
            ({"chat_id": "chat-ghost", "path": str(self.image_path)}, "no_binding"),
            ({"chat_id": "chat-1", "path": str(self.data_dir / "gone.png")}, "invalid_path"),
            ({"chat_id": "chat-1", "path": str(self.data_dir)}, "invalid_path"),
        ]
        for params, code in cases:
            with self.subTest(params=params):
                with self.assertRaises(ControlError) as ctx:
                    self._send(params)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.transport.uploads, [])

    def test_over_cap_image_is_rejected(self) -> None:
        self.handler = self._make_handler(attachment_max_bytes=4)
        self.dispatch = kited._control_dispatch(SimpleNamespace(handler=self.handler))
        self._bind("chat-1", "s-1")

        with self.assertRaises(ControlError) as ctx:
            self._send({"chat_id": "chat-1", "path": str(self.image_path)})

        self.assertEqual(ctx.exception.code, "image_too_large")
        self.assertIn("byte cap", ctx.exception.msg)
        self.assertEqual(self.transport.uploads, [])

    def test_no_attached_chat_is_an_error(self) -> None:
        self._bind("chat-1", "s-1", attached=False)

        with self.assertRaises(ControlError) as ctx:
            self._send({"chat_id": "chat-1", "path": str(self.image_path)})

        self.assertEqual(ctx.exception.code, "no_targets")
        self.assertEqual(self.transport.uploads, [])


if __name__ == "__main__":
    unittest.main()
