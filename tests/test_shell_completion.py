"""Tests for kite.shell_completion.

The spec is hand-maintained but machine-locked: the completeness tests walk
the real argparse trees of kitectl/kited and fail on any drift (a missing
subcommand, flag, value flag, positional, or choice set).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from kite import kited
from kite import kitectl
from kite import shell_completion

_HELP_FLAGS = {"-h", "--help"}

_KNOWN_SPEC_KEYS = {
    "flags",
    "value_flags",
    "flag_choices",
    "positionals",
    "choices",
    "subcommands",
}


def _argparse_tree(parser: argparse.ArgumentParser) -> dict:
    """Extract the completion-relevant shape of an argparse parser tree."""
    node: dict = {
        "flags": set(),
        "value_flags": set(),
        "flag_choices": {},
        "positionals": set(),
        "positional_choices": {},
        "subcommands": {},
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                node["subcommands"][name] = _argparse_tree(subparser)
        elif action.option_strings:
            node["flags"].update(action.option_strings)
            # nargs == 0 covers -h/--help and the store_true flags: those are
            # the option strings that consume no following word.
            if action.nargs != 0:
                node["value_flags"].update(action.option_strings)
                if action.choices is not None:
                    node["flag_choices"][action.option_strings[0]] = tuple(action.choices)
        else:
            name = str(action.metavar or action.dest)
            node["positionals"].add(name)
            if action.choices is not None:
                node["positional_choices"][name] = tuple(action.choices)
    return node


def _assert_spec_matches_tree(
    case: unittest.TestCase, spec: dict, tree: dict, path: str
) -> None:
    where = path or "<top>"
    case.assertEqual(
        set(spec.get("flags", ())) | _HELP_FLAGS,
        tree["flags"],
        f"{where}: flags drifted from the argparse parser",
    )
    case.assertEqual(
        set(spec.get("value_flags", ())),
        tree["value_flags"],
        f"{where}: value flags drifted",
    )
    case.assertEqual(
        set(spec.get("positionals", ())),
        tree["positionals"],
        f"{where}: positionals drifted",
    )
    case.assertEqual(
        {flag: tuple(choices) for flag, choices in spec.get("flag_choices", {}).items()},
        tree["flag_choices"],
        f"{where}: flag choices drifted",
    )
    expected_positional_choices = (
        {name: tuple(spec["choices"]) for name in spec.get("positionals", ())}
        if "choices" in spec
        else {}
    )
    case.assertEqual(
        expected_positional_choices,
        tree["positional_choices"],
        f"{where}: positional choices drifted",
    )
    case.assertEqual(
        set(spec.get("subcommands", ())),
        set(tree["subcommands"]),
        f"{where}: subcommands drifted",
    )
    for name, sub_spec in spec.get("subcommands", {}).items():
        _assert_spec_matches_tree(
            case, sub_spec, tree["subcommands"][name], f"{where} {name}".strip()
        )


class SpecHygieneTests(unittest.TestCase):
    """The spec itself stays well-formed, before any parser comparison."""

    def _check_node(self, spec: dict, path: str) -> None:
        where = path or "<top>"
        self.assertLessEqual(set(spec), _KNOWN_SPEC_KEYS, f"{where}: unknown spec keys")
        flags = set(spec.get("flags", ()))
        self.assertFalse(
            flags & _HELP_FLAGS, f"{where}: -h/--help are implicit, do not list them"
        )
        self.assertLessEqual(
            set(spec.get("value_flags", ())), flags, f"{where}: value flags must be flags"
        )
        self.assertLessEqual(
            set(spec.get("flag_choices", ())),
            set(spec.get("value_flags", ())),
            f"{where}: flag_choices keys must be value flags",
        )
        for name, sub in spec.get("subcommands", {}).items():
            self._check_node(sub, f"{where} {name}".strip())

    def test_kitectl_spec_is_well_formed(self) -> None:
        self._check_node(shell_completion.KITECTL_SPEC, "")

    def test_kited_spec_is_well_formed(self) -> None:
        self._check_node(shell_completion.KITED_SPEC, "")


class CompletenessTests(unittest.TestCase):
    """Spec covers every subcommand and flag of the real argparse trees."""

    def test_kitectl_spec_matches_parser(self) -> None:
        _assert_spec_matches_tree(
            self, shell_completion.KITECTL_SPEC, _argparse_tree(kitectl._build_parser()), ""
        )

    def test_kited_spec_matches_parser(self) -> None:
        _assert_spec_matches_tree(
            self, shell_completion.KITED_SPEC, _argparse_tree(kited._build_parser()), ""
        )


class RendererTests(unittest.TestCase):
    def test_render_is_deterministic(self) -> None:
        for shell in shell_completion.SUPPORTED_SHELLS:
            self.assertEqual(
                shell_completion.render(shell), shell_completion.render(shell), shell
            )

    def test_render_rejects_unknown_shell(self) -> None:
        with self.assertRaises(ValueError):
            shell_completion.render("powershell")

    def test_every_shell_covers_the_command_surface(self) -> None:
        words = (
            "kitectl",
            "kited",
            "config",
            "init-token",
            "service",
            "autostart",
            "binding",
            "session",
            "prompt",
            "image",
            "sweep",
            "schedule",
            "run-now",
            "completion",
        )
        flags = ("--config-dir", "--data-dir", "--force", "--display", "--yes")
        for shell in shell_completion.SUPPORTED_SHELLS:
            script = shell_completion.render(shell)
            for word in words:
                self.assertIn(word, script, f"{shell}: {word!r} missing")
            for flag in flags:
                # fish `complete` spells a long flag as `-l name`.
                expected = f"-l {flag[2:]}" if shell == "fish" else flag
                self.assertIn(expected, script, f"{shell}: flag {flag!r} missing")

    def test_bash_script_registers_handlers(self) -> None:
        script = shell_completion.render("bash")
        self.assertIn("_kitectl_complete()", script)
        self.assertIn("_kited_complete()", script)
        self.assertIn("complete -o default -F _kitectl_complete kitectl", script)
        self.assertIn("complete -o default -F _kited_complete kited", script)
        self.assertIn("compgen", script)

    def test_zsh_script_registers_handlers(self) -> None:
        script = shell_completion.render("zsh")
        self.assertIn("compdef _kitectl_complete kitectl", script)
        self.assertIn("compdef _kited_complete kited", script)
        self.assertIn("compadd", script)

    def test_fish_script_registers_handlers(self) -> None:
        script = shell_completion.render("fish")
        self.assertIn("complete -c kitectl", script)
        self.assertIn("complete -c kited", script)
        self.assertIn("__kitectl_entered_path", script)

    def test_fish_value_flag_guard_keeps_flag_argument_completion(self) -> None:
        # Audit N4-MED-1: at `--display <TAB>` (empty value) the fish guard
        # used to suppress the flag's own choice completion too, silently
        # degrading to file completion. The after-value-flag guard attaches
        # to word entries only; flag entries keep the bare path guard.
        script = shell_completion.render("fish")
        display_lines = [
            line
            for line in script.splitlines()
            if line.startswith("complete -c kitectl") and "-l display" in line
        ]
        # Both --display sites (prompt send, schedule create) offer the
        # choices at the empty-value moment.
        self.assertEqual(len(display_lines), 2)
        for line in display_lines:
            self.assertNotIn("after_value_flag", line)
            self.assertIn('-a "silent announce"', line)
            self.assertIn('__kitectl_at ', line)
        # Word entries still carry the guard: a subcommand name is never
        # offered as a flag's value.
        word_lines = [
            line
            for line in script.splitlines()
            if line.startswith("complete -c kitectl")
            and ' -a "' in line
            and "-l " not in line
            and "-s " not in line
        ]
        self.assertTrue(word_lines)
        for line in word_lines:
            self.assertIn("after_value_flag", line)

    @unittest.skipUnless(shutil.which("bash"), "bash not installed")
    def test_bash_script_has_valid_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n"],
            input=shell_completion.render("bash"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("bash"), "bash not installed")
    def test_bash_completion_function_behaves(self) -> None:
        script = shell_completion.render("bash")
        with tempfile.TemporaryDirectory() as tmp:
            script_path = pathlib.Path(tmp) / "completion.bash"
            script_path.write_text(script, encoding="utf-8")
            probes = [
                # (COMP_WORDS, COMP_CWORD, invoked function)
                (["kitectl", "se"], 1, "_kitectl_complete"),
                (["kitectl", "service", ""], 2, "_kitectl_complete"),
                (["kitectl", "service", "autostart", ""], 3, "_kitectl_complete"),
                (["kitectl", "prompt", "send", "--dis"], 3, "_kitectl_complete"),
                (["kitectl", "prompt", "send", "--display", ""], 4, "_kitectl_complete"),
                (["kitectl", "completion", ""], 2, "_kitectl_complete"),
                (["kitectl", "--config-dir", "/x", ""], 3, "_kitectl_complete"),
                (["kited", "--"], 1, "_kited_complete"),
            ]
            lines = [f"source {script_path}"]
            for words, cword, function in probes:
                quoted = " ".join(f"'{word}'" for word in words)
                lines.append(f"COMP_WORDS=({quoted}); COMP_CWORD={cword}")
                lines.append(function)
                lines.append('printf "%s\\n" "${COMPREPLY[@]}"')
                lines.append("echo ---")
            result = subprocess.run(
                ["bash", "-c", "\n".join(lines)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        sections = [section.split() for section in result.stdout.split("---\n")[:-1]]
        self.assertEqual(len(sections), len(probes), result.stdout)
        (
            top_prefix,
            service_words,
            autostart_words,
            send_flag_prefix,
            display_choices,
            completion_words,
            after_global_value_flag,
            kited_flags,
        ) = sections
        self.assertEqual(top_prefix, ["service", "session"])
        self.assertEqual(
            service_words,
            ["install", "uninstall", "start", "stop", "restart", "status", "autostart", "log"],
        )
        self.assertEqual(autostart_words, ["enable", "disable", "status"])
        self.assertEqual(send_flag_prefix, ["--display"])
        self.assertEqual(display_choices, ["silent", "announce"])
        self.assertEqual(completion_words, ["bash", "zsh", "fish"])
        # The walker's value-flag skip keeps the top-level context.
        self.assertIn("schedule", after_global_value_flag)
        self.assertEqual(kited_flags, ["--instance", "--config-dir", "--data-dir", "--help"])


class CliTests(unittest.TestCase):
    def test_kitectl_completion_prints_script(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = kitectl.main(["completion", "bash"])
        self.assertEqual(code, 0)
        script = stdout.getvalue()
        self.assertEqual(script, shell_completion.render("bash"))
        for word in ("_kitectl_complete", "run-now", "autostart", "completion"):
            self.assertIn(word, script)

    def test_kitectl_completion_rejects_unknown_shell(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                kitectl.main(["completion", "tcsh"])
        self.assertEqual(raised.exception.code, 2)

    def test_module_main_prints_script(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = shell_completion.main(["fish"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), shell_completion.render("fish"))

    def test_module_main_rejects_bad_usage(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(shell_completion.main([]), 2)
            self.assertEqual(shell_completion.main(["bash", "extra"]), 2)
            self.assertEqual(shell_completion.main(["tcsh"]), 2)


if __name__ == "__main__":
    unittest.main()
