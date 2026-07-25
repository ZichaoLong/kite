import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import fake_kap
from kite import kitectl
from kite import service_manager
from kite.control_plane import ControlError, ControlPlaneServer
from kite.runtime_status import RuntimeStatusWriter
from kite.stores.binding_store import BindingStore
from test_control_plane import DropConnectionServer, dead_pid, unused_port


class KitectlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.home = self.root / "kap-home"
        self.config_dir.mkdir()
        self.data_dir.mkdir()
        (self.home / "server" / "instances").mkdir(parents=True)
        (self.home / "server.token").write_text(f"{fake_kap.FAKE_TOKEN}\n", encoding="utf-8")

        self.state = fake_kap.FakeKapState()
        self.rest_server = fake_kap.make_rest_server(self.state)
        self.addCleanup(self.rest_server.shutdown)
        self.rest_port = self.rest_server.server_address[1]
        self._write_config(port=self.rest_port)

    def _write_config(self, *, port: int) -> None:
        (self.config_dir / "system.yaml").write_text(
            "kap:\n"
            "  host: 127.0.0.1\n"
            f"  port: {port}\n"
            f"  home: {self.home}\n",
            encoding="utf-8",
        )

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kitectl.main(
                ["--config-dir", str(self.config_dir), "--data-dir", str(self.data_dir), *argv]
            )
        return code, stdout.getvalue(), stderr.getvalue()


class SessionListTests(KitectlTestCase):
    def test_lists_sessions_with_title_cwd_busy(self) -> None:
        session = self.state.create_session("s-abc", title="demo", cwd="/work/demo")
        session.busy = True
        self.state.create_session("s-def", title="idle")

        code, out, _ = self._run_cli("session", "list")

        self.assertEqual(code, 0)
        self.assertIn("SESSION_ID", out)
        self.assertIn("s-abc", out)
        self.assertIn("demo", out)
        self.assertIn("/work/demo", out)
        self.assertIn("yes", out)
        self.assertIn("s-def", out)

    def test_lists_sessions_sorted_by_recent_activity(self) -> None:
        old = self.state.create_session("s-old", title="old")
        old.updated_at = "2026-07-01T00:00:00Z"
        new = self.state.create_session("s-new", title="new")
        new.updated_at = "2026-07-25T00:00:00Z"

        code, out, _ = self._run_cli("session", "list")

        self.assertEqual(code, 0)
        self.assertLess(out.index("s-new"), out.index("s-old"))

    def test_empty_session_list(self) -> None:
        code, out, _ = self._run_cli("session", "list")
        self.assertEqual(code, 0)
        self.assertIn("(no sessions)", out)


class SessionStatusTests(KitectlTestCase):
    def _bind(self, chat_id: str, session_id: str) -> None:
        store = BindingStore(self.data_dir)
        store.save(
            chat_id,
            {"session_id": session_id, "attached": True,
             "permission_mode": "auto", "plan_mode": False},
        )

    def test_status_shows_bindings_queue_and_daemon(self) -> None:
        session = self.state.create_session("s-abc", title="demo", cwd="/work/demo")
        session.busy = True
        session.active_prompt = "p-1"
        session.queued_prompts = ["p-2", "p-3"]
        self._bind("chat-1", "s-abc")
        status = RuntimeStatusWriter(self.data_dir)
        status.update(
            kap={"pid": 4242, "port": self.rest_port},
            ws={"connected_at": time.time() - 65, "last_resync_at": time.time() - 5},
        )

        code, out, _ = self._run_cli("session", "status")

        self.assertEqual(code, 0)
        self.assertIn("chat-1", out)
        self.assertIn("s-abc", out)
        self.assertIn("QUEUE", out)
        # queue depth 2 and active prompt p-1 on the sessions table row
        row = next(line for line in out.splitlines() if line.startswith("s-abc"))
        self.assertIn("p-1", row)
        self.assertRegex(row, r"\b2\b")
        self.assertIn("yes", row)
        self.assertIn("kited: running", out)
        self.assertIn("WS: connected (age 1m", out)
        self.assertIn("last resync:", out)
        self.assertNotIn("last resync: never", out)

    def test_status_without_kited_runtime_status(self) -> None:
        self.state.create_session("s-abc", title="demo")
        self._bind("chat-1", "s-abc")

        code, out, _ = self._run_cli("session", "status")

        self.assertEqual(code, 0)
        self.assertIn("kited: not running", out)

    def test_status_without_bindings(self) -> None:
        code, out, _ = self._run_cli("session", "status")
        self.assertEqual(code, 0)
        self.assertIn("(no bindings)", out)
        self.assertIn("(no bound sessions)", out)

    def test_status_prefers_live_registry_port(self) -> None:
        # Config points at a dead port; the registry points at the live one.
        self._write_config(port=1)
        (self.home / "server" / "instances" / "srv-live.json").write_text(
            json.dumps({"pid": os.getpid(), "port": self.rest_port}), encoding="utf-8"
        )
        self.state.create_session("s-abc", title="demo")
        self._bind("chat-1", "s-abc")

        code, out, _ = self._run_cli("session", "status")

        self.assertEqual(code, 0)
        self.assertIn("s-abc", out)


