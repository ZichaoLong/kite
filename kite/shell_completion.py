"""Static shell completion scripts for the KITE CLIs (kitectl, kited).

A single declarative spec (nested dicts mirroring the argparse trees of
kite/kitectl.py and kite/kited.py) feeds one renderer per shell
(bash/zsh/fish); render() returns the full script text. The scripts are
static — no callback into Python at Tab time — so completion stays instant
and works without an instance config. Re-sourcing a script is harmless (it
just redefines the same functions and registrations).

The spec is hand-maintained but machine-locked:
tests/test_shell_completion.py walks the real argparse parsers and fails on
any drift (a missing subcommand, flag, positional, or choice set).

Usage: `kitectl completion bash` (or `python -m kite.shell_completion bash`),
typically wired as `eval "$(kitectl completion bash)"` in the shell's rc.
"""

from __future__ import annotations

import sys
from typing import Iterator

from kite.schedule_units import DISPLAY_MODES

SUPPORTED_SHELLS = ("bash", "zsh", "fish")

# argparse adds -h/--help on every level; the renderers append them to every
# node's flags instead of repeating them throughout the spec.
_HELP_FLAGS = ("-h", "--help")

# Value flags whose argument is a filesystem path: fish keeps its default
# file completion for them (for the other shells the value-flag fall-through
# already lands on path completion).
_PATH_VALUE_FLAGS = ("--config-dir", "--data-dir", "--path", "--ctl-path")

# Spec node shape (every key optional):
#   "flags":        option strings offered at this level (short and long
#                   forms; -h/--help are implicit, see _HELP_FLAGS)
#   "value_flags":  the subset of flags that consume the next word (the
#                   command-line walkers skip that word when locating the
#                   current subcommand path)
#   "flag_choices": {flag: fixed value candidates} (e.g. --display)
#   "positionals":  positional argument names (completeness locking only)
#   "choices":      fixed candidates for the positional (offered as words)
#   "subcommands":  {name: node}
KITECTL_SPEC: dict = {
    "flags": ("--instance", "--config-dir", "--data-dir"),
    "value_flags": ("--instance", "--config-dir", "--data-dir"),
    "subcommands": {
        "config": {
            "subcommands": {
                "show": {},
                "init-token": {},
            },
        },
        "service": {
            "subcommands": {
                "install": {},
                "uninstall": {},
                "start": {},
                "stop": {"flags": ("--force",)},
                "restart": {"flags": ("--force",)},
                "status": {},
                "autostart": {
                    "subcommands": {
                        "enable": {},
                        "disable": {},
                        "status": {},
                    },
                },
                "log": {
                    "flags": ("-n", "--lines"),
                    "value_flags": ("-n", "--lines"),
                },
            },
        },
        "binding": {
            "subcommands": {
                "list": {},
            },
        },
        "session": {
            "subcommands": {
                "list": {},
                "status": {},
            },
        },
        "prompt": {
            "subcommands": {
                "send": {
                    "flags": ("--chat", "--session", "--text", "--display"),
                    "value_flags": ("--chat", "--session", "--text", "--display"),
                    "flag_choices": {"--display": DISPLAY_MODES},
                },
            },
        },
        "image": {
            "subcommands": {
                "send": {
                    "flags": ("--chat", "--path"),
                    "value_flags": ("--chat", "--path"),
                },
            },
        },
        "interaction": {
            "subcommands": {
                "sweep": {
                    "flags": ("--session", "--yes"),
                    "value_flags": ("--session",),
                },
            },
        },
        "instance": {
            "subcommands": {
                "create": {"positionals": ("name",)},
            },
        },
        "schedule": {
            "subcommands": {
                "create": {
                    "flags": ("--chat", "--text", "--at", "--cron", "--display", "--ctl-path"),
                    "value_flags": (
                        "--chat",
                        "--text",
                        "--at",
                        "--cron",
                        "--display",
                        "--ctl-path",
                    ),
                    "flag_choices": {"--display": DISPLAY_MODES},
                },
                "list": {},
                "show": {"positionals": ("name",)},
                "remove": {
                    "flags": ("--yes",),
                    "positionals": ("name",),
                },
                "run-now": {"positionals": ("name",)},
            },
        },
        "completion": {
            "positionals": ("shell",),
            "choices": SUPPORTED_SHELLS,
        },
    },
}

