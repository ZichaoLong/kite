import unittest

from kite.card_limits import MAX_CARD_TABLES, count_card_tables, limit_card_tables


def _table(name: str) -> str:
    return f"| {name} | v |\n| --- | --- |\n| a | b |"


class CountCardTablesTests(unittest.TestCase):
    def test_no_tables(self) -> None:
        self.assertEqual(count_card_tables("plain text\n\n- item\n"), 0)

    def test_counts_single_table(self) -> None:
        self.assertEqual(count_card_tables(_table("t")), 1)

    def test_ignores_tables_inside_fenced_code(self) -> None:
        text = f"```\n{_table('t')}\n```"
        self.assertEqual(count_card_tables(text), 0)

    def test_pipe_lines_without_separator_are_not_tables(self) -> None:
        self.assertEqual(count_card_tables("| a | b |\n| c | d |"), 0)


class LimitCardTablesTests(unittest.TestCase):
    def test_under_limit_is_untouched(self) -> None:
        text = "\n\n".join(_table(f"t{i}") for i in range(MAX_CARD_TABLES))
        self.assertEqual(limit_card_tables(text), text)

    def test_overflow_tables_become_code_blocks(self) -> None:
        text = "\n\n".join(_table(f"t{i}") for i in range(MAX_CARD_TABLES + 2))
        limited = limit_card_tables(text)

        self.assertEqual(count_card_tables(limited), MAX_CARD_TABLES)
        # The overflow tables are preserved verbatim inside fences.
        self.assertIn("```\n" + _table(f"t{MAX_CARD_TABLES}") + "\n```", limited)
        self.assertIn(_table(f"t{MAX_CARD_TABLES + 1}"), limited)

    def test_custom_limit(self) -> None:
        text = "\n\n".join(_table(f"t{i}") for i in range(3))
        limited = limit_card_tables(text, max_tables=1)
        self.assertEqual(count_card_tables(limited), 1)

    def test_tables_inside_fences_do_not_consume_budget(self) -> None:
        fenced = f"```\n{_table('fenced')}\n```"
        text = fenced + "\n\n" + _table("live")
        limited = limit_card_tables(text, max_tables=1)
        self.assertEqual(limited, text)


if __name__ == "__main__":
    unittest.main()
