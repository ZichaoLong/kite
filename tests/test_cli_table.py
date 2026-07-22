import unittest

from kite.cli_table import render_table, terminal_display_width


class CliTableTests(unittest.TestCase):
    def _visual_cell_starts(self, line: str, cells: list[str]) -> list[int]:
        starts: list[int] = []
        offset = 0
        for cell in cells:
            start = line.find(cell, offset)
            self.assertNotEqual(start, -1)
            starts.append(terminal_display_width(line[:start]))
            offset = start + len(cell)
        return starts

    def test_render_table_aligns_wide_characters(self) -> None:
        headers = ["SESSION_ID", "AGENT", "CWD", "TITLE"]
        rows = [
            ["session-1", "default", "/tmp/项目", "修复对齐"],
            ["session-22", "-", "/tmp/demo", "ascii title"],
        ]

        rendered = render_table(headers, rows)

        self.assertEqual(terminal_display_width("项目"), 4)
        self.assertEqual(terminal_display_width("e\u0301"), 1)
        self.assertNotIn("\t", "\n".join(rendered))
        header_starts = self._visual_cell_starts(rendered[0], headers)
        self.assertEqual(self._visual_cell_starts(rendered[1], rows[0]), header_starts)
        self.assertEqual(self._visual_cell_starts(rendered[2], rows[1]), header_starts)

    def test_render_table_rejects_column_count_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            render_table(["A", "B"], [["only-one-cell"]])

    def test_render_table_without_headers_renders_nothing(self) -> None:
        self.assertEqual(render_table([], [["ignored"]]), [])

    def test_terminal_display_width_ignores_non_printing_characters(self) -> None:
        self.assertEqual(terminal_display_width("abc"), 3)
        self.assertEqual(terminal_display_width("a\r\nb"), 2)
        self.assertEqual(terminal_display_width("a\u200bb"), 2)


if __name__ == "__main__":
    unittest.main()
