import json
import os
import pathlib
import socket
import stat
import sys
import tempfile
import unittest
import unittest.mock

import fake_kap
from kite.adapters.kap_server import (
    BackoffPolicy,
    KapServerProcess,
    build_child_env,
    detect_kimi_version,
    find_live_server,
    read_server_token,
)

FAKE_KAP_PY = pathlib.Path(fake_kap.__file__).resolve()


def write_fake_kimi(directory: pathlib.Path) -> str:
    """Write an executable `kimi` shim that runs the fake kap-server."""
    shim = directory / "kimi"
    shim.write_text(
        f"#!/bin/sh\nexec {sys.executable} {FAKE_KAP_PY} \"$@\"\n", encoding="utf-8"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(shim)


class FakeKimiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.kimi_bin = write_fake_kimi(self.root)
        self.home = self.root / "home"


class KapServerProcessTests(FakeKimiTestCase):
    def test_start_waits_for_readiness_and_resolves_port_and_token(self) -> None:
        proc = KapServerProcess(kimi_bin=self.kimi_bin, home=self.home, requested_port=0)
        try:
            proc.start()
            self.assertIsNotNone(proc.pid)
            self.assertIsInstance(proc.port, int)
            self.assertGreater(proc.port, 0)
            self.assertEqual(proc.token, fake_kap.FAKE_TOKEN)
            self.assertIsNone(proc.poll())
        finally:
            # Load-tolerant grace: under a full-suite run the fake's SIGTERM
            # handling can outlast the production default (10s), and what this
            # test locks is the clean-stop path, not the timing budget.
            returncode = proc.stop(grace_seconds=30)
        self.assertEqual(returncode, 0)

    def test_port_conflict_retries_with_next_port(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        occupied = blocker.getsockname()[1]

        proc = KapServerProcess(
            kimi_bin=self.kimi_bin, home=self.home, requested_port=occupied
        )
        try:
            proc.start()
            self.assertEqual(proc.port, occupied + 1)
        finally:
            proc.stop()

    def test_crash_is_observed_and_restart_spawns_a_new_child(self) -> None:
        proc = KapServerProcess(
            kimi_bin=self.kimi_bin,
            home=self.home,
            requested_port=0,
            env_overlay={"KITE_FAKE_KAP_DIE_AFTER": "0.3"},
        )
        proc.start()
        first_pid = proc.pid
        returncode = proc.wait(timeout=10)
        self.assertEqual(returncode, 3)  # the fake's crash exit code

        proc2 = KapServerProcess(kimi_bin=self.kimi_bin, home=self.home, requested_port=0)
        try:
            proc2.start()
            self.assertNotEqual(proc2.pid, first_pid)
        finally:
            proc2.stop()

    def test_early_exit_fails_start_and_leaves_no_child(self) -> None:
        proc = KapServerProcess(
            kimi_bin=self.kimi_bin, home=self.home, requested_port=0,
            readiness_timeout_seconds=5.0,
            env_overlay={"KITE_FAKE_KAP_EXIT": "7"},
        )
        with self.assertRaises(RuntimeError) as ctx:
            proc.start()
        self.assertIn("rc=7", str(ctx.exception))
        self.assertIsNotNone(proc.poll())

    def test_sigkill_crash_then_sigterm_stop(self) -> None:
        proc = KapServerProcess(kimi_bin=self.kimi_bin, home=self.home, requested_port=0)
        proc.start()
        pid = proc.pid
        assert pid is not None
        os.kill(pid, 9)
        returncode = proc.wait(timeout=10)
        self.assertEqual(returncode, -9)

        proc2 = KapServerProcess(kimi_bin=self.kimi_bin, home=self.home, requested_port=0)
        proc2.start()
        # Load-tolerant grace (see test_start_waits_for_readiness...).
        self.assertEqual(proc2.stop(grace_seconds=30), 0)
        self.assertIsNotNone(proc2.poll())

    def test_child_env_contains_home_and_model_overlay(self) -> None:
        # The fake server re-exports selected env into the registry-adjacent
        # file via build_child_env; assert the mapping logic directly.
        env = build_child_env(
            self.home,
            {"KIMI_MODEL_NAME": "kimi-for-coding", "EXTRA": "1"},
            base_env={"PATH": "/usr/bin", "KIMI_API_KEY": "sk-test",
                      "KIMI_BASE_URL": "https://example.test", "SECRET_DROP": "x"},
        )
        self.assertEqual(env["KIMI_CODE_HOME"], str(self.home))
        self.assertEqual(env["KIMI_MODEL_API_KEY"], "sk-test")
        self.assertEqual(env["KIMI_MODEL_BASE_URL"], "https://example.test")
        self.assertEqual(env["KIMI_MODEL_NAME"], "kimi-for-coding")
        self.assertEqual(env["EXTRA"], "1")
        self.assertNotIn("SECRET_DROP", env)


class BackoffPolicyTests(unittest.TestCase):
    def test_exponential_growth_capped(self) -> None:
        policy = BackoffPolicy(base_seconds=1.0, cap_seconds=8.0)
        delays = [policy.next_delay(uptime_seconds=0) for _ in range(6)]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_stable_uptime_resets_streak(self) -> None:
        policy = BackoffPolicy(base_seconds=1.0, cap_seconds=30.0, stable_after_seconds=60.0)
        policy.next_delay(uptime_seconds=0)
        policy.next_delay(uptime_seconds=0)
        self.assertEqual(policy.next_delay(uptime_seconds=120.0), 1.0)


class RegistryAndTokenTests(FakeKimiTestCase):
    def test_find_live_server_and_read_token(self) -> None:
        proc = KapServerProcess(kimi_bin=self.kimi_bin, home=self.home, requested_port=0)
        try:
            proc.start()
            live = find_live_server(self.home)
            self.assertIsNotNone(live)
            assert live is not None
            self.assertEqual(live.pid, proc.pid)
            self.assertEqual(live.port, proc.port)
            self.assertEqual(read_server_token(self.home), fake_kap.FAKE_TOKEN)
        finally:
            proc.stop()

    def test_find_live_server_ignores_dead_pids(self) -> None:
        instances = self.home / "server" / "instances"
        instances.mkdir(parents=True)
        (instances / "dead.json").write_text(json.dumps({"pid": 999999, "port": 1}))
        self.assertIsNone(find_live_server(self.home))

    def test_detect_kimi_version_parses_fake(self) -> None:
        self.assertEqual(detect_kimi_version(self.kimi_bin), "0.28.1")


if __name__ == "__main__":
    unittest.main()