class ErrorPathTests(KitectlTestCase):
    def test_missing_token_is_fail_closed(self) -> None:
        (self.home / "server.token").unlink()

        code, _, err = self._run_cli("session", "list")

        self.assertEqual(code, 2)
        self.assertIn("is kited running?", err)

    def test_unreachable_server_is_fail_closed(self) -> None:
        self.rest_server.shutdown()
        self.rest_server.server_close()

        code, _, err = self._run_cli("session", "list")

        self.assertEqual(code, 2)
        self.assertIn("cannot reach kap-server", err)

    def test_business_error_is_fail_closed(self) -> None:
        self._write_config(port=self.rest_port)
        # Wrong token: auth fails with 40101 on the server side.
        (self.home / "server.token").write_text("wrong-token\n", encoding="utf-8")

        code, _, err = self._run_cli("session", "list")

        self.assertEqual(code, 1)
        self.assertIn("kap-server error 40101", err)


class _RecordingServiceManager:
    """ServiceManager double: records actions, never touches real systemd."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.definitions: list[object] = []
        self.status_result = service_manager.ServiceStatus(
            installed=True, running=True, source="fake-status", detail="active"
        )
        self.autostart_result = service_manager.AutostartStatus(
            enabled=True, source="fake-autostart", detail="enabled"
        )
        self.error: Exception | None = None

    def display_name(self, definition: object) -> str:
        return definition.identifier

    def _record(self, action: str, definition: object) -> None:
        self.calls.append(action)
        self.definitions.append(definition)
        if self.error is not None:
            raise self.error

    def ensure_service(self, definition: object) -> None:
        self._record("ensure_service", definition)

    def uninstall(self, definition: object) -> None:
        self._record("uninstall", definition)

    def start(self, definition: object) -> None:
        self._record("start", definition)

    def stop(self, definition: object) -> None:
        self._record("stop", definition)

    def restart(self, definition: object) -> None:
        self._record("restart", definition)

    def status(self, definition: object) -> service_manager.ServiceStatus:
        self._record("status", definition)
        return self.status_result

    def autostart_enable(self, definition: object) -> None:
        self._record("autostart_enable", definition)

    def autostart_disable(self, definition: object) -> None:
        self._record("autostart_disable", definition)

    def autostart_status(self, definition: object) -> service_manager.AutostartStatus:
        self._record("autostart_status", definition)
        return self.autostart_result


class ServiceCommandTests(KitectlTestCase):
    def _fake_manager(self) -> _RecordingServiceManager:
        manager = _RecordingServiceManager()
        patcher = patch("kite.service_manager.current_service_manager", return_value=manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager

    def test_install_writes_definition_without_starting(self) -> None:
        manager = self._fake_manager()

        code, out, _ = self._run_cli("service", "install")

        self.assertEqual(code, 0)
        self.assertEqual(manager.calls, ["ensure_service"])
        self.assertIn("service 'kite' installed", out)
        self.assertIn("not started", out)
        definition = manager.definitions[0]
        self.assertEqual(definition.config_dir, self.config_dir)
        self.assertEqual(definition.data_dir, self.data_dir)
        self.assertEqual(definition.stdout_log_path, self.data_dir / "service.stdout.log")

    def test_start_stop_restart_uninstall(self) -> None:
        manager = self._fake_manager()
        done_words = {
            "start": "started",
            "stop": "stopped",
            "restart": "restarted",
            "uninstall": "uninstalled",
        }
        for action, done in done_words.items():
            with self.subTest(action=action):
                code, out, _ = self._run_cli("service", action)
                self.assertEqual(code, 0)
                self.assertIn(f"service 'kite' {done}", out)
        self.assertEqual(manager.calls, list(done_words))

    def test_status_prints_state(self) -> None:
        self._fake_manager()

        code, out, _ = self._run_cli("service", "status")

        self.assertEqual(code, 0)
        self.assertIn("service: kite", out)
        self.assertIn("installed: yes", out)
        self.assertIn("running: yes", out)
        self.assertIn("detail: active", out)

    def test_status_not_installed(self) -> None:
        manager = self._fake_manager()
        manager.status_result = service_manager.ServiceStatus(
            installed=False, running=False, detail="unit file missing"
        )

        code, out, _ = self._run_cli("service", "status")

        self.assertEqual(code, 0)
        self.assertIn("installed: no", out)
        self.assertIn("running: no", out)

    def test_autostart_enable_disable(self) -> None:
        manager = self._fake_manager()

        code, out, _ = self._run_cli("service", "autostart", "enable")
        self.assertEqual(code, 0)
        self.assertIn("autostart enabled", out)
        code, out, _ = self._run_cli("service", "autostart", "disable")
        self.assertEqual(code, 0)
        self.assertIn("autostart disabled", out)
        self.assertEqual(manager.calls, ["autostart_enable", "autostart_disable"])

    def test_autostart_status_prints_state(self) -> None:
        self._fake_manager()

        code, out, _ = self._run_cli("service", "autostart", "status")

        self.assertEqual(code, 0)
        self.assertIn("autostart: enabled", out)
        self.assertIn("detail: enabled", out)

    def test_manager_failure_is_a_clear_error(self) -> None:
        manager = self._fake_manager()
        manager.error = service_manager.ServiceManagerError("systemctl exploded")

        code, _, err = self._run_cli("service", "start")

        self.assertEqual(code, 1)
        self.assertIn("systemctl exploded", err)


class ServicePreviewGateTests(KitectlTestCase):
    """stop/restart preview gate: busy or unverifiable live state is
    --force-only (never silently available)."""

    def _fake_manager(self) -> _RecordingServiceManager:
        manager = _RecordingServiceManager()
        patcher = patch("kite.service_manager.current_service_manager", return_value=manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager

    def test_stop_refused_when_session_busy_without_force(self) -> None:
        manager = self._fake_manager()
        session = self.state.create_session("s-busy", title="demo")
        session.busy = True

        code, _, err = self._run_cli("service", "stop")

        self.assertEqual(code, 2)
        self.assertIn("1 session(s) busy, 0 pending interaction(s)", err)
        self.assertIn("stopping kills in-flight prompts", err)
        self.assertIn("--force", err)
        self.assertEqual(manager.calls, [])

    def test_stop_with_force_proceeds_when_busy(self) -> None:
        manager = self._fake_manager()
        session = self.state.create_session("s-busy", title="demo")
        session.busy = True

        code, out, _ = self._run_cli("service", "stop", "--force")

        self.assertEqual(code, 0)
        self.assertEqual(manager.calls, ["stop"])
        self.assertIn("warning:", out)
        self.assertIn("1 session(s) busy, 0 pending interaction(s)", out)
        self.assertIn("proceeding anyway (--force)", out)

    def test_restart_refused_with_pending_interaction(self) -> None:
        manager = self._fake_manager()
        session = self.state.create_session("s-pending", title="demo")
        session.pending_interaction = "approval"

        code, _, err = self._run_cli("service", "restart")

        self.assertEqual(code, 2)
        self.assertIn("0 session(s) busy, 1 pending interaction(s)", err)
        self.assertIn("restarting kills in-flight prompts", err)
        self.assertEqual(manager.calls, [])

    def test_restart_refused_when_live_state_unverifiable(self) -> None:
        manager = self._fake_manager()
        self.rest_server.shutdown()
        self.rest_server.server_close()

        code, _, err = self._run_cli("service", "restart")

        self.assertEqual(code, 2)
        self.assertIn("cannot verify live state", err)
        self.assertIn("--force", err)
        self.assertEqual(manager.calls, [])

    def test_restart_with_force_proceeds_when_unverifiable(self) -> None:
        manager = self._fake_manager()
        self.rest_server.shutdown()
        self.rest_server.server_close()

        code, out, _ = self._run_cli("service", "restart", "--force")

        self.assertEqual(code, 0)
        self.assertEqual(manager.calls, ["restart"])
        self.assertIn("warning:", out)
        self.assertIn("cannot verify live state", out)

    def test_clean_live_state_proceeds_without_force(self) -> None:
        manager = self._fake_manager()
        self.state.create_session("s-idle", title="demo")

        code, out, _ = self._run_cli("service", "restart")

        self.assertEqual(code, 0)
        self.assertEqual(manager.calls, ["restart"])
        self.assertNotIn("warning:", out)

    def test_log_tails_stdout_log(self) -> None:
        (self.data_dir / "service.stdout.log").write_text(
            "".join(f"line-{i}\n" for i in range(1, 11)), encoding="utf-8"
        )

        code, out, _ = self._run_cli("service", "log", "-n", "3")

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["line-8", "line-9", "line-10"])

    def test_log_defaults_to_fifty_lines(self) -> None:
        (self.data_dir / "service.stdout.log").write_text(
            "".join(f"line-{i}\n" for i in range(1, 101)), encoding="utf-8"
        )

        code, out, _ = self._run_cli("service", "log")

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 50)
        self.assertEqual(lines[0], "line-51")
        self.assertEqual(lines[-1], "line-100")

    def test_log_missing_file_is_fail_closed(self) -> None:
        code, _, err = self._run_cli("service", "log")

        self.assertEqual(code, 2)
        self.assertIn("no service stdout log", err)

    def test_log_rejects_non_positive_count(self) -> None:
        code, _, err = self._run_cli("service", "log", "-n", "0")

        self.assertEqual(code, 2)
        self.assertIn("positive integer", err)


class BindingListTests(KitectlTestCase):
    def _bind(
        self,
        chat_id: str,
        session_id: str,
        *,
        attached: bool = True,
        permission_mode: str = "auto",
        plan_mode: bool = False,
    ) -> None:
        BindingStore(self.data_dir).save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
            },
        )

    def test_lists_bindings_sorted_with_modes(self) -> None:
        self._bind("chat-b", "s-2", attached=False, permission_mode="yolo", plan_mode=True)
        self._bind("chat-a", "s-1", permission_mode="manual")

        code, out, _ = self._run_cli("binding", "list")

        self.assertEqual(code, 0)
        self.assertIn("CHAT_ID", out)
        self.assertIn("SESSION_ID", out)
        self.assertIn("ATTACHED", out)
        self.assertIn("MODE", out)
        self.assertIn("PLAN", out)
        lines = out.splitlines()
        row_a = next(line for line in lines if line.startswith("chat-a"))
        row_b = next(line for line in lines if line.startswith("chat-b"))
        self.assertLess(lines.index(row_a), lines.index(row_b))
        self.assertIn("s-1", row_a)
        self.assertIn("manual", row_a)
        self.assertIn("no", row_b)
        self.assertIn("yolo", row_b)
        self.assertIn("on", row_b)

    def test_empty_binding_list(self) -> None:
        code, out, _ = self._run_cli("binding", "list")

        self.assertEqual(code, 0)
        self.assertIn("(no bindings)", out)


class PromptSendTests(KitectlTestCase):
    """`prompt send` is a client of kited's loopback control plane.

    The daemon side is faked with a real ControlPlaneServer over a scripted
    dispatch; wire-protocol details are covered in test_control_plane.py and
    the submit discipline in test_kited.py.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.config_dir / "control.token").write_text(
            "test-control-token\n", encoding="utf-8"
        )
        self.received: list[tuple[str, dict]] = []
        self.dispatch_error: Exception | None = None
        self.dispatch_response: dict = {
            "prompt_id": "p-1",
            "session_id": "s-abc",
            "status": "running",
            "owner_recorded": True,
        }
        self.control_server = ControlPlaneServer(
            data_dir=self.data_dir,
            dispatch=self._dispatch,
            auth_token=lambda: "test-control-token",
        )
        self.control_server.start()
        self.addCleanup(self.control_server.stop)

    def _dispatch(self, method: str, params: dict) -> dict:
        self.received.append((method, params))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return self.dispatch_response

    def _write_metadata(self, *, port: int, pid: int) -> None:
        (self.data_dir / "control_plane.json").write_text(
            json.dumps({"port": port, "pid": pid, "started_at": time.time()}),
            encoding="utf-8",
        )

    def test_send_to_bound_chat_goes_through_the_daemon(self) -> None:
        code, out, _ = self._run_cli(
            "prompt", "send", "--chat", "chat-1", "--text", "hello kite"
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            self.received,
            [("prompt/submit", {"text": "hello kite", "chat_id": "chat-1"})],
        )
        self.assertIn("prompt_id: p-1", out)
        self.assertIn("session_id: s-abc", out)
        self.assertIn("status: running", out)
        self.assertIn("owner_recorded: yes", out)

    def test_send_to_session_reports_owner_not_recorded(self) -> None:
        self.dispatch_response = {
            "prompt_id": "p-2",
            "session_id": "s-abc",
            "status": "queued",
            "owner_recorded": False,
        }

        code, out, _ = self._run_cli(
            "prompt", "send", "--session", "s-abc", "--text", "hi"
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            self.received, [("prompt/submit", {"text": "hi", "session_id": "s-abc"})]
        )
        self.assertIn("status: queued", out)
        self.assertIn("owner_recorded: no", out)

    def test_send_requires_a_target(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._run_cli("prompt", "send", "--text", "hi")
        self.assertEqual(ctx.exception.code, 2)

    def test_send_rejects_empty_text(self) -> None:
        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "  ")

        self.assertEqual(code, 2)
        self.assertIn("prompt text must not be empty", err)
        self.assertEqual(self.received, [])

    def test_send_daemon_down_is_fail_closed(self) -> None:
        self.control_server.stop()  # also unpublishes the metadata file

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 2)
        self.assertIn("kited is not running", err)
        self.assertEqual(self.received, [])

    def test_send_stale_metadata_means_daemon_down(self) -> None:
        self.control_server.stop()
        self._write_metadata(port=unused_port(), pid=dead_pid())

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 2)
        self.assertIn("kited is not running", err)

    def test_send_refused_connection_means_daemon_down(self) -> None:
        # Live metadata (own pid) but a dead port: the daemon went away
        # between discovery and connect. Never retried, never direct REST.
        self.control_server.stop()
        self._write_metadata(port=unused_port(), pid=os.getpid())

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 2)
        self.assertIn("kited is not running", err)
        self.assertIn("not submitted", err)

    def test_send_outcome_unknown_is_exit_3(self) -> None:
        dropper = DropConnectionServer()
        self.addCleanup(dropper.close)
        self.control_server.stop()
        self._write_metadata(port=dropper.port, pid=os.getpid())

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 3)
        self.assertIn("may have been delivered", err)
        self.assertIn("kitectl session status", err)

    def test_send_business_error_is_exit_1(self) -> None:
        self.dispatch_error = ControlError(
            "no binding for chat chat-ghost; bind the chat from Feishu first",
            code="no_binding",
        )

        code, _, err = self._run_cli(
            "prompt", "send", "--chat", "chat-ghost", "--text", "hi"
        )

        self.assertEqual(code, 1)
        self.assertIn("kited error no_binding", err)
        self.assertIn("no binding for chat chat-ghost", err)

    def test_send_kap_error_code_passes_through(self) -> None:
        self.dispatch_error = ControlError("session not found", code="40401")

        code, _, err = self._run_cli("prompt", "send", "--session", "s-ghost", "--text", "hi")

        self.assertEqual(code, 1)
        self.assertIn("kited error 40401", err)

    def test_send_without_control_token_is_fail_closed(self) -> None:
        (self.config_dir / "control.token").unlink()

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 2)
        self.assertIn("no control-plane token", err)
        self.assertEqual(self.received, [])

    def test_send_with_wrong_token_surfaces_unauthorized(self) -> None:
        (self.config_dir / "control.token").write_text("wrong-token\n", encoding="utf-8")

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 1)
        self.assertIn("kited error unauthorized", err)


