from __future__ import annotations

# Feishu card limit: max markdown tables per card (empirically ~5-10; stay conservative).
MAX_CARD_TABLES = 5


def _scan_tables(text: str) -> list[tuple[int, int]]:
    """Scan for markdown tables outside fenced code blocks; return (start, end) line ranges."""
    lines = text.split("\n")
    tables: list[tuple[int, int]] = []
    in_fence = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence and stripped.startswith("|") and stripped.endswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if sep.startswith("|") and "---" in sep:
                start = i
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|"):
                    j += 1
                tables.append((start, j))
                i = j
                continue
        i += 1
    return tables


def limit_card_tables(text: str, max_tables: int = MAX_CARD_TABLES) -> str:
    """Limit markdown tables in Feishu cards, preserving overflow as code blocks."""
    tables = _scan_tables(text)
    if len(tables) <= max_tables:
        return text

    lines = text.split("\n")
    for start, end in reversed(tables[max_tables:]):
        table_lines = lines[start:end]
        lines[start:end] = ["```", *table_lines, "```"]

    return "\n".join(lines)


def count_card_tables(text: str) -> int:
    """Count markdown tables outside fenced code blocks."""
    return len(_scan_tables(text))
