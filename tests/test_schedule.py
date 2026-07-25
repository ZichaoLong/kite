import contextlib
import io
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from kite import kitectl
from kite import schedule_units
from kite.schedule_units import ScheduleError, ScheduleSystemctlError
from kite.stores.binding_store import BindingStore


class AtParsingTests(unittest.TestCase):
    def test_future_timestamp_normalizes(self) -> None:
        on_calendar = schedule_units.parse_at_on_calendar(
            "2026-08-01T10:30:00", now=datetime(2026, 7, 25)
        )
        self.assertEqual(on_calendar, "2026-08-01 10:30:00")

    def test_past_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "in the past"):
            schedule_units.parse_at_on_calendar(
                "2026-07-01T00:00:00", now=datetime(2026, 7, 25)
            )

    def test_garbage_timestamp_is_rejected(self) -> None:
        for raw in ("tomorrow", "2026-13-01", "*-*-*", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ScheduleError):
                    schedule_units.parse_at_on_calendar(raw, now=datetime(2026, 7, 25))

    def test_offset_aware_timestamp_converts_to_local(self) -> None:
        aware = datetime.fromisoformat("2026-08-01T10:30:00+05:00")
        expected = aware.astimezone().replace(tzinfo=None)
        on_calendar = schedule_units.parse_at_on_calendar(
            "2026-08-01T10:30:00+05:00", now=datetime(2026, 7, 25)
        )
        self.assertEqual(on_calendar, f"{expected:%Y-%m-%d %H:%M:%S}")


class CronParsingTests(unittest.TestCase):
    def test_systemd_shorthands_pass_through(self) -> None:
        for shorthand in ("daily", "Hourly", "WEEKLY", "minutely", "monthly"):
            with self.subTest(shorthand=shorthand):
                self.assertEqual(
                    schedule_units.parse_cron_on_calendar(shorthand), shorthand.lower()
                )

    def test_standard_five_field_conversions(self) -> None:
        cases = {
            "0 9 * * *": "*-*-* 09:00:00",
            "*/5 * * * *": "*-*-* *:00,05,10,15,20,25,30,35,40,45,50,55:00",
            "30 8 * * 1-5": "Mon,Tue,Wed,Thu,Fri *-*-* 08:30:00",
            "0 9 * * mon-fri": "Mon,Tue,Wed,Thu,Fri *-*-* 09:00:00",
            "0 9 * * 0": "Sun *-*-* 09:00:00",
            "0 9 * * 7": "Sun *-*-* 09:00:00",
            "0 9 * * sun": "Sun *-*-* 09:00:00",
            "0 0 1 * *": "*-*-01 00:00:00",
            "0 0 1 jan *": "*-01-01 00:00:00",
            "0 0 1 1 *": "*-01-01 00:00:00",
            "15 14 1 * *": "*-*-01 14:15:00",
            "0 22 * * 1-5/2": "Mon,Wed,Fri *-*-* 22:00:00",
            "1/5 * * * *": "*-*-* *:01,06,11,16,21,26,31,36,41,46,51,56:00",
        }
        for cron, expected in cases.items():
            with self.subTest(cron=cron):
                self.assertEqual(schedule_units.parse_cron_on_calendar(cron), expected)

    def test_garbage_is_rejected(self) -> None:
        cases = [
            "blah",  # not a shorthand, not 5 fields
            "* * * *",  # 4 fields
            "* * * * * *",  # 6 fields
            "61 * * * *",  # minute out of range
            "* 25 * * *",  # hour out of range
            "* * 0 * *",  # day-of-month out of range
            "* * * 13 *",  # month out of range
            "* * * * 8",  # day-of-week out of range
            "*/0 * * * *",  # zero step
            "5-1 * * * *",  # inverted range
            "1, * * * *",  # empty list element
            "0 9 1 * 1",  # dom AND dow restricted: cron ORs, systemd ANDs
            "0 9 15 jan 1",  # month name ok but dom+dow still rejected
        ]
        for cron in cases:
            with self.subTest(cron=cron):
                with self.assertRaises(ScheduleError):
                    schedule_units.parse_cron_on_calendar(cron)


class CtlPathResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def _make_executable(self, path: pathlib.Path) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_explicit_ctl_path_wins(self) -> None:
        explicit = self._make_executable(self.root / "bin" / "kitectl")
        self.assertEqual(
            schedule_units.resolve_ctl_path(str(explicit)), str(explicit.resolve())
        )

    def test_explicit_ctl_path_must_be_executable(self) -> None:
        missing = self.root / "bin" / "kitectl"
        with self.assertRaisesRegex(ScheduleError, "--ctl-path"):
            schedule_units.resolve_ctl_path(str(missing))
        not_executable = self.root / "bin" / "kitectl"
        not_executable.parent.mkdir(parents=True)
        not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        with self.assertRaisesRegex(ScheduleError, "--ctl-path"):
            schedule_units.resolve_ctl_path(str(not_executable))

    def test_bin_dir_candidate_beats_venv(self) -> None:
        bin_ctl = self._make_executable(self.root / "bin" / "kitectl")
        self._make_executable(self.root / "data" / ".venv" / "bin" / "kitectl")
        with patch(
            "kite.schedule_units.default_user_bin_dir", return_value=self.root / "bin"
        ), patch(
            "kite.schedule_units.default_data_root", return_value=self.root / "data"
        ):
            self.assertEqual(schedule_units.resolve_ctl_path(), str(bin_ctl.resolve()))

    def test_venv_is_the_fallback(self) -> None:
        venv_ctl = self._make_executable(self.root / "data" / ".venv" / "bin" / "kitectl")
        (self.root / "bin").mkdir()
        with patch(
            "kite.schedule_units.default_user_bin_dir", return_value=self.root / "bin"
        ), patch(
            "kite.schedule_units.default_data_root", return_value=self.root / "data"
        ):
            self.assertEqual(schedule_units.resolve_ctl_path(), str(venv_ctl.resolve()))

    def test_nothing_found_is_fail_closed(self) -> None:
        with patch(
            "kite.schedule_units.default_user_bin_dir", return_value=self.root / "bin"
        ), patch(
            "kite.schedule_units.default_data_root", return_value=self.root / "data"
        ):
            with self.assertRaisesRegex(ScheduleError, "kitectl not found"):
                schedule_units.resolve_ctl_path()


def _spec(**overrides) -> schedule_units.ScheduleSpec:
    values = {
        "name": schedule_units.schedule_name("chat-1", "*-*-* 09:00:00", "hello"),
        "chat_id": "chat-1",
        "text": "hello",
        "on_calendar": "*-*-* 09:00:00",
        "recurring": True,
        "display": "silent",
        "ctl_path": "/home/user/.local/bin/kitectl",
    }
    values.update(overrides)
    return schedule_units.ScheduleSpec(**values)


class UnitRenderingTests(unittest.TestCase):
    def test_name_is_a_stable_hash_of_chat_schedule_text(self) -> None:
        name_a = schedule_units.schedule_name("chat-1", "*-*-* 09:00:00", "hello")
        name_b = schedule_units.schedule_name("chat-1", "*-*-* 09:00:00", "hello")
        self.assertEqual(name_a, name_b)
        self.assertRegex(name_a, r"^kite-schedule-[0-9a-f]{12}$")
        for chat, on_calendar, text in (
            ("chat-2", "*-*-* 09:00:00", "hello"),
            ("chat-1", "*-*-* 10:00:00", "hello"),
            ("chat-1", "*-*-* 09:00:00", "bye"),
        ):
            with self.subTest(chat=chat, on_calendar=on_calendar, text=text):
                self.assertNotEqual(
                    schedule_units.schedule_name(chat, on_calendar, text), name_a
                )

    def test_recurring_timer_unit(self) -> None:
        rendered = schedule_units.render_timer_unit(_spec())
        self.assertIn("OnCalendar=*-*-* 09:00:00", rendered)
        self.assertIn("Persistent=true", rendered)
        self.assertIn(f"Unit={_spec().name}.service", rendered)
        self.assertIn("WantedBy=timers.target", rendered)

    def test_one_shot_timer_unit(self) -> None:
        spec = _spec(on_calendar="2026-08-01 10:30:00", recurring=False)
        rendered = schedule_units.render_timer_unit(spec)
        self.assertIn("OnCalendar=2026-08-01 10:30:00", rendered)
        self.assertIn("Persistent=true", rendered)

    def test_service_unit_runs_prompt_send_with_resolved_ctl_path(self) -> None:
        spec = _spec(display="announce")
        rendered = schedule_units.render_service_unit(spec)
        self.assertIn("Type=oneshot", rendered)
        self.assertIn('ExecStart="/home/user/.local/bin/kitectl"', rendered)
        self.assertIn('"prompt" "send"', rendered)
        self.assertIn('"--chat" "chat-1"', rendered)
        self.assertIn('"--text" "hello"', rendered)
        self.assertIn('"--display" "announce"', rendered)

    def test_service_unit_quotes_spaces_and_quotes(self) -> None:
        spec = _spec(text='say "hi" there')
        rendered = schedule_units.render_service_unit(spec)
        self.assertIn('"--text" "say \\"hi\\" there"', rendered)

    def test_multiline_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "single line"):
            schedule_units.render_service_unit(_spec(text="line1\nline2"))