class InteractionSweepTests(KitectlTestCase):
    def _seed(
        self,
        session_id: str = "s-1",
        *,
        approvals: int = 2,
        questions: int = 1,
    ) -> None:
        session = self.state.create_session(session_id)
        for index in range(approvals):
            self.state.add_pending_approval(session, f"a-{index}")
        for index in range(questions):
            self.state.add_pending_question(session, f"q-{index}")

    def test_dry_run_prints_plan_without_resolving(self) -> None:
        self._seed()

        code, out, _ = self._run_cli("interaction", "sweep")

        self.assertEqual(code, 0)
        self.assertIn("2 approval(s), 1 question(s) pending", out)
        self.assertIn("dry-run", out)
        self.assertEqual(self.state.approval_resolutions, [])
        self.assertEqual(self.state.question_dismissals, [])

    def test_yes_sweeps_and_tolerates_dismiss_envelope(self) -> None:
        self._seed()

        code, out, _ = self._run_cli("interaction", "sweep", "--yes")

        self.assertEqual(code, 0)
        self.assertEqual(len(self.state.approval_resolutions), 2)
        for resolution in self.state.approval_resolutions:
            self.assertEqual(resolution["body"]["decision"], "rejected")
        self.assertEqual(self.state.question_dismissals, [("s-1", "q-0")])
        session = self.state.sessions["s-1"]
        self.assertEqual(session.pending_approvals, [])
        self.assertEqual(session.pending_questions, [])
        self.assertIn("swept 2 approval(s), 1 question(s)", out)

    def test_no_pending(self) -> None:
        self.state.create_session("s-1")

        code, out, _ = self._run_cli("interaction", "sweep")

        self.assertEqual(code, 0)
        self.assertIn("(no pending interactions)", out)

    def test_session_filter_leaves_others_untouched(self) -> None:
        self._seed("s-1")
        other = self.state.create_session("s-2")
        self.state.add_pending_approval(other, "a-x")

        code, _, _ = self._run_cli("interaction", "sweep", "--session", "s-2", "--yes")

        self.assertEqual(code, 0)
        self.assertEqual(
            [item["approval_id"] for item in self.state.approval_resolutions], ["a-x"]
        )
        self.assertEqual(len(self.state.sessions["s-1"].pending_approvals), 2)

    def test_missing_session_is_reported_and_skipped(self) -> None:
        code, out, _ = self._run_cli("interaction", "sweep", "--session", "s-nope")

        self.assertEqual(code, 0)
        self.assertIn("session not found; skipped", out)


