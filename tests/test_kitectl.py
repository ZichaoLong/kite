import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest

import fake_kap
from kite import kitectl
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


if __name__ == "__main__":
    unittest.main()
