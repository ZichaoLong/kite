import contextlib
import io
import os
import pathlib
import plistlib
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest.mock import patch

from kite import kitectl
from kite import schedule_units
from kite.schedule_units import ScheduleBackendError, ScheduleError, ScheduleSystemctlError
from kite.stores.binding_store import BindingStore

_TASK_NS = schedule_units._TASK_XML_NAMESPACE


def _tag(name: str) -> str:
    return f"{{{_TASK_NS}}}{name}"


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

    def test_service_unit_escapes_systemd_specifiers(self) -> None:
        # `%h`/`%z` in user text would otherwise be expanded by systemd
        # (silently rewritten text) or reject the unit (bad-setting).
        spec = _spec(text="把 %h 和 %z 写进文本")
        rendered = schedule_units.render_service_unit(spec)
        self.assertIn("%%h", rendered)
        self.assertIn("%%z", rendered)
        self.assertNotIn("%h ", rendered.replace("%%h", ""))
        timer_rendered = schedule_units.render_timer_unit(_spec(chat_id="oc_100%x"))
        self.assertIn("oc_100%%x", timer_rendered)

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


# ---------------------------------------------------------------------------
# Cross-platform backends (docs/contracts/scheduled-prompts.md §2)
# ---------------------------------------------------------------------------


def _planned_spec(plan: schedule_units.SchedulePlan, **overrides) -> schedule_units.ScheduleSpec:
    values = {
        "chat_id": "chat-1",
        "text": "hello",
        "display": "silent",
        "ctl_path": "/home/user/.local/bin/kitectl",
    }
    values.update(overrides)
    return schedule_units.build_schedule_spec(plan=plan, **values)


def _at_plan(raw: str = "2026-08-01T10:30:00") -> schedule_units.SchedulePlan:
    return schedule_units.parse_at_schedule(raw, now=datetime(2026, 7, 25))


class SchedulePlanTests(unittest.TestCase):
    def test_at_plan_carries_local_wall_time(self) -> None:
        plan = _at_plan()
        self.assertFalse(plan.recurring)
        self.assertEqual(plan.on_calendar, "2026-08-01 10:30:00")
        self.assertEqual(plan.one_shot_at, datetime(2026, 8, 1, 10, 30))
        self.assertIsNone(plan.shorthand)
        self.assertIsNone(plan.cron)

    def test_shorthand_plan(self) -> None:
        plan = schedule_units.parse_cron_schedule("Daily")
        self.assertTrue(plan.recurring)
        self.assertEqual(plan.shorthand, "daily")
        self.assertEqual(plan.on_calendar, "daily")
        self.assertIsNone(plan.cron)

    def test_cron_plan_carries_normalized_fields(self) -> None:
        plan = schedule_units.parse_cron_schedule("30 8 * jan 7")
        self.assertTrue(plan.recurring)
        self.assertIsNone(plan.shorthand)
        cron = plan.cron
        self.assertIsNotNone(cron)
        assert cron is not None
        self.assertEqual(cron.minutes, frozenset({30}))
        self.assertEqual(cron.hours, frozenset({8}))
        self.assertIsNone(cron.doms)
        self.assertEqual(cron.months, frozenset({1}))
        # cron's 7 folds into 0 (Sunday).
        self.assertEqual(cron.dows, frozenset({0}))

    def test_legacy_wrappers_still_return_on_calendar_text(self) -> None:
        self.assertEqual(
            schedule_units.parse_at_on_calendar("2026-08-01T10:30:00", now=datetime(2026, 7, 25)),
            "2026-08-01 10:30:00",
        )
        self.assertEqual(
            schedule_units.parse_cron_on_calendar("0 9 * * *"), "*-*-* 09:00:00"
        )


