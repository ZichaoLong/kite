import unittest

from kite.command_surface import (
    COMMAND_SPECS,
    build_help_text,
    build_usage_text,
    get_command_spec,
    parse_permission_mode_arg,
    parse_plan_mode_arg,
    parse_slash_command,
)


class ParseSlashCommandTests(unittest.TestCase):
    def test_plain_text_is_not_a_command(self) -> None:
        self.assertIsNone(parse_slash_command("hello world"))

    def test_empty_and_whitespace_are_not_commands(self) -> None:
        self.assertIsNone(parse_slash_command(""))
        self.assertIsNone(parse_slash_command("   "))

    def test_bare_slash_is_not_a_command(self) -> None:
        self.assertIsNone(parse_slash_command("/"))

    def test_parses_name_and_arg(self) -> None:
        command = parse_slash_command("/mode auto")
        assert command is not None
        self.assertEqual(command.name, "/mode")
        self.assertEqual(command.arg, "auto")

    def test_name_is_lowercased_arg_keeps_casing(self) -> None:
        command = parse_slash_command("/MODE  AbC ")
        assert command is not None
        self.assertEqual(command.name, "/mode")
        self.assertEqual(command.arg, "AbC")

    def test_arg_with_spaces_is_preserved(self) -> None:
        command = parse_slash_command("/switch abc def")
        assert command is not None
        self.assertEqual(command.name, "/switch")
        self.assertEqual(command.arg, "abc def")

    def test_bot_mention_suffix_is_stripped(self) -> None:
        command = parse_slash_command("/help@KiteBot extra")
        assert command is not None
        self.assertEqual(command.name, "/help")
        self.assertEqual(command.arg, "extra")

    def test_leading_whitespace_is_tolerated(self) -> None:
        command = parse_slash_command("  /status")
        assert command is not None
        self.assertEqual(command.name, "/status")
        self.assertEqual(command.arg, "")

    def test_double_slash_is_a_command_name(self) -> None:
        # "/ /x" is not, but "//x" parses as name "//x" (unknown downstream).
        command = parse_slash_command("//x")
        assert command is not None
        self.assertEqual(command.name, "//x")


class CommandSpecTests(unittest.TestCase):
    def test_mvp_command_table_is_complete(self) -> None:
        names = [spec.name for spec in COMMAND_SPECS]
        self.assertEqual(
            names,
            [
                "/new",
                "/sessions",
                "/switch",
                "/detach",
                "/attach",
                "/mode",
                "/plan",
                "/group",
                "/status",
                "/last",
                "/abort",
                "/init",
                "/help",
            ],
        )

    def test_get_command_spec_lookup(self) -> None:
        spec = get_command_spec("/switch")
        assert spec is not None
        self.assertIn("〈id〉", spec.usage)
        self.assertIsNone(get_command_spec("/bogus"))
        self.assertIsNone(get_command_spec(""))

    def test_usage_text_for_known_command(self) -> None:
        text = build_usage_text("/detach")
        self.assertIn("用法：`/detach`", text)
        self.assertIn("说明：", text)

    def test_usage_text_for_unknown_command_points_to_help(self) -> None:
        self.assertIn("/help", build_usage_text("/bogus"))


class HelpTextTests(unittest.TestCase):
    def test_help_lists_every_mvp_command(self) -> None:
        text = build_help_text()
        for spec in COMMAND_SPECS:
            self.assertIn(spec.usage, text)
            self.assertIn(spec.summary, text)

    def test_help_uses_full_width_angle_brackets(self) -> None:
        # Feishu hides ASCII <...> even in code spans; usage placeholders
        # must use the full-width form.
        self.assertNotIn("<", build_help_text())
        self.assertIn("〈id〉", build_help_text())

    def test_help_mentions_plain_text_prompts(self) -> None:
        self.assertIn("直接发送文字", build_help_text())


class ArgParserTests(unittest.TestCase):
    def test_permission_mode_accepts_valid_values(self) -> None:
        self.assertEqual(parse_permission_mode_arg("auto"), "auto")
        self.assertEqual(parse_permission_mode_arg("manual"), "manual")
        self.assertEqual(parse_permission_mode_arg(" yolo "), "yolo")
        self.assertEqual(parse_permission_mode_arg("AUTO"), "auto")

    def test_permission_mode_rejects_invalid_values(self) -> None:
        self.assertIsNone(parse_permission_mode_arg(""))
        self.assertIsNone(parse_permission_mode_arg("plan"))
        self.assertIsNone(parse_permission_mode_arg("yolo2"))
        self.assertIsNone(parse_permission_mode_arg(None))  # type: ignore[arg-type]

    def test_plan_mode_accepts_on_off(self) -> None:
        self.assertIs(parse_plan_mode_arg("on"), True)
        self.assertIs(parse_plan_mode_arg("OFF"), False)
        self.assertIs(parse_plan_mode_arg(" off "), False)

    def test_plan_mode_rejects_invalid_values(self) -> None:
        self.assertIsNone(parse_plan_mode_arg(""))
        self.assertIsNone(parse_plan_mode_arg("1"))
        self.assertIsNone(parse_plan_mode_arg("toggle"))


if __name__ == "__main__":
    unittest.main()
