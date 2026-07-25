"""Tests for the terminal-card marker contract and text projection.

Locks the FOCUS-ported contract (kite/card_text_projection.py): marker
placement, build-side guards (injection / embedded image / over budget),
terminal-card recognition in both the as-sent and Feishu history
re-rendered shapes, and the checksum verification /last relies on.
"""

import unittest
from unittest import mock

from kite import cards
from kite.card_text_projection import (
    TERMINAL_RESULT_CARD_MARKER,
    TERMINAL_RESULT_SOURCE_CARD_DEGRADED,
    TERMINAL_RESULT_SOURCE_CARD_LEGACY,
    TERMINAL_RESULT_SOURCE_NONE,
    can_render_terminal_result_card,
    is_terminal_result_card,
    project_interactive_card_text,
    terminal_result_checksum,
    verify_terminal_result_checksum,
)
from kite.cards import build_approval_card, build_terminal_card


def _terminal_element(card: dict) -> dict:
    return card["body"]["elements"][0]


class TerminalMarkerPlacementTests(unittest.TestCase):
    def test_plain_text_ends_with_marker(self) -> None:
        card = build_terminal_card(outcome="completed", text="全部完成。")
        content = _terminal_element(card)["content"]
        self.assertTrue(content.endswith(TERMINAL_RESULT_CARD_MARKER))
        self.assertEqual(content, f"全部完成。{TERMINAL_RESULT_CARD_MARKER}")

    def test_marker_goes_on_new_line_after_closing_fence(self) -> None:
        card = build_terminal_card(outcome="completed", text="```bash\necho ok\n```")
        content = _terminal_element(card)["content"]
        self.assertIn(TERMINAL_RESULT_CARD_MARKER, content)
        # The marker never glues onto the closing fence.
        self.assertNotIn(f"```{TERMINAL_RESULT_CARD_MARKER}", content)
        self.assertIn(f"```\n", content)
        stripped = content[: -len(TERMINAL_RESULT_CARD_MARKER)]
        self.assertTrue(stripped.rstrip().endswith("```"))

    def test_empty_text_fallback_has_no_marker(self) -> None:
        card = build_terminal_card(outcome="completed", text="")
        content = _terminal_element(card)["content"]
        self.assertIn("无最终输出", content)
        self.assertNotIn(TERMINAL_RESULT_CARD_MARKER, content)
        self.assertNotIn("element_id", _terminal_element(card))


class TerminalGuardTests(unittest.TestCase):
    def test_can_render_fail_closed_on_marker_injection(self) -> None:
        self.assertFalse(
            can_render_terminal_result_card(
                f"包含{TERMINAL_RESULT_CARD_MARKER}隐藏标记", char_limit=1000
            )
        )

    def test_can_render_fail_closed_on_embedded_image(self) -> None:
        self.assertFalse(
            can_render_terminal_result_card(
                "![示意图](/tmp/phase1_report_diagram.png)", char_limit=1000
            )
        )

    def test_can_render_fail_closed_on_empty_and_over_budget(self) -> None:
        self.assertFalse(can_render_terminal_result_card("", char_limit=1000))
        self.assertFalse(can_render_terminal_result_card("  ", char_limit=1000))
        self.assertFalse(can_render_terminal_result_card("x" * 100, char_limit=10))
        self.assertFalse(can_render_terminal_result_card("文本", char_limit=0))
        self.assertTrue(can_render_terminal_result_card("文本", char_limit=1000))

    def test_injected_marker_gets_safe_rendering_without_id(self) -> None:
        injected = f"包含{TERMINAL_RESULT_CARD_MARKER}隐藏标记"
        card = build_terminal_card(
            outcome="completed", text=injected, terminal_result_id="p-1"
        )
        element = _terminal_element(card)
        # The builder appends no second marker and stamps no element id, so
        # the injected marker can never acquire a verifying checksum.
        self.assertEqual(element["content"], injected)
        self.assertEqual(element["content"].count(TERMINAL_RESULT_CARD_MARKER), 1)
        self.assertNotIn("element_id", element)

    def test_embedded_image_gets_safe_rendering_without_id(self) -> None:
        card = build_terminal_card(
            outcome="completed",
            text="![示意图](/tmp/a.png)",
            terminal_result_id="p-1",
        )
        element = _terminal_element(card)
        self.assertNotIn(TERMINAL_RESULT_CARD_MARKER, element["content"])
        self.assertIn("【图片】", element["content"])
        self.assertNotIn("element_id", element)

    def test_over_budget_text_gets_safe_rendering_without_id(self) -> None:
        with mock.patch.object(cards, "TERMINAL_RESULT_CARD_CHAR_LIMIT", 10):
            card = build_terminal_card(
                outcome="completed", text="x" * 100, terminal_result_id="p-1"
            )
        element = _terminal_element(card)
        self.assertEqual(element["content"], "x" * 100)
        self.assertNotIn(TERMINAL_RESULT_CARD_MARKER, element["content"])
        self.assertNotIn("element_id", element)