class LaunchdPlistRenderingTests(unittest.TestCase):
    def _render(self, spec: schedule_units.ScheduleSpec) -> dict:
        with patch(
            "kite.schedule_units.default_data_root", return_value=pathlib.Path("/data")
        ):
            return plistlib.loads(schedule_units.render_launchd_plist(spec))

    def test_one_shot_renders_full_date(self) -> None:
        payload = self._render(_planned_spec(_at_plan()))
        self.assertEqual(
            payload["StartCalendarInterval"],
            {"Year": 2026, "Month": 8, "Day": 1, "Hour": 10, "Minute": 30},
        )
        self.assertIs(payload["RunAtLoad"], False)

    def test_recurring_cron_field_mapping(self) -> None:
        cases = {
            "0 9 * * *": {"Minute": 0, "Hour": 9},
            "*/5 * * * *": {
                "Minute": [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
            },
            "30 8 * * 1-5": {"Minute": 30, "Hour": 8, "Weekday": [1, 2, 3, 4, 5]},
            "0 0 1 jan *": {"Minute": 0, "Hour": 0, "Day": 1, "Month": 1},
            "0 9 * * 0": {"Minute": 0, "Hour": 9, "Weekday": 0},
            "0 9 * * 7": {"Minute": 0, "Hour": 9, "Weekday": 0},
            "0 9,17 * * *": {"Minute": 0, "Hour": [9, 17]},
        }
        for cron, expected in cases.items():
            with self.subTest(cron=cron):
                spec = _planned_spec(schedule_units.parse_cron_schedule(cron))
                self.assertEqual(
                    self._render(spec)["StartCalendarInterval"], expected
                )

    def test_shorthand_mapping(self) -> None:
        cases = {
            "minutely": {},
            "hourly": {"Minute": 0},
            "daily": {"Hour": 0, "Minute": 0},
            "weekly": {"Weekday": 1, "Hour": 0, "Minute": 0},
            "monthly": {"Day": 1, "Hour": 0, "Minute": 0},
            "yearly": {"Month": 1, "Day": 1, "Hour": 0, "Minute": 0},
            "annually": {"Month": 1, "Day": 1, "Hour": 0, "Minute": 0},
            "quarterly": {"Month": [1, 4, 7, 10], "Day": 1, "Hour": 0, "Minute": 0},
            "semiannually": {"Month": [1, 7], "Day": 1, "Hour": 0, "Minute": 0},
        }
        for shorthand, expected in cases.items():
            with self.subTest(shorthand=shorthand):
                spec = _planned_spec(schedule_units.parse_cron_schedule(shorthand))
                self.assertEqual(
                    self._render(spec)["StartCalendarInterval"], expected
                )

    def test_program_arguments_carry_verbatim_text(self) -> None:
        spec = _planned_spec(_at_plan(), text='say "hi" there', display="announce")
        payload = self._render(spec)
        # A plist array needs no shell quoting layer; the text rides verbatim.
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/home/user/.local/bin/kitectl",
                "prompt",
                "send",
                "--chat",
                "chat-1",
                "--text",
                'say "hi" there',
                "--display",
                "announce",
            ],
        )
        digest = spec.name.removeprefix("kite-schedule-")
        self.assertEqual(payload["Label"], f"io.kite.schedule.{digest}")
        self.assertEqual(
            payload["StandardOutPath"], f"/data/schedules/{spec.name}.stdout.log"
        )
        self.assertEqual(
            payload["StandardErrorPath"], f"/data/schedules/{spec.name}.stderr.log"
        )

    def test_multiline_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "single line"):
            self._render(_planned_spec(_at_plan(), text="line1\nline2"))


