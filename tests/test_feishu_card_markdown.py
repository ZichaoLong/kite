import unittest

from kite.cards import build_execution_card
from kite.feishu_card_markdown import (
    sanitize_runtime_markdown_for_feishu_card,
    sanitize_terminal_result_markdown_for_feishu_json2,
)


class FeishuCardMarkdownTests(unittest.TestCase):
    def test_terminal_result_hardens_ordered_list_continuation_soft_break(self) -> None:
        text = "1. **明确一次性任务**\n   用精确时间："

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "1. **明确一次性任务**<br>\n   用精确时间：",
        )

    def test_runtime_card_hardens_ordered_list_continuation_soft_break(self) -> None:
        text = "1. **明确一次性任务**\n   用精确时间："

        self.assertEqual(
            sanitize_runtime_markdown_for_feishu_card(text),
            "1. **明确一次性任务**<br>\n   用精确时间：",
        )

    def test_execution_card_builder_hardens_list_continuation_soft_break(self) -> None:
        card = build_execution_card(
            session_title="会话",
            session_id="sess-1",
            prompt_text="1. **指令步骤**\n   检查状态",
            tool_lines=["1. **回复步骤**\n   输出结论"],
        )

        markdown_blocks = _collect_markdown_blocks(card)
        self.assertIn("1. **指令步骤**<br>\n   检查状态", "\n".join(markdown_blocks))
        self.assertIn("1. **回复步骤**<br>\n   输出结论", "\n".join(markdown_blocks))

    def test_list_continuation_hardening_marks_parent_before_nested_lists(self) -> None:
        text = "1. 外层\n    - 内层\n2. 另一项"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "1. 外层<br>\n    - 内层\n2. 另一项",
        )

    def test_runtime_card_neutralizes_raw_rss_xml_outside_fenced_code(self) -> None:
        text = (
            '<?xml version="1.0"?><rss><channel><item><title>示例</title></item>'
            "<br></channel></rss>"
        )

        self.assertEqual(
            sanitize_runtime_markdown_for_feishu_card(text),
            (
                '＜?xml version="1.0"?>＜rss>＜channel>＜item>＜title>示例'
                "＜/title>＜/item><br>＜/channel>＜/rss>"
            ),
        )

    def test_runtime_card_preserves_xml_inside_fenced_code(self) -> None:
        text = "```xml\n<rss><item>示例</item></rss>\n```"

        self.assertEqual(sanitize_runtime_markdown_for_feishu_card(text), text)

    def test_runtime_card_preserves_markup_inside_closed_inline_code(self) -> None:
        text = "使用 `<section>`，以及 ``<tag attr=`value`>``。"

        self.assertEqual(sanitize_runtime_markdown_for_feishu_card(text), text)

    def test_terminal_result_normalizes_uri_and_email_autolinks_to_plain_targets(self) -> None:
        text = "文档：<https://open.feishu.cn/path?q=1> 邮箱：<user@example.com>"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "文档：https://open.feishu.cn/path?q=1 邮箱：user@example.com",
        )

    def test_runtime_card_does_not_protect_unclosed_inline_code_or_autolink(self) -> None:
        text = (
            "未闭合代码：`<section>\n"
            "未闭合链接：<https://example.com\n"
            "非法链接：<https://example.com path>"
        )

        self.assertEqual(
            sanitize_runtime_markdown_for_feishu_card(text),
            (
                "未闭合代码：`＜section>\n"
                "未闭合链接：＜https://example.com\n"
                "非法链接：＜https://example.com path>"
            ),
        )

    def test_runtime_card_keeps_code_context_separate_from_raw_markup(self) -> None:
        text = "代码：`<section>`；原文：<section>正文</section>"

        self.assertEqual(
            sanitize_runtime_markdown_for_feishu_card(text),
            "代码：`<section>`；原文：＜section>正文＜/section>",
        )

    def test_terminal_result_neutralizes_raw_html_outside_fenced_code(self) -> None:
        text = "结果：<section data-kind=\"summary\">完成</section>"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "结果：＜section data-kind=\"summary\">完成＜/section>",
        )

    def test_runtime_card_neutralizes_complex_markup_openers_without_parsing_declarations(self) -> None:
        text = (
            "<![CDATA[<p>hello</p>]]>\n"
            "<!-- <tag>comment</tag> -->\n"
            "<!DOCTYPE root [<!ELEMENT root (#PCDATA)>]>\n"
            "1 < 2\n"
            "<br><BR/><br />"
        )

        self.assertEqual(
            sanitize_runtime_markdown_for_feishu_card(text),
            (
                "＜![CDATA[＜p>hello＜/p>]]>\n"
                "＜!-- ＜tag>comment＜/tag> -->\n"
                "＜!DOCTYPE root [＜!ELEMENT root (#PCDATA)>]>\n"
                "1 < 2\n"
                "<br><BR/><br />"
            ),
        )

    def test_nested_list_item_continuation_keeps_child_order_for_feishu(self) -> None:
        text = "2. xxxx：\n  - yyyy\n     zzzz"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "2. xxxx：<br>\n  - yyyy<br>\n     zzzz",
        )

    def test_parent_continuation_after_nested_list_is_not_hoisted_by_feishu(self) -> None:
        text = "2. xxxx：\n   - yyyy\n\n   zzzz\n3. next"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "2. xxxx：<br>\n   - yyyy\n\nzzzz\n3. next",
        )

    def test_grandchild_list_before_child_continuation_gets_hard_break(self) -> None:
        text = (
            "1. 父项 A：\n"
            "   - 子项 A1：\n"
            "     - 孙项 A1-a\n"
            "     - 孙项 A1-b\n"
            "     回到子项 A1 的续行。\n"
            "   - 子项 A2：\n"
            "     子项 A2 的续行。"
        )

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            (
                "1. 父项 A：<br>\n"
                "   - 子项 A1：<br>\n"
                "     - 孙项 A1-a\n"
                "     - 孙项 A1-b<br>\n"
                "     回到子项 A1 的续行。\n"
                "   - 子项 A2：<br>\n"
                "     子项 A2 的续行。"
            ),
        )

    def test_grandchild_list_after_blank_keeps_child_continuation_indent(self) -> None:
        text = (
            "2. 父项 B：\n"
            "   - 子项 B1：\n"
            "     - 孙项 B1-a\n"
            "\n"
            "     空行后回到子项 B1 的续行。\n"
            "   - 子项 B2：\n"
            "     子项 B2 的续行。"
        )

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            (
                "2. 父项 B：<br>\n"
                "   - 子项 B1：<br>\n"
                "     - 孙项 B1-a\n"
                "\n"
                "     空行后回到子项 B1 的续行。\n"
                "   - 子项 B2：<br>\n"
                "     子项 B2 的续行。"
            ),
        )

    def test_grandchild_list_before_child_sibling_does_not_get_hard_break(self) -> None:
        text = (
            "1. 父项：\n"
            "   - 子项 A：\n"
            "     - 孙项 A1\n"
            "   - 子项 B："
        )

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            (
                "1. 父项：<br>\n"
                "   - 子项 A：<br>\n"
                "     - 孙项 A1\n"
                "   - 子项 B："
            ),
        )

    def test_nested_fenced_code_block_clears_list_projection_context(self) -> None:
        text = (
            "1. 父项：\n"
            "   - 子项：\n"
            "     ```python\n"
            "     x = 1\n"
            "     ```\n"
            "   父项续行"
        )

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            (
                "1. 父项：<br>\n"
                "   - 子项：\n\n"
                "```python\n"
                "x = 1\n"
                "```\n\n"
                "   父项续行"
            ),
        )

    def test_parent_continuation_before_nested_list_gets_hard_break(self) -> None:
        text = "3. xxxx：\n   yyyy：\n   - zzzz"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "3. xxxx：<br>\n   yyyy：<br>\n   - zzzz",
        )

    def test_list_continuation_hardening_keeps_indented_code_like_list_text(self) -> None:
        text = "    1. code-like line\n       still code"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            text,
        )

    def test_nested_list_item_continuation_can_be_hardened(self) -> None:
        text = "1. outer\n    1. inner\n       detail"

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            "1. outer<br>\n    1. inner<br>\n       detail",
        )

    def test_list_continuation_hardening_skips_fenced_code_blocks(self) -> None:
        text = (
            "```markdown\n"
            "1. **示例**\n"
            "   不应改写\n"
            "```\n"
            "1. **示例**\n"
            "   应该换行"
        )

        self.assertEqual(
            sanitize_terminal_result_markdown_for_feishu_json2(text),
            (
                "```markdown\n"
                "1. **示例**\n"
                "   不应改写\n"
                "```\n\n"
                "1. **示例**<br>\n"
                "   应该换行"
            ),
        )


def _collect_markdown_blocks(node: object) -> list[str]:
    if isinstance(node, dict):
        blocks: list[str] = []
        if node.get("tag") == "markdown":
            blocks.append(str(node.get("content", "")))
        for value in node.values():
            blocks.extend(_collect_markdown_blocks(value))
        return blocks
    if isinstance(node, list):
        blocks: list[str] = []
        for item in node:
            blocks.extend(_collect_markdown_blocks(item))
        return blocks
    return []


if __name__ == "__main__":
    unittest.main()