# kited has no user-facing subcommands (top-level flags only).
KITED_SPEC: dict = {
    "flags": ("--instance", "--config-dir", "--data-dir"),
    "value_flags": ("--instance", "--config-dir", "--data-dir"),
}

COMMANDS = {
    "kitectl": KITECTL_SPEC,
    "kited": KITED_SPEC,
}


def _iter_nodes(spec: dict, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], dict]]:
    """Yield (path, node) for the spec root and every descendant, in spec order."""
    yield path, spec
    for name, sub in spec.get("subcommands", {}).items():
        yield from _iter_nodes(sub, (*path, name))


def _node_words(node: dict) -> tuple[str, ...]:
    """The plain-word candidates of a node: subcommand names, else positional choices."""
    subcommands = node.get("subcommands", {})
    if subcommands:
        return tuple(subcommands)
    return tuple(node.get("choices", ()))


def _node_flags(node: dict) -> tuple[str, ...]:
    return tuple(node.get("flags", ())) + _HELP_FLAGS


def _value_flags(spec: dict) -> tuple[str, ...]:
    """Every value-consuming flag of the tree, deduplicated, in spec order."""
    ordered: list[str] = []
    for _path, node in _iter_nodes(spec):
        for flag in node.get("value_flags", ()):
            if flag not in ordered:
                ordered.append(flag)
    return tuple(ordered)


def _flag_choices(spec: dict) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for _path, node in _iter_nodes(spec):
        for flag, choices in node.get("flag_choices", {}).items():
            merged.setdefault(flag, tuple(choices))
    return merged


def _render_bash() -> str:
    lines = [
        "# Bash completion for kitectl and kited.",
        "# Generated by kite/shell_completion.py; regenerate with",
        "# `kitectl completion bash` after changing the CLI surface.",
    ]
    for command, spec in COMMANDS.items():
        lines.append("")
        lines.extend(_bash_function_lines(command, spec))
    lines.append("")
    for command in COMMANDS:
        lines.append(f"complete -o default -F _{command}_complete {command}")
    return "\n".join(lines) + "\n"


def _bash_function_lines(command: str, spec: dict) -> list[str]:
    value_flags = _value_flags(spec)
    lines = [
        f"_{command}_complete() {{",
        "  local cur prev path word cmds flags i skip_next",
        "  COMPREPLY=()",
        '  cur="${COMP_WORDS[COMP_CWORD]}"',
        '  prev=""',
        "  if (( COMP_CWORD > 0 )); then",
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        "  fi",
        "",
        "  # A flag with fixed value candidates completes those; any other",
        "  # value flag leaves completion to the shell default (paths).",
        '  case "$prev" in',
    ]
    for flag, choices in _flag_choices(spec).items():
        choices_text = " ".join(choices)
        lines += [
            f"    {flag})",
            f'      COMPREPLY=($(compgen -W "{choices_text}" -- "$cur"))',
            "      return 0",
            "      ;;",
        ]
    if value_flags:
        lines += [
            f"    {'|'.join(value_flags)})",
            "      return 0",
            "      ;;",
        ]
    lines += [
        "  esac",
        "",
        "  # Locate the current subcommand path, skipping flag values.",
        '  path=""',
        "  skip_next=0",
        "  for (( i = 1; i < COMP_CWORD; i++ )); do",
        '    word="${COMP_WORDS[i]}"',
        "    if (( skip_next )); then",
        "      skip_next=0",
        "      continue",
        "    fi",
        '    case "$word" in',
    ]
    if value_flags:
        lines += [
            f"      {'|'.join(value_flags)})",
            "        skip_next=1",
            "        ;;",
        ]
    lines += [
        "      -*)",
        "        ;;",
        "      *)",
        '        if [[ -n "$path" ]]; then',
        '          path="$path $word"',
        "        else",
        '          path="$word"',
        "        fi",
        "        ;;",
        "    esac",
        "  done",
        "",
        '  cmds=""',
        f'  flags="{" ".join(_HELP_FLAGS)}"',
        '  case "$path" in',
    ]
    for path, node in _iter_nodes(spec):
        path_text = " ".join(path)
        words = _node_words(node)
        flags_text = " ".join(_node_flags(node))
        lines.append(f'    "{path_text}")')
        if words:
            lines.append(f'      cmds="{" ".join(words)}"')
        lines.append(f'      flags="{flags_text}"')
        lines.append("      ;;")
    lines += [
        "  esac",
        "",
        '  if [[ "$cur" == -* ]]; then',
        '    COMPREPLY=($(compgen -W "$flags" -- "$cur"))',
        "  else",
        '    COMPREPLY=($(compgen -W "$cmds" -- "$cur"))',
        "  fi",
        "  return 0",
        "}",
    ]
    return lines