class TaskXmlRenderingTests(unittest.TestCase):
    def _render(self, spec: schedule_units.ScheduleSpec) -> ET.Element:
        return ET.fromstring(
            schedule_units.render_task_xml(spec, now=datetime(2026, 7, 25))
        )

    def _calendar_trigger(self, root: ET.Element) -> ET.Element:
        trigger = root.find(f"{_tag('Triggers')}/{_tag('CalendarTrigger')}")
        self.assertIsNotNone(trigger)
        assert trigger is not None
        return trigger

    def test_one_shot_renders_time_trigger(self) -> None:
        root = self._render(_planned_spec(_at_plan()))
        trigger = root.find(f"{_tag('Triggers')}/{_tag('TimeTrigger')}")
        self.assertIsNotNone(trigger)
        assert trigger is not None
        self.assertEqual(trigger.findtext(_tag("StartBoundary")), "2026-08-01T10:30:00")
        self.assertEqual(trigger.findtext(_tag("Enabled")), "true")
        self.assertIsNone(root.find(f"{_tag('Triggers')}/{_tag('CalendarTrigger')}"))

    def test_daily_cron_maps_to_schedule_by_day(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("0 9 * * *"))
        trigger = self._calendar_trigger(self._render(spec))
        self.assertEqual(trigger.findtext(_tag("StartBoundary")), "2026-07-25T09:00:00")
        self.assertEqual(
            trigger.findtext(f"{_tag('ScheduleByDay')}/{_tag('DaysInterval')}"), "1"
        )

    def test_weekly_cron_maps_to_schedule_by_week(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("30 8 * * 1-5"))
        trigger = self._calendar_trigger(self._render(spec))
        self.assertEqual(trigger.findtext(_tag("StartBoundary")), "2026-07-25T08:30:00")
        days = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(
                f"{_tag('ScheduleByWeek')}/{_tag('DaysOfWeek')}/*"
            )
        ]
        self.assertEqual(days, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.assertEqual(
            trigger.findtext(f"{_tag('ScheduleByWeek')}/{_tag('WeeksInterval')}"), "1"
        )

    def test_monthly_cron_maps_to_schedule_by_month(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("0 0 1 jan *"))
        trigger = self._calendar_trigger(self._render(spec))
        self.assertEqual(trigger.findtext(_tag("StartBoundary")), "2026-07-25T00:00:00")
        days = [
            element.text
            for element in trigger.findall(
                f"{_tag('ScheduleByMonth')}/{_tag('DaysOfMonth')}/*"
            )
        ]
        self.assertEqual(days, ["1"])
        months = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(f"{_tag('ScheduleByMonth')}/{_tag('Months')}/*")
        ]
        self.assertEqual(months, ["January"])

    def test_months_only_cron_expands_every_day_of_month(self) -> None:
        # `15 14 * 3 *` = every day at 14:15 in March; DaysOfMonth 1..31 skips
        # nonexistent days exactly like cron's dom=* does.
        spec = _planned_spec(schedule_units.parse_cron_schedule("15 14 * 3 *"))
        trigger = self._calendar_trigger(self._render(spec))
        days = [
            element.text
            for element in trigger.findall(
                f"{_tag('ScheduleByMonth')}/{_tag('DaysOfMonth')}/*"
            )
        ]
        self.assertEqual(days, [str(day) for day in range(1, 32)])
        months = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(f"{_tag('ScheduleByMonth')}/{_tag('Months')}/*")
        ]
        self.assertEqual(months, ["March"])

    def test_shorthand_hourly_maps_to_repetition(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("hourly"))
        trigger = self._calendar_trigger(self._render(spec))
        self.assertEqual(trigger.findtext(_tag("StartBoundary")), "2026-07-25T00:00:00")
        self.assertEqual(
            trigger.findtext(f"{_tag('Repetition')}/{_tag('Interval')}"), "PT1H"
        )
        self.assertIsNotNone(trigger.find(_tag("ScheduleByDay")))

    def test_shorthand_minutely_maps_to_repetition(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("minutely"))
        trigger = self._calendar_trigger(self._render(spec))
        self.assertEqual(
            trigger.findtext(f"{_tag('Repetition')}/{_tag('Interval')}"), "PT1M"
        )

    def test_shorthand_quarterly_maps_to_schedule_by_month(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("quarterly"))
        trigger = self._calendar_trigger(self._render(spec))
        months = [
            element.tag.rsplit("}", 1)[-1]
            for element in trigger.findall(f"{_tag('ScheduleByMonth')}/{_tag('Months')}/*")
        ]
        self.assertEqual(months, ["January", "April", "July", "October"])

    def test_unmappable_cron_forms_are_rejected(self) -> None:
        # Fail-closed (contract §2): these have no faithful Task Scheduler
        # mapping, so they are rejected instead of silently degraded.
        cases = [
            "*/5 * * * *",  # minute steps: no faithful mapping
            "0 9,17 * * *",  # two times of day: one StartBoundary only
            "* 9 * * *",  # wildcard minute: not a single time of day
            "0 9 * jan mon",  # month+weekday: counts weeks of month, not weekdays
        ]
        for cron in cases:
            with self.subTest(cron=cron):
                spec = _planned_spec(schedule_units.parse_cron_schedule(cron))
                with self.assertRaises(ScheduleError):
                    self._render(spec)

    def test_settings_enable_the_task_and_on_demand_start(self) -> None:
        root = self._render(_planned_spec(_at_plan()))
        settings = root.find(_tag("Settings"))
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.findtext(_tag("Enabled")), "true")
        self.assertEqual(settings.findtext(_tag("AllowStartOnDemand")), "true")
        # The Persistent=true analog: a missed fire runs once on recovery.
        self.assertEqual(settings.findtext(_tag("StartWhenAvailable")), "true")

    def test_actions_command_and_arguments(self) -> None:
        spec = _planned_spec(
            _at_plan(), text='say "hi" there', ctl_path=r"C:\kite\bin\kitectl.exe"
        )
        root = self._render(spec)
        exec_action = root.find(f"{_tag('Actions')}/{_tag('Exec')}")
        self.assertIsNotNone(exec_action)
        assert exec_action is not None
        self.assertEqual(exec_action.findtext(_tag("Command")), r"C:\kite\bin\kitectl.exe")
        self.assertEqual(
            exec_action.findtext(_tag("Arguments")),
            '"prompt" "send" "--chat" "chat-1" "--text" '
            '"say \\"hi\\" there" "--display" "silent"',
        )

    def test_windows_arg_quoting_rules(self) -> None:
        quote = schedule_units._quote_windows_arg
        self.assertEqual(quote("plain"), '"plain"')
        self.assertEqual(quote('a"b'), '"a\\"b"')
        # A trailing backslash precedes the closing quote and must double.
        self.assertEqual(quote("trail\\"), '"trail\\\\"')
        # Interior backslashes not before a quote stay literal.
        self.assertEqual(quote(r"C:\path\x"), '"C:\\path\\x"')

    def test_multiline_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "single line"):
            self._render(_planned_spec(_at_plan(), text="line1\nline2"))