class ImageSendTests(KitectlTestCase):
    """`image send` is a client of kited's loopback control plane (§3).

    Same discipline as PromptSendTests: the daemon side is faked with a real
    ControlPlaneServer over a scripted dispatch.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.config_dir / "control.token").write_text(
            "test-control-token\n", encoding="utf-8"
        )
        self.received: list[tuple[str, dict]] = []
        self.dispatch_error: Exception | None = None
        self.dispatch_response: dict = {
            "session_id": "s-abc",
            "image_key": "img_v3_fake",
            "delivered": [
                {"chat_id": "chat-1", "message_id": "om_1"},
                {"chat_id": "chat-2", "message_id": "om_2"},
            ],
            "failed": [],
        }
        self.control_server = ControlPlaneServer(
            data_dir=self.data_dir,
            dispatch=self._dispatch,
            auth_token=lambda: "test-control-token",
        )
        self.control_server.start()
        self.addCleanup(self.control_server.stop)
        self.image_path = self.root / "img.png"
        self.image_path.write_bytes(b"\x89PNG-fake")

    def _dispatch(self, method: str, params: dict) -> dict:
        self.received.append((method, params))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return self.dispatch_response

    def test_send_happy_path_reports_delivery(self) -> None:
        code, out, _ = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(self.received), 1)
        method, params = self.received[0]
        self.assertEqual(method, "image/send")
        self.assertEqual(params["chat_id"], "chat-1")
        self.assertEqual(params["path"], str(self.image_path))
        self.assertIn("session_id: s-abc", out)
        self.assertIn("image_key: img_v3_fake", out)
        self.assertIn("delivered: chat-1 (om_1), chat-2 (om_2)", out)
        self.assertNotIn("failed:", out)

    def test_send_absolutizes_relative_paths(self) -> None:
        code, _, _ = self._run_cli("image", "send", "--chat", "chat-1", "--path", "img.png")

        self.assertEqual(code, 0)
        params = self.received[0][1]
        self.assertTrue(os.path.isabs(params["path"]))
        self.assertTrue(params["path"].endswith("img.png"))

    def test_send_partial_failure_reports_and_exits_1(self) -> None:
        self.dispatch_response = {
            "session_id": "s-abc",
            "image_key": "img_v3_fake",
            "delivered": [{"chat_id": "chat-1", "message_id": "om_1"}],
            "failed": [{"chat_id": "chat-2", "error": "send_failed"}],
        }

        code, out, _ = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 1)
        self.assertIn("delivered: chat-1 (om_1)", out)
        self.assertIn("failed: chat-2 (send_failed)", out)

    def test_send_business_error_is_exit_1(self) -> None:
        self.dispatch_error = ControlError(
            "image path does not exist or is not a file: /nope.png",
            code="invalid_path",
        )

        code, _, err = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 1)
        self.assertIn("kited error invalid_path", err)

    def test_send_daemon_down_is_fail_closed(self) -> None:
        self.control_server.stop()  # also unpublishes the metadata file

        code, _, err = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 2)
        self.assertIn("kited is not running", err)
        self.assertEqual(self.received, [])

    def test_send_outcome_unknown_is_exit_3(self) -> None:
        dropper = DropConnectionServer()
        self.addCleanup(dropper.close)
        self.control_server.stop()
        (self.data_dir / "control_plane.json").write_text(
            json.dumps({"port": dropper.port, "pid": os.getpid(), "started_at": time.time()}),
            encoding="utf-8",
        )

        code, _, err = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 3)
        self.assertIn("may have been delivered", err)

    def test_send_with_wrong_token_surfaces_unauthorized(self) -> None:
        (self.config_dir / "control.token").write_text("wrong-token\n", encoding="utf-8")

        code, _, err = self._run_cli(
            "image", "send", "--chat", "chat-1", "--path", str(self.image_path)
        )

        self.assertEqual(code, 1)
        self.assertIn("kited error unauthorized", err)
        self.assertEqual(self.received, [])

    def test_send_requires_chat_and_path(self) -> None:
        with self.assertRaises(SystemExit):
            self._run_cli("image", "send", "--chat", "chat-1")
        with self.assertRaises(SystemExit):
            self._run_cli("image", "send", "--path", str(self.image_path))


class ConfigShowTests(KitectlTestCase):
    def test_show_redacts_secrets_recursively(self) -> None:
        (self.config_dir / "system.yaml").write_text(
            "app_id: cli_abc123\n"
            "app_secret: super-secret-value\n"
            "admin_open_ids:\n"
            "  - ou_admin1\n"
            "kap:\n"
            "  host: 127.0.0.1\n"
            "  port: 58627\n"
            "  token: nested-secret\n"
            "custom_api_key: yet-another-secret\n",
            encoding="utf-8",
        )

        code, out, _ = self._run_cli("config", "show")

        self.assertEqual(code, 0)
        self.assertIn("cli_abc123", out)
        self.assertIn("ou_admin1", out)
        self.assertIn("58627", out)
        self.assertNotIn("super-secret-value", out)
        self.assertNotIn("nested-secret", out)
        self.assertNotIn("yet-another-secret", out)
        self.assertIn("********", out)

    def test_show_without_config_file(self) -> None:
        (self.config_dir / "system.yaml").unlink()

        code, out, _ = self._run_cli("config", "show")

        self.assertEqual(code, 0)
        self.assertIn("system.yaml", out)
        self.assertIn("(no config file)", out)


if __name__ == "__main__":
    unittest.main()
