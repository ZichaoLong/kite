"""Instance resolution ladder tests (docs/decisions/multi-instance.md §3)."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from kite import instance_resolution
from test_control_plane import dead_pid


def _write_control_metadata(data_dir: pathlib.Path, *, pid: int, port: int = 43210) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "control_plane.json").write_text(
        json.dumps({"port": port, "pid": pid, "started_at": time.time()}),
        encoding="utf-8",
    )


class ResolutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self._env = patch.dict(
            os.environ,
            {
                "KITE_CONFIG_ROOT": str(self.config_root),
                "KITE_DATA_ROOT": str(self.data_root),
                "KITE_INSTANCE": "",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run_instance(self, name: str | None, *, pid: int) -> None:
        data_dir = (
            self.data_root if name is None else self.data_root / "instances" / name
        )
        _write_control_metadata(data_dir, pid=pid)


class ExplicitPrecedenceTests(ResolutionTestCase):
    def test_flag_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"KITE_INSTANCE": "bravo"}):
            self.assertEqual(
                instance_resolution.resolve_instance_name("acme"), "acme"
            )

    def test_env_wins_when_no_flag(self) -> None:
        with patch.dict(os.environ, {"KITE_INSTANCE": "bravo"}):
            self.assertEqual(instance_resolution.resolve_instance_name(None), "bravo")
            self.assertEqual(instance_resolution.resolve_instance_name("  "), "bravo")

    def test_bad_explicit_names_fail_closed(self) -> None:
        for bad in ("..", "default", "instances", "UPPER"):
            with self.assertRaises(ValueError, msg=bad):
                instance_resolution.resolve_instance_name(bad)
        with patch.dict(os.environ, {"KITE_INSTANCE": ".."}):
            with self.assertRaises(ValueError):
                instance_resolution.resolve_instance_name(None)

    def test_no_explicit_no_running_resolves_default(self) -> None:
        self.assertIsNone(instance_resolution.resolve_instance_name(None))


class SingleRunningTests(ResolutionTestCase):
    def test_single_running_named_instance_wins(self) -> None:
        self._run_instance("acme", pid=os.getpid())
        self.assertEqual(instance_resolution.resolve_instance_name(None), "acme")

    def test_single_running_default_instance_resolves_default(self) -> None:
        self._run_instance(None, pid=os.getpid())
        self.assertIsNone(instance_resolution.resolve_instance_name(None))

    def test_stale_pid_is_filtered_out(self) -> None:
        self._run_instance("acme", pid=dead_pid())
        self.assertEqual(instance_resolution.list_running_instances(), [])
        # Nothing live: fall through to the default instance.
        self.assertIsNone(instance_resolution.resolve_instance_name(None))

    def test_stale_named_instance_does_not_shadow_live_default(self) -> None:
        self._run_instance("acme", pid=dead_pid())
        self._run_instance(None, pid=os.getpid())
        self.assertIsNone(instance_resolution.resolve_instance_name(None))

    def test_invalid_metadata_is_ignored(self) -> None:
        data_dir = self.data_root / "instances" / "acme"
        data_dir.mkdir(parents=True)
        (data_dir / "control_plane.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(instance_resolution.list_running_instances(), [])

    def test_non_instance_dirs_are_skipped(self) -> None:
        bogus = self.data_root / "instances" / "UPPER"
        bogus.mkdir(parents=True)
        _write_control_metadata(bogus, pid=os.getpid())
        self.assertEqual(instance_resolution.list_running_instances(), [])

    def test_ambiguity_fails_closed_with_candidates(self) -> None:
        self._run_instance("acme", pid=os.getpid())
        self._run_instance("bravo", pid=os.getpid())
        with self.assertRaises(ValueError) as ctx:
            instance_resolution.resolve_instance_name(None)
        message = str(ctx.exception)
        self.assertIn("acme", message)
        self.assertIn("bravo", message)
        self.assertIn("--instance", message)

    def test_ambiguity_lists_default_as_candidate(self) -> None:
        self._run_instance(None, pid=os.getpid())
        self._run_instance("acme", pid=os.getpid())
        with self.assertRaises(ValueError) as ctx:
            instance_resolution.resolve_instance_name(None)
        self.assertIn("default", str(ctx.exception))

    def test_explicit_flag_resolves_through_ambiguity(self) -> None:
        self._run_instance("acme", pid=os.getpid())
        self._run_instance("bravo", pid=os.getpid())
        self.assertEqual(instance_resolution.resolve_instance_name("acme"), "acme")

    def test_service_rung_skips_single_running(self) -> None:
        self._run_instance("acme", pid=os.getpid())
        self.assertIsNone(
            instance_resolution.resolve_instance_name(None, allow_single_running=False)
        )
        # ... but the explicit env var still counts for service commands.
        with patch.dict(os.environ, {"KITE_INSTANCE": "acme"}):
            self.assertEqual(
                instance_resolution.resolve_instance_name(
                    None, allow_single_running=False
                ),
                "acme",
            )


class DaemonInstanceTests(ResolutionTestCase):
    def test_daemon_flag_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"KITE_INSTANCE": "bravo"}):
            self.assertEqual(instance_resolution.daemon_instance_name("acme"), "acme")

    def test_daemon_env_then_default(self) -> None:
        with patch.dict(os.environ, {"KITE_INSTANCE": "bravo"}):
            self.assertEqual(instance_resolution.daemon_instance_name(None), "bravo")
        self.assertIsNone(instance_resolution.daemon_instance_name(None))

    def test_daemon_never_uses_single_running(self) -> None:
        self._run_instance("acme", pid=os.getpid())
        self.assertIsNone(instance_resolution.daemon_instance_name(None))

    def test_daemon_bad_name_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            instance_resolution.daemon_instance_name("default")


class RequireExistingInstanceTests(ResolutionTestCase):
    """instance_layout.require_existing_instance (FOCUS parity, decision §3)."""

    def test_uncreated_name_fails_closed_pointing_at_create(self) -> None:
        from kite import instance_layout

        with self.assertRaises(ValueError) as ctx:
            instance_layout.require_existing_instance("ghost")
        message = str(ctx.exception)
        self.assertIn("ghost", message)
        self.assertIn("kitectl instance create ghost", message)
        # Nothing was scaffolded as a side effect.
        self.assertFalse((self.config_root / "instances" / "ghost").exists())
        self.assertFalse((self.data_root / "instances" / "ghost").exists())

    def test_either_axis_on_disk_counts_as_existing(self) -> None:
        from kite import instance_layout

        (self.config_root / "instances" / "acme").mkdir(parents=True)
        self.assertEqual(instance_layout.require_existing_instance("acme"), "acme")

        (self.data_root / "instances" / "bravo").mkdir(parents=True)
        self.assertEqual(instance_layout.require_existing_instance("bravo"), "bravo")

    def test_explicit_directory_axes_are_judged(self) -> None:
        # With KITE_CONFIG_DIR/KITE_DATA_ROOT published, existence is decided
        # by those effective dirs, not the instance layout (kitectl/kited
        # call this only after publishing the axes).
        from kite import instance_layout

        custom = self.root / "custom-config"
        custom.mkdir()
        with patch.dict(
            os.environ,
            {"KITE_CONFIG_DIR": str(custom), "KITE_DATA_ROOT": str(self.root / "nope")},
        ):
            self.assertEqual(instance_layout.require_existing_instance("ghost"), "ghost")

    def test_bad_name_still_fails_format_first(self) -> None:
        from kite import instance_layout

        with self.assertRaises(ValueError):
            instance_layout.require_existing_instance("..")


if __name__ == "__main__":
    unittest.main()