class ScheduleBackendDispatchTests(unittest.TestCase):
    def test_dispatch_matches_the_service_manager_rule(self) -> None:
        with patch("kite.schedule_units.is_windows", return_value=True):
            self.assertIsInstance(
                schedule_units.current_schedule_backend(),
                schedule_units.TaskSchedulerScheduleBackend,
            )
        with patch("kite.schedule_units.is_windows", return_value=False), patch(
            "kite.schedule_units.is_macos", return_value=True
        ):
            self.assertIsInstance(
                schedule_units.current_schedule_backend(),
                schedule_units.LaunchdScheduleBackend,
            )
        with patch("kite.schedule_units.is_windows", return_value=False), patch(
            "kite.schedule_units.is_macos", return_value=False
        ), patch("kite.schedule_units.is_linux", return_value=True):
            self.assertIsInstance(
                schedule_units.current_schedule_backend(),
                schedule_units.SystemdScheduleBackend,
            )

    def test_unsupported_platform_is_a_clear_error(self) -> None:
        with patch("kite.schedule_units.is_windows", return_value=False), patch(
            "kite.schedule_units.is_macos", return_value=False
        ), patch("kite.schedule_units.is_linux", return_value=False):
            with self.assertRaisesRegex(ScheduleError, "not supported on this platform"):
                schedule_units.current_schedule_backend()

    def test_cli_rejects_unsupported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            with patch("kite.schedule_units.is_windows", return_value=False), patch(
                "kite.schedule_units.is_macos", return_value=False
            ), patch("kite.schedule_units.is_linux", return_value=False):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = kitectl.main(
                        [
                            "--config-dir",
                            str(root / "config"),
                            "--data-dir",
                            str(root / "data"),
                            "schedule",
                            "list",
                        ]
                    )
            self.assertEqual(code, 2)
            self.assertIn("not supported on this platform", stderr.getvalue())