class ScheduleCliTests(unittest.TestCase):
    """kitectl schedule over a mocked systemctl (no real systemd calls)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.unit_dir = self.root / "systemd"
        self.config_dir.mkdir()
        self.data_dir.mkdir()
        self.ctl_path = self.root / "bin" / "kitectl"
        self.ctl_path.parent.mkdir()
        self.ctl_path.write_text("#!/bin/sh\n", encoding="utf-8")
        self.ctl_path.chmod(self.ctl_path.stat().st_mode | stat.S_IXUSR)

        self.systemctl_calls: list[tuple[str, ...]] = []
        self.list_timers_returncode = 0
        self.list_timers_stdout = ""

        dir_patcher = patch(
            "kite.schedule_units.default_systemd_user_dir", return_value=self.unit_dir
        )
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)
        run_patcher = patch(
            "kite.schedule_units._run_systemctl", side_effect=self._fake_systemctl
        )
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

    def _fake_systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        self.systemctl_calls.append(args)
        if args[0] == "list-timers":
            return subprocess.CompletedProcess(
                args, self.list_timers_returncode, stdout=self.list_timers_stdout, stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kitectl.main(
                ["--config-dir", str(self.config_dir), "--data-dir", str(self.data_dir), *argv]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def _bind(self, chat_id: str = "chat-1", session_id: str = "s-1") -> None:
        BindingStore(self.data_dir).save(
            chat_id,
            {
                "session_id": session_id,
                "attached": True,
                "permission_mode": "auto",
                "plan_mode": False,
            },
        )

    def _create(self, *extra: str, chat: str = "chat-1", text: str = "hello kite") -> tuple[int, str, str]:
        return self._run_cli(
            "schedule",
            "create",
            "--chat",
            chat,
            "--text",
            text,
            "--ctl-path",
            str(self.ctl_path),
            *extra,
        )

    def _future_at(self) -> str:
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

    def _unit_files(self) -> list[pathlib.Path]:
        if not self.unit_dir.exists():
            return []
        return list(self.unit_dir.iterdir())

    # -- create -----------------------------------------------------------

    def test_create_one_shot_writes_units_and_enables_timer(self) -> None:
        self._bind()
        at = self._future_at()

        code, out, err = self._create("--at", at)

        self.assertEqual(code, 0)
        name_line = next(line for line in out.splitlines() if line.startswith("name: "))
        name = name_line.split(": ", 1)[1]
        self.assertRegex(name, r"^kite-schedule-[0-9a-f]{12}$")
        timer = (self.unit_dir / f"{name}.timer").read_text(encoding="utf-8")
        service = (self.unit_dir / f"{name}.service").read_text(encoding="utf-8")
        on_calendar = at.replace("T", " ")
        self.assertIn(f"OnCalendar={on_calendar}", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(f"Unit={name}.service", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("Type=oneshot", service)
        self.assertIn(f'ExecStart="{self.ctl_path.resolve()}"', service)
        self.assertIn('"--chat" "chat-1"', service)
        self.assertIn('"--text" "hello kite"', service)
        self.assertIn('"--display" "silent"', service)
        # Enable only: the service is never started for the future time.
        self.assertEqual(
            self.systemctl_calls,
            [("daemon-reload",), ("enable", "--now", f"{name}.timer")],
        )
        self.assertEqual(err, "")

    def test_create_recurring_cron_converts_and_warns(self) -> None:
        self._bind()

        code, out, err = self._create("--cron", "0 9 * * *")

        self.assertEqual(code, 0)
        self.assertIn("on_calendar: *-*-* 09:00:00", out)
        # Contract §4.4: recurring without a verified termination strategy warns.
        self.assertIn("warning: recurring schedule", err)
        self.assertIn("termination strategy", err)

    def test_create_recurring_shorthand(self) -> None:
        self._bind()

        code, out, _ = self._create("--cron", "daily")

        self.assertEqual(code, 0)
        self.assertIn("on_calendar: daily", out)

    def test_create_recreate_replaces_the_same_unit_pair(self) -> None:
        self._bind()
        code, out1, _ = self._create("--at", self._future_at())
        self.assertEqual(code, 0)
        code, out2, _ = self._create("--at", self._future_at())
        self.assertEqual(code, 0)
        name1 = next(line for line in out1.splitlines() if line.startswith("name: "))
        name2 = next(line for line in out2.splitlines() if line.startswith("name: "))
        # Same chat + schedule + text → the same unit pair (stable identity).
        if name1.split(": ", 1)[1] == name2.split(": ", 1)[1]:
            self.assertEqual(len(self._unit_files()), 2)

    def test_create_past_at_is_rejected_before_writing(self) -> None:
        self._bind()

        code, _, err = self._create("--at", "2020-01-01T00:00:00")

        self.assertEqual(code, 2)
        self.assertIn("in the past", err)
        self.assertEqual(self._unit_files(), [])
        self.assertEqual(self.systemctl_calls, [])

    def test_create_bad_cron_is_rejected_before_writing(self) -> None:
        self._bind()

        code, _, err = self._create("--cron", "not a cron")

        self.assertEqual(code, 2)
        self.assertIn("--cron", err)
        self.assertEqual(self._unit_files(), [])
        self.assertEqual(self.systemctl_calls, [])

    def test_create_unknown_chat_is_rejected_before_writing(self) -> None:
        code, _, err = self._create("--at", self._future_at(), chat="chat-ghost")

        self.assertEqual(code, 2)
        self.assertIn("no binding for chat chat-ghost", err)
        self.assertEqual(self._unit_files(), [])
        self.assertEqual(self.systemctl_calls, [])

    def test_create_bad_ctl_path_is_rejected_before_writing(self) -> None:
        self._bind()

        code, _, err = self._run_cli(
            "schedule",
            "create",
            "--chat",
            "chat-1",
            "--text",
            "hi",
            "--at",
            self._future_at(),
            "--ctl-path",
            str(self.root / "no-such-kitectl"),
        )

        self.assertEqual(code, 2)
        self.assertIn("--ctl-path", err)
        self.assertEqual(self._unit_files(), [])
        self.assertEqual(self.systemctl_calls, [])

    def test_create_multiline_text_is_rejected_before_writing(self) -> None:
        self._bind()

        code, _, err = self._create("--at", self._future_at(), text="line1\nline2")

        self.assertEqual(code, 2)
        self.assertIn("single line", err)
        self.assertEqual(self._unit_files(), [])
        self.assertEqual(self.systemctl_calls, [])

    def test_create_systemctl_failure_is_exit_1(self) -> None:
        self._bind()

        def _failing(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            raise ScheduleSystemctlError("enable exploded")

        with patch("kite.schedule_units._run_systemctl", side_effect=_failing):
            code, _, err = self._create("--at", self._future_at())

        self.assertEqual(code, 1)
        self.assertIn("enable exploded", err)

    # -- list ---------------------------------------------------------------

    def _seed_schedule(self, text: str = "hello kite") -> str:
        self._bind()
        code, out, _ = self._create("--at", self._future_at(), text=text)
        self.assertEqual(code, 0)
        return next(line for line in out.splitlines() if line.startswith("name: ")).split(
            ": ", 1
        )[1]

    def test_list_parses_list_timers_when_available(self) -> None:
        name = self._seed_schedule()
        self.list_timers_stdout = (
            "Sat 2026-08-01 09:00:00 CST  1 day left   n/a  n/a  "
            f"{name}.timer  {name}.service\n"
            "\n"
            "2 timers listed.\n"
        )

        code, out, _ = self._run_cli("schedule", "list")

        self.assertEqual(code, 0)
        self.assertIn("NAME", out)
        self.assertIn("ON_CALENDAR", out)
        self.assertIn("NEXT", out)
        self.assertIn(name, out)
        self.assertIn("Sat 2026-08-01 09:00:00 CST", out)

    def test_list_falls_back_to_unit_files(self) -> None:
        name = self._seed_schedule()
        self.list_timers_returncode = 1  # systemctl unavailable/erroring

        code, out, _ = self._run_cli("schedule", "list")

        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines() if line.startswith(name))
        self.assertIn("-", row)

    def test_list_empty(self) -> None:
        code, out, _ = self._run_cli("schedule", "list")

        self.assertEqual(code, 0)
        self.assertIn("(no schedules)", out)

    # -- show ---------------------------------------------------------------

    def test_show_prints_both_unit_files(self) -> None:
        name = self._seed_schedule()

        code, out, _ = self._run_cli("schedule", "show", name)

        self.assertEqual(code, 0)
        self.assertIn(f"# {self.unit_dir / (name + '.timer')}", out)
        self.assertIn(f"# {self.unit_dir / (name + '.service')}", out)
        self.assertIn("OnCalendar=", out)
        self.assertIn("ExecStart=", out)

    def test_show_accepts_bare_hash_and_suffixed_names(self) -> None:
        name = self._seed_schedule()
        bare = name.removeprefix("kite-schedule-")
        for given in (name, bare, f"{name}.timer", f"{name}.service"):
            with self.subTest(given=given):
                code, out, _ = self._run_cli("schedule", "show", given)
                self.assertEqual(code, 0)
                self.assertIn("OnCalendar=", out)

    def test_show_unknown_name_is_fail_closed(self) -> None:
        code, _, err = self._run_cli("schedule", "show", "kite-schedule-000000000000")

        self.assertEqual(code, 2)
        self.assertIn("no schedule named", err)

    def test_show_invalid_name_is_fail_closed(self) -> None:
        code, _, err = self._run_cli("schedule", "show", "../../etc")

        self.assertEqual(code, 2)
        self.assertIn("invalid schedule name", err)

    # -- remove -------------------------------------------------------------

    def test_remove_requires_yes(self) -> None:
        name = self._seed_schedule()
        calls_before = list(self.systemctl_calls)

        code, _, err = self._run_cli("schedule", "remove", name)

        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertTrue((self.unit_dir / f"{name}.timer").exists())
        self.assertTrue((self.unit_dir / f"{name}.service").exists())
        self.assertEqual(self.systemctl_calls, calls_before)

    def test_remove_with_yes_disables_and_deletes(self) -> None:
        name = self._seed_schedule()

        code, out, _ = self._run_cli("schedule", "remove", name, "--yes")

        self.assertEqual(code, 0)
        self.assertIn(f"schedule '{name}' removed", out)
        self.assertFalse((self.unit_dir / f"{name}.timer").exists())
        self.assertFalse((self.unit_dir / f"{name}.service").exists())
        self.assertIn(("disable", "--now", f"{name}.timer"), self.systemctl_calls)
        self.assertEqual(self.systemctl_calls[-1], ("daemon-reload",))

    def test_remove_unknown_name_is_fail_closed(self) -> None:
        code, _, err = self._run_cli(
            "schedule", "remove", "kite-schedule-000000000000", "--yes"
        )

        self.assertEqual(code, 2)
        self.assertIn("no schedule named", err)

    # -- run-now ------------------------------------------------------------

    def test_run_now_starts_the_service_unit(self) -> None:
        name = self._seed_schedule()

        code, out, _ = self._run_cli("schedule", "run-now", name)

        self.assertEqual(code, 0)
        self.assertIn(f"schedule '{name}' started", out)
        self.assertEqual(self.systemctl_calls[-1], ("start", f"{name}.service"))

    def test_run_now_unknown_name_is_fail_closed(self) -> None:
        code, _, err = self._run_cli("schedule", "run-now", "kite-schedule-000000000000")

        self.assertEqual(code, 2)
        self.assertIn("no schedule named", err)

    # -- fire-time behavior --------------------------------------------------

    def test_daemon_down_at_fire_time_surfaces_the_refusal(self) -> None:
        """Contract §4.5/§5: the service unit runs exactly this command; when
        kited is down the prompt send exits non-zero, and that failure is what
        the timer's unit log shows (outcome visible, never silently retried)."""
        self._bind()

        code, _, err = self._run_cli(
            "prompt", "send", "--chat", "chat-1", "--text", "hello kite"
        )

        self.assertEqual(code, 2)
        self.assertIn("kited is not running", err)


if __name__ == "__main__":
    unittest.main()