class TerminalProjectionAsSentTests(unittest.TestCase):
    def test_completed_card_projects_text_id_and_checksum(self) -> None:
        card = build_terminal_card(
            outcome="completed", text="最终答复", terminal_result_id="p-1"
        )
        projection = project_interactive_card_text(card)
        self.assertEqual(projection.final_reply_text, "最终答复")
        self.assertEqual(projection.text, "最终答复")
        self.assertEqual(projection.terminal_result_id, "p-1")
        self.assertEqual(len(projection.terminal_result_checksum), 16)
        self.assertEqual(
            projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_DEGRADED
        )
        self.assertTrue(projection.has_verifiable_terminal_result)
        self.assertTrue(verify_terminal_result_checksum(projection))
        self.assertNotIn(TERMINAL_RESULT_CARD_MARKER, projection.text)
        self.assertIn("Kimi 执行结果", projection.visible_text)
        self.assertNotIn("Kimi 执行结果", projection.text)

    def test_projected_text_is_the_rendered_text_and_still_verifies(self) -> None:
        # Sanitization rewrites the raw text (list hardening appends <br>);
        # the stamped checksum binds the rendered text, so the projection of
        # the sanitized form still verifies.
        raw_text = "1. **明确一次性任务**\n   用精确时间："
        card = build_terminal_card(
            outcome="completed", text=raw_text, terminal_result_id="p-1"
        )
        projection = project_interactive_card_text(card)
        self.assertEqual(
            projection.final_reply_text, "1. **明确一次性任务**<br>\n   用精确时间："
        )
        self.assertNotEqual(projection.final_reply_text, raw_text)
        self.assertTrue(verify_terminal_result_checksum(projection))

    def test_tampered_card_fails_checksum_verification(self) -> None:
        card = build_terminal_card(
            outcome="completed", text="权威原文", terminal_result_id="p-1"
        )
        # Tamper: change the text, keep the stamped element id.
        card["body"]["elements"][0]["content"] = (
            f"被篡改的文本{TERMINAL_RESULT_CARD_MARKER}"
        )
        projection = project_interactive_card_text(card)
        self.assertEqual(projection.final_reply_text, "被篡改的文本")
        self.assertEqual(projection.terminal_result_id, "p-1")
        self.assertFalse(verify_terminal_result_checksum(projection))

    def test_marker_only_card_is_legacy_and_unverifiable(self) -> None:
        card = build_terminal_card(outcome="completed", text="无 id 终态")
        projection = project_interactive_card_text(card)
        self.assertEqual(projection.final_reply_text, "无 id 终态")
        self.assertEqual(projection.terminal_result_id, "")
        self.assertEqual(
            projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_LEGACY
        )
        self.assertFalse(projection.has_verifiable_terminal_result)
        self.assertFalse(verify_terminal_result_checksum(projection))

    def test_aborted_and_failed_variants_are_recognized(self) -> None:
        for outcome, template in (("aborted", "grey"), ("failed", "red")):
            card = build_terminal_card(
                outcome=outcome, text="终态文本", terminal_result_id="p-1"  # type: ignore[arg-type]
            )
            self.assertEqual(card["header"]["template"], template)
            self.assertTrue(is_terminal_result_card(card))
            projection = project_interactive_card_text(card)
            self.assertEqual(projection.final_reply_text, "终态文本")
            self.assertTrue(verify_terminal_result_checksum(projection))

    def test_wrong_template_is_not_recognized(self) -> None:
        card = build_terminal_card(
            outcome="completed", text="终态", terminal_result_id="p-1"
        )
        card["header"]["template"] = "blue"
        self.assertFalse(is_terminal_result_card(card))
        projection = project_interactive_card_text(card)
        self.assertEqual(projection.final_reply_text, "")
        self.assertEqual(projection.final_reply_source, TERMINAL_RESULT_SOURCE_NONE)

    def test_terminal_title_without_marker_is_not_recognized(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Kimi 执行结果"},
                    "template": "green",
                },
                "elements": [{"tag": "markdown", "content": "普通展示文本"}],
            }
        )
        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("Kimi 执行结果", projection.visible_text)
        self.assertIn("普通展示文本", projection.visible_text)


