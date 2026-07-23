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
from kite.runtime_status import RuntimeStatusWriter
from kite.stores.binding_store import BindingStore


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
    def _bind(
        self,
        chat_id: str,
        session_id: str,
        *,
        permission_mode: str = "auto",
        plan_mode: bool = False,
    ) -> None:
        BindingStore(self.data_dir).save(
            chat_id,
            {
                "session_id": session_id,
                "attached": True,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
            },
        )

    def test_send_to_bound_chat_carries_binding_modes(self) -> None:
        session = self.state.create_session("s-abc", title="demo")
        self._bind("chat-1", "s-abc", permission_mode="manual", plan_mode=True)

        code, out, _ = self._run_cli(
            "prompt", "send", "--chat", "chat-1", "--text", "hello kite"
        )

        self.assertEqual(code, 0)
        self.assertIn("session_id: s-abc", out)
        self.assertIn("status: running", out)
        prompt_line = next(
            line for line in out.splitlines() if line.startswith("prompt_id: ")
        )
        prompt_id = prompt_line.split(": ", 1)[1]
        self.assertTrue(prompt_id.startswith("p-"))
        self.assertEqual(session.active_prompt, prompt_id)
        submission = self.state.prompt_submissions[-1]
        self.assertEqual(submission["content"], [{"type": "text", "text": "hello kite"}])
        self.assertEqual(submission["permission_mode"], "manual")
        self.assertEqual(submission["plan_mode"], True)

    def test_send_second_prompt_is_queued(self) -> None:
        self.state.create_session("s-abc")
        self._bind("chat-1", "s-abc")
        self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "first")

        code, out, _ = self._run_cli(
            "prompt", "send", "--chat", "chat-1", "--text", "second"
        )

        self.assertEqual(code, 0)
        self.assertIn("status: queued", out)

    def test_send_to_session_uses_default_modes(self) -> None:
        self.state.create_session("s-abc")

        code, out, _ = self._run_cli(
            "prompt", "send", "--session", "s-abc", "--text", "hi"
        )

        self.assertEqual(code, 0)
        submission = self.state.prompt_submissions[-1]
        self.assertEqual(submission["permission_mode"], "auto")
        self.assertEqual(submission["plan_mode"], False)

    def test_send_requires_a_target(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._run_cli("prompt", "send", "--text", "hi")
        self.assertEqual(ctx.exception.code, 2)

    def test_send_rejects_empty_text(self) -> None:
        self.state.create_session("s-abc")
        self._bind("chat-1", "s-abc")

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "  ")

        self.assertEqual(code, 2)
        self.assertIn("prompt text must not be empty", err)

    def test_send_without_binding_is_fail_closed(self) -> None:
        code, _, err = self._run_cli(
            "prompt", "send", "--chat", "chat-ghost", "--text", "hi"
        )

        self.assertEqual(code, 2)
        self.assertIn("no binding for chat chat-ghost", err)
        self.assertEqual(self.state.prompt_submissions, [])

    def test_send_unreachable_is_fail_closed(self) -> None:
        self.state.create_session("s-abc")
        self._bind("chat-1", "s-abc")
        self.rest_server.shutdown()
        self.rest_server.server_close()

        code, _, err = self._run_cli("prompt", "send", "--chat", "chat-1", "--text", "hi")

        self.assertEqual(code, 2)
        self.assertIn("cannot reach kap-server", err)

    def test_send_to_unknown_session_surfaces_business_error(self) -> None:
        code, _, err = self._run_cli(
            "prompt", "send", "--session", "s-ghost", "--text", "hi"
        )

        self.assertEqual(code, 1)
        self.assertIn("kap-server error 40401", err)


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
