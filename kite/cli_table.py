"""Small table renderer for local CLI output."""

from __future__ import annotations

import unicodedata


def terminal_display_width(text: str) -> int:
    total = 0
    for ch in str(text):
        if ch in "\r\n":
            continue
        if unicodedata.combining(ch):
            continue
        if unicodedata.category(ch) == "Cf":
            continue
        total += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return total


def render_table(headers: list[str], rows: list[list[str]], *, gap: int = 2) -> list[str]:
    if not headers:
        return []
    normalized_rows = [[str(cell) for cell in row] for row in rows]
    widths = [terminal_display_width(header) for header in headers]
    for row in normalized_rows:
        if len(row) != len(headers):
            raise ValueError("表格列数不一致。")
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], terminal_display_width(cell))

    def _pad(cell: str, width: int) -> str:
        padding = max(width - terminal_display_width(cell), 0)
        return cell + (" " * padding)

    rendered: list[str] = []
    for row in [headers, *normalized_rows]:
        parts: list[str] = []
        for index, cell in enumerate(row):
            if index == len(headers) - 1:
                parts.append(cell)
                continue
            parts.append(_pad(cell, widths[index]) + (" " * gap))
        rendered.append("".join(parts).rstrip())
    return rendered