class TerminalProjectionHistoryShapeTests(unittest.TestCase):
    def test_history_rendered_shape_projects_joined_text(self) -> None:
        # Feishu's history re-render flattens the card: bare title string,
        # nested text nodes, no element ids (FOCUS fixture shape).
        content_dict = {
            "title": "Kimi 执行结果",
            "elements": [
                [
                    {"tag": "text", "text": "## 结论"},
                    {"tag": "text", "text": f"第一条\n第二条{TERMINAL_RESULT_CARD_MARKER}"},
                ]
            ],
        }
        self.assertTrue(is_terminal_result_card(content_dict))
        projection = project_interactive_card_text(content_dict)
        self.assertEqual(projection.final_reply_text, "## 结论\n第一条\n第二条")
        self.assertEqual(
            projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_LEGACY
        )
        self.assertFalse(verify_terminal_result_checksum(projection))

    def test_history_rendered_shape_with_element_id_verifies(self) -> None:
        # When the re-render preserves the element id and the exact text, the
        # projection verifies and /last may export it.
        checksum = terminal_result_checksum("历史终态")
        content_dict = {
            "title": "Kimi 执行结果",
            "elements": [
                {
                    "tag": "text",
                    "element_id": f"kite_tr_p-1_{checksum[:16]}",
                    "text": f"历史终态{TERMINAL_RESULT_CARD_MARKER}",
                }
            ],
        }
        projection = project_interactive_card_text(content_dict)
        self.assertEqual(projection.final_reply_text, "历史终态")
        self.assertEqual(projection.terminal_result_id, "p-1")
        self.assertEqual(projection.terminal_result_checksum, checksum[:16])
        self.assertEqual(
            projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_DEGRADED
        )
        self.assertTrue(verify_terminal_result_checksum(projection))

    def test_history_rendered_shape_without_marker_is_not_terminal(self) -> None:
        content_dict = {
            "title": "Kimi 执行结果",
            "elements": [[{"tag": "text", "text": "投影里 marker 丢了"}]],
        }
        self.assertFalse(is_terminal_result_card(content_dict))
        projection = project_interactive_card_text(content_dict)
        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("投影里 marker 丢了", projection.visible_text)


class GenericCardProjectionTests(unittest.TestCase):
    def test_ordinary_card_projects_visible_text_without_final_reply(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "外部卡片"},
                },
                "elements": [
                    {"tag": "markdown", "content": "这里是正文"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "不应进入投影"},
                            }
                        ],
                    },
                ],
            }
        )
        self.assertEqual(projection.final_reply_text, "")
        self.assertEqual(projection.final_reply_source, TERMINAL_RESULT_SOURCE_NONE)
        self.assertIn("外部卡片", projection.text)
        self.assertIn("这里是正文", projection.text)
        self.assertNotIn("不应进入投影", projection.text)

    def test_approval_card_is_not_a_terminal_card(self) -> None:
        card = build_approval_card(approval_id="a-1", prompt_id="p-1", tool_name="Bash")
        self.assertFalse(is_terminal_result_card(card))
        projection = project_interactive_card_text(card)
        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("Kimi 审批请求", projection.text)


if __name__ == "__main__":
    unittest.main()