class LaunchdBackendTests(unittest.TestCase):
    """launchd backend over a mocked launchctl (no real launchd calls)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.agent_dir = self.root / "LaunchAgents"
        self.data_dir = self.root / "data"
        self.launchctl_calls: list[tuple[str, ...]] = []
        self.bootstrap_returncode = 0
        self.bootout_returncode = 0
        self.list_returncode = 0

        for target, value in (
            ("kite.schedule_units.default_launch_agent_dir", self.agent_dir),
            ("kite.schedule_units.default_data_root", self.data_dir),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        run_patcher = patch(
            "kite.schedule_units._run_launchctl", side_effect=self._fake_launchctl
        )
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        self.backend = schedule_units.LaunchdScheduleBackend()
        uid_patcher = patch.object(self.backend, "_uid_domain", return_value="gui/501")
        uid_patcher.start()
        self.addCleanup(uid_patcher.stop)

    def _fake_launchctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        self.launchctl_calls.append(args)
        returncode = 0
        if args[0] == "bootstrap":
            returncode = self.bootstrap_returncode
        elif args[0] == "bootout":
            returncode = self.bootout_returncode
        elif args[0] == "list":
            returncode = self.list_returncode
        return subprocess.CompletedProcess(args, returncode, stdout="", stderr="")

    def _install(self, **overrides) -> schedule_units.ScheduleSpec:
        spec = _planned_spec(
            overrides.pop("plan", schedule_units.parse_cron_schedule("0 9 * * *")),
            **overrides,
        )
        self.backend.install(spec)
        return spec

    def _plist_path(self, spec: schedule_units.ScheduleSpec) -> pathlib.Path:
        digest = spec.name.removeprefix("kite-schedule-")
        return self.agent_dir / f"io.kite.schedule.{digest}.plist"

    # -- install ------------------------------------------------------------

    def test_install_writes_plist_and_bootstraps(self) -> None:
        spec = self._install()

        plist_path = self._plist_path(spec)
        self.assertTrue(plist_path.exists())
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(payload["Label"], f"io.kite.schedule.{spec.name[14:]}")
        label = schedule_units.launchd_label(spec.name)
        self.assertEqual(
            self.launchctl_calls,
            [("bootout", "gui/501", label), ("bootstrap", "gui/501", str(plist_path))],
        )
        self.assertTrue((self.data_dir / "schedules").is_dir())

    def test_install_falls_back_to_load_when_bootstrap_fails(self) -> None:
        self.bootstrap_returncode = 1
        spec = self._install()

        plist_path = self._plist_path(spec)
        self.assertEqual(self.launchctl_calls[-1], ("load", str(plist_path)))

    def test_install_driver_failure_is_a_backend_error(self) -> None:
        def _failing(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            raise ScheduleBackendError("bootstrap exploded")

        with patch("kite.schedule_units._run_launchctl", side_effect=_failing):
            with self.assertRaisesRegex(ScheduleBackendError, "bootstrap exploded"):
                self.backend.install(
                    _planned_spec(schedule_units.parse_cron_schedule("0 9 * * *"))
                )

    # -- list ---------------------------------------------------------------

    def test_list_parses_plists_and_launchctl_state(self) -> None:
        spec_a = self._install(text="first")
        spec_b = self._install(text="second")

        entries = {entry.name: entry for entry in self.backend.list()}

        self.assertEqual(set(entries), {spec_a.name, spec_b.name})
        self.assertEqual(entries[spec_a.name].on_calendar, "0 9 * * *")
        self.assertEqual(entries[spec_a.name].next_elapse, "-")
        self.list_returncode = 1  # launchd no longer knows the label
        entries = {entry.name: entry for entry in self.backend.list()}
        self.assertEqual(entries[spec_a.name].next_elapse, "(not loaded)")

    def test_list_empty(self) -> None:
        self.assertEqual(self.backend.list(), [])

    # -- show / remove / run-now --------------------------------------------

    def test_show_returns_the_plist_text(self) -> None:
        spec = self._install()

        files = self.backend.show(spec.name)

        self.assertEqual(len(files), 1)
        path, text = files[0]
        self.assertEqual(path, self._plist_path(spec))
        self.assertIn("StartCalendarInterval", text)

    def test_show_unknown_name_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "no schedule named"):
            self.backend.show("kite-schedule-000000000000")

    def test_remove_boots_out_and_deletes_the_plist(self) -> None:
        spec = self._install()
        plist_path = self._plist_path(spec)

        removed = self.backend.remove(spec.name)

        self.assertEqual(removed, spec.name)
        self.assertFalse(plist_path.exists())
        self.assertIn(
            ("bootout", "gui/501", schedule_units.launchd_label(spec.name)),
            self.launchctl_calls,
        )

    def test_remove_falls_back_to_unload(self) -> None:
        spec = self._install()
        self.bootout_returncode = 1

        self.backend.remove(spec.name)

        self.assertIn(("unload", str(self._plist_path(spec))), self.launchctl_calls)

    def test_run_now_starts_the_label(self) -> None:
        spec = self._install()

        started = self.backend.run_now(spec.name)

        self.assertEqual(started, spec.name)
        self.assertEqual(
            self.launchctl_calls[-1], ("start", schedule_units.launchd_label(spec.name))
        )

    def test_run_now_unknown_name_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "no schedule named"):
            self.backend.run_now("kite-schedule-000000000000")


class TaskSchedulerBackendTests(unittest.TestCase):
    """Task Scheduler backend over a mocked schtasks (no real schtasks calls)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.schtasks_calls: list[tuple[str, ...]] = []
        self.query_returncode = 0
        self.query_stdout = ""

        dir_patcher = patch(
            "kite.schedule_units.default_data_root", return_value=self.data_dir
        )
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)
        run_patcher = patch(
            "kite.schedule_units._run_schtasks", side_effect=self._fake_schtasks
        )
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        self.backend = schedule_units.TaskSchedulerScheduleBackend()

    def _fake_schtasks(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        self.schtasks_calls.append(args)
        if args[0] == "/Query":
            return subprocess.CompletedProcess(
                args, self.query_returncode, stdout=self.query_stdout, stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _install(self, **overrides) -> schedule_units.ScheduleSpec:
        spec = _planned_spec(
            overrides.pop("plan", schedule_units.parse_cron_schedule("0 9 * * *")),
            **overrides,
        )
        self.backend.install(spec)
        return spec

    def _xml_path(self, spec: schedule_units.ScheduleSpec) -> pathlib.Path:
        return self.data_dir / "schedules" / f"{spec.name}.xml"

    # -- install ------------------------------------------------------------

    def test_install_writes_xml_and_creates_the_task(self) -> None:
        spec = self._install()

        xml_path = self._xml_path(spec)
        self.assertTrue(xml_path.exists())
        root = ET.fromstring(xml_path.read_bytes())  # utf-16 with declaration
        self.assertEqual(root.tag, _tag("Task"))
        self.assertEqual(
            self.schtasks_calls,
            [("/Create", "/TN", spec.name, "/XML", str(xml_path), "/F")],
        )

    def test_install_rejects_unmappable_cron_before_writing(self) -> None:
        spec = _planned_spec(schedule_units.parse_cron_schedule("*/5 * * * *"))

        with self.assertRaises(ScheduleError):
            self.backend.install(spec)

        self.assertFalse(self._xml_path(spec).exists())
        self.assertEqual(self.schtasks_calls, [])

    def test_install_driver_failure_is_a_backend_error(self) -> None:
        def _failing(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            raise ScheduleBackendError("create exploded")

        with patch("kite.schedule_units._run_schtasks", side_effect=_failing):
            with self.assertRaisesRegex(ScheduleBackendError, "create exploded"):
                self.backend.install(
                    _planned_spec(schedule_units.parse_cron_schedule("0 9 * * *"))
                )

    # -- list ---------------------------------------------------------------

    def test_list_reads_registry_and_queries_next_run_time(self) -> None:
        spec = self._install()
        self.query_stdout = (
            "\r\nHostName: HOST\r\nTaskName: "
            f"{spec.name}\r\nNext Run Time: 8/1/2026 9:00:00 AM\r\n"
        )

        entries = self.backend.list()

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, spec.name)
        self.assertEqual(entry.on_calendar, "daily 09:00")
        self.assertEqual(entry.next_elapse, "8/1/2026 9:00:00 AM")

    def test_list_falls_back_when_query_fails(self) -> None:
        spec = self._install()
        self.query_returncode = 1

        entries = self.backend.list()

        self.assertEqual(entries[0].name, spec.name)
        self.assertEqual(entries[0].next_elapse, "-")

    def test_list_empty(self) -> None:
        self.assertEqual(self.backend.list(), [])

    # -- show / remove / run-now --------------------------------------------

    def test_show_decodes_the_utf16_task_xml(self) -> None:
        spec = self._install()

        files = self.backend.show(spec.name)

        self.assertEqual(len(files), 1)
        path, text = files[0]
        self.assertEqual(path, self._xml_path(spec))
        self.assertIn("StartBoundary", text)

    def test_show_unknown_name_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "no schedule named"):
            self.backend.show("kite-schedule-000000000000")

    def test_remove_deletes_task_and_registry_file(self) -> None:
        spec = self._install()
        xml_path = self._xml_path(spec)

        removed = self.backend.remove(spec.name)

        self.assertEqual(removed, spec.name)
        self.assertFalse(xml_path.exists())
        self.assertEqual(self.schtasks_calls[-1], ("/Delete", "/TN", spec.name, "/F"))

    def test_run_now_runs_the_task(self) -> None:
        spec = self._install()

        started = self.backend.run_now(spec.name)

        self.assertEqual(started, spec.name)
        self.assertEqual(self.schtasks_calls[-1], ("/Run", "/TN", spec.name))

    def test_run_now_unknown_name_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScheduleError, "no schedule named"):
            self.backend.run_now("kite-schedule-000000000000")


if __name__ == "__main__":
    unittest.main()