def _render_zsh() -> str:
    lines = [
        "# zsh completion for kitectl and kited.",
        "# Generated by kite/shell_completion.py; regenerate with",
        "# `kitectl completion zsh` after changing the CLI surface.",
        "",
        "if ! whence compdef >/dev/null 2>&1; then",
        "  autoload -Uz compinit",
        "  compinit",
        "fi",
    ]
    for command, spec in COMMANDS.items():
        lines.append("")
        lines.extend(_zsh_function_lines(command, spec))
    lines.append("")
    for command in COMMANDS:
        lines.append(f"compdef _{command}_complete {command}")
    return "\n".join(lines) + "\n"


def _zsh_function_lines(command: str, spec: dict) -> list[str]:
    value_flags = _value_flags(spec)
    lines = [
        f"_{command}_complete() {{",
        "  local cur prev path word cmds flags",
        "  local i skip_next",
        "  local -a candidates",
        '  cur="${words[CURRENT]}"',
        '  prev=""',
        "  if (( CURRENT > 2 )); then",
        '    prev="${words[CURRENT-1]}"',
        "  fi",
        "",
        "  # A flag with fixed value candidates completes those; any other",
        "  # value flag leaves completion to the shell default (paths).",
        '  case "$prev" in',
    ]
    for flag, choices in _flag_choices(spec).items():
        lines += [
            f"    {flag})",
            f"      candidates=({' '.join(choices)})",
            '      compadd -- "${candidates[@]}"',
            "      return 0",
            "      ;;",
        ]
    if value_flags:
        lines += [
            f"    {'|'.join(value_flags)})",
            "      return 1",
            "      ;;",
        ]
    lines += [
        "  esac",
        "",
        "  # Locate the current subcommand path, skipping flag values.",
        '  path=""',
        "  skip_next=0",
        "  for (( i = 2; i < CURRENT; i++ )); do",
        '    word="${words[i]}"',
        "    if (( skip_next )); then",
        "      skip_next=0",
        "      continue",
        "    fi",
        '    case "$word" in',
    ]
    if value_flags:
        lines += [
            f"      {'|'.join(value_flags)})",
            "        skip_next=1",
            "        ;;",
        ]
    lines += [
        "      -*)",
        "        ;;",
        "      *)",
        '        if [[ -n "$path" ]]; then',
        '          path="$path $word"',
        "        else",
        '          path="$word"',
        "        fi",
        "        ;;",
        "    esac",
        "  done",
        "",
        '  cmds=""',
        f'  flags="{" ".join(_HELP_FLAGS)}"',
        '  case "$path" in',
    ]
    for path, node in _iter_nodes(spec):
        path_text = " ".join(path)
        words = _node_words(node)
        flags_text = " ".join(_node_flags(node))
        lines.append(f'    "{path_text}")')
        if words:
            lines.append(f'      cmds="{" ".join(words)}"')
        lines.append(f'      flags="{flags_text}"')
        lines.append("      ;;")
    lines += [
        "  esac",
        "",
        '  if [[ "$cur" == -* ]]; then',
        "    candidates=(${=flags})",
        "  else",
        "    candidates=(${=cmds})",
        "  fi",
        "  (( ${#candidates[@]} > 0 )) || return 1",
        '  compadd -- "${candidates[@]}"',
        "}",
    ]
    return lines


def _render_fish() -> str:
    lines = [
        "# fish completion for kitectl and kited.",
        "# Generated by kite/shell_completion.py; regenerate with",
        "# `kitectl completion fish` after changing the CLI surface.",
    ]
    for command, spec in COMMANDS.items():
        lines.append("")
        if spec.get("subcommands"):
            lines.extend(_fish_helper_lines(command, spec))
            lines.append("")
        lines.extend(_fish_complete_lines(command, spec))
    return "\n".join(lines) + "\n"


