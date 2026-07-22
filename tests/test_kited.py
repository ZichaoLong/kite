import os
import pathlib
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest

import fake_kap
from kite import kited
from kite.adapters.kap_server import BackoffPolicy
from kite.process_utils import process_exists
from kite.runtime_status import read_runtime_status
from kite.stores.binding_store import BindingStore

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


if __name__ == "__main__":
    unittest.main()