def _fish_helper_lines(command: str, spec: dict) -> list[str]:
    prefix = f"__{command}"
    value_cases = " ".join(f"'{flag}'" for flag in _value_flags(spec))
    value_list = " ".join(_value_flags(spec))
    return [
        # The subcommand path entered so far (flag values skipped), joined
        # with spaces, e.g. "service autostart"; empty at the top level.
        f"function {prefix}_entered_path",
        "    set -l tokens (commandline -opc)",
        "    if test (count $tokens) -gt 0",
        "        set -e tokens[1]",
        "    end",
        "    set -l path",
        "    set -l skip_next 0",
        "    for token in $tokens",
        "        if test $skip_next -eq 1",
        "            set skip_next 0",
        "            continue",
        "        end",
        "        switch $token",
        f"            case {value_cases}",
        "                set skip_next 1",
        "            case '-*'",
        "            case '*'",
        "                set -a path $token",
        "        end",
        "    end",
        '    string join -- " " $path',
        "end",
        "",
        f"function {prefix}_at --argument-names want",
        f'    test "({prefix}_entered_path)" = "$want"',
        "end",
        "",
        # True when the word before the cursor is a flag that consumes the
        # word being completed. Used to suppress WORD candidates there (a
        # subcommand name is never a flag value); the flag's own argument
        # completion (fixed choices or paths) is governed by the flag
        # entries and is deliberately NOT gated on this.
        f"function {prefix}_after_value_flag",
        "    set -l tokens (commandline -opc)",
        "    if test (count $tokens) -lt 2",
        "        return 1",
        "    end",
        f"    contains -- $tokens[-1] {value_list}",
        "end",
    ]


def _fish_flag_options(flag: str, node: dict) -> str:
    """The `complete` option fragment for one flag (-s/-l, -r, -f, -a)."""
    parts: list[str] = []
    if flag.startswith("--"):
        parts.append(f"-l {flag[2:]}")
    else:
        parts.append(f"-s {flag[1:]}")
    if flag in node.get("value_flags", ()):
        parts.append("-r")
        choices = node.get("flag_choices", {}).get(flag)
        if choices is not None:
            parts.append("-f")
            parts.append(f'-a "{" ".join(choices)}"')
        elif flag not in _PATH_VALUE_FLAGS:
            parts.append("-f")
    else:
        parts.append("-f")
    return " " + " ".join(parts)


def _fish_complete_lines(command: str, spec: dict) -> list[str]:
    lines: list[str] = []
    has_subcommands = bool(spec.get("subcommands"))
    for path, node in _iter_nodes(spec):
        if has_subcommands:
            path_guard = f' -n "__{command}_at \'{" ".join(path)}\'"'
            # The value-flag guard attaches to WORD entries only (audit
            # N4-MED-1): right after a value flag (e.g. `--display <TAB>`,
            # empty value) subcommand/positional words must stay out of the
            # way, but the flag's own argument completion — fixed choices
            # like silent/announce, or the path fallback — lives on the flag
            # entry and must NOT be suppressed (it would silently degrade
            # to file completion).
            word_guard = f' -n "__{command}_at \'{" ".join(path)}\'; and not __{command}_after_value_flag"'
        else:
            path_guard = ""
            word_guard = ""
        for word in _node_words(node):
            lines.append(f'complete -c {command}{word_guard} -f -a "{word}"')
        for flag in _node_flags(node):
            lines.append(f"complete -c {command}{path_guard}{_fish_flag_options(flag, node)}")
    return lines


_RENDERERS = {
    "bash": _render_bash,
    "zsh": _render_zsh,
    "fish": _render_fish,
}


def render(shell: str) -> str:
    """The full completion script for one of SUPPORTED_SHELLS."""
    normalized = str(shell or "").strip().lower()
    renderer = _RENDERERS.get(normalized)
    if renderer is None:
        raise ValueError(
            f"unsupported shell {shell!r}; expected one of {', '.join(SUPPORTED_SHELLS)}"
        )
    return renderer()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            f"usage: python -m kite.shell_completion <{'|'.join(SUPPORTED_SHELLS)}>",
            file=sys.stderr,
        )
        return 2
    try:
        script = render(args[0])
    except ValueError as exc:
        print(f"kite.shell_completion: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(script)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
