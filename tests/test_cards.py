import json
import unittest

from kite.cards import (
    ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
    ACTION_APPROVAL_RESOLVE,
    ACTION_PROMPT_ABORT,
    APPROVAL_ALREADY_PROCESSED_NOTICE,
    EXECUTION_STATE_FROZEN_DONE,
    EXECUTION_STATE_FROZEN_UNKNOWN,
    EXECUTION_STATE_RUNNING,
    ExecutionCardAnchor,
    QuestionItemSpec,
    QuestionOptionSpec,
    build_approval_card,
    build_approval_expired_card,
    build_approval_resolved_card,
    build_execution_card,
    build_question_text,
    build_terminal_card,
    terminal_result_checksum,
    terminal_result_element_id,
)


def _collect_buttons(node: object) -> list[dict]:
    if isinstance(node, dict):
        buttons: list[dict] = []
        if node.get("tag") == "button":
            buttons.append(node)
        for value in node.values():
            buttons.extend(_collect_buttons(value))
        return buttons
    if isinstance(node, list):
        buttons = []
        for item in node:
            buttons.extend(_collect_buttons(item))
        return buttons
    return []


def _collect_markdown(node: object) -> list[str]:
    if isinstance(node, dict):
        blocks: list[str] = []
        if node.get("tag") == "markdown":
            blocks.append(str(node.get("content", "")))
        for value in node.values():
            blocks.extend(_collect_markdown(value))
        return blocks
    if isinstance(node, list):
        blocks = []
        for item in node:
            blocks.extend(_collect_markdown(item))
        return blocks
    return []


def _collect_panels(node: object) -> list[dict]:
    if isinstance(node, dict):
        panels: list[dict] = []
        if node.get("tag") == "collapsible_panel":
            panels.append(node)
        for value in node.values():
            panels.extend(_collect_panels(value))
        return panels
    if isinstance(node, list):
        panels = []
        for item in node:
            panels.extend(_collect_panels(item))
        return panels
    return []


class ExecutionCardAnchorTests(unittest.TestCase):
    def test_anchor_fields(self) -> None:
        anchor = ExecutionCardAnchor(
            chat_id="oc_1",
            session_id="sess-1",
            prompt_id="p-1",
            card_message_id="om_1",
        )
        self.assertEqual(anchor.chat_id, "oc_1")
        self.assertEqual(anchor.session_id, "sess-1")
        self.assertEqual(anchor.prompt_id, "p-1")
        self.assertEqual(anchor.card_message_id, "om_1")

    def test_matches_prompt_equal(self) -> None:
        anchor = ExecutionCardAnchor("oc_1", "sess-1", "p-1", "om_1")
        self.assertTrue(anchor.matches_prompt("p-1"))

    def test_matches_prompt_rejects_mismatch(self) -> None:
        anchor = ExecutionCardAnchor("oc_1", "sess-1", "p-1", "om_1")
        self.assertFalse(anchor.matches_prompt("p-2"))

    def test_matches_prompt_rejects_empty(self) -> None:
        anchor = ExecutionCardAnchor("oc_1", "sess-1", "p-1", "om_1")
        self.assertFalse(anchor.matches_prompt(None))
        self.assertFalse(anchor.matches_prompt(""))
        self.assertFalse(anchor.matches_prompt("   "))


class ExecutionCardTests(unittest.TestCase):
    def _build(self, **overrides: object) -> dict:
        kwargs = {
            "session_title": "我的会话",
            "session_id": "sess-1234567890",
            "prompt_text": "帮我修复测试",
        }
        kwargs.update(overrides)
        return build_execution_card(**kwargs)  # type: ignore[arg-type]

    def test_running_card_shape(self) -> None:
        card = self._build(state=EXECUTION_STATE_RUNNING, elapsed_seconds=12)
        json.dumps(card)  # transport sends card JSON strings
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "turquoise")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行过程（执行中 12s）")
        text = "\n".join(_collect_markdown(card))
        self.assertIn("我的会话", text)
        self.assertIn("帮我修复测试", text)

    def test_running_card_with_prompt_id_has_cancel_button(self) -> None:
        card = self._build(state=EXECUTION_STATE_RUNNING, prompt_id="p-1")
        buttons = _collect_buttons(card)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["text"]["content"], "取消执行")
        self.assertEqual(buttons[0]["type"], "danger")
        self.assertEqual(
            buttons[0]["value"],
            {
                "action": ACTION_PROMPT_ABORT,
                "prompt_id": "p-1",
                "session_id": "sess-1234567890",
            },
        )

    def test_running_card_without_prompt_id_has_no_button(self) -> None:
        card = self._build(state=EXECUTION_STATE_RUNNING)
        self.assertEqual(_collect_buttons(card), [])

    def test_frozen_cards_have_no_button(self) -> None:
        for state in (EXECUTION_STATE_FROZEN_DONE, EXECUTION_STATE_FROZEN_UNKNOWN):
            with self.subTest(state=state):
                card = self._build(state=state, prompt_id="p-1")
                self.assertEqual(_collect_buttons(card), [])

    def test_running_card_without_elapsed(self) -> None:
        card = self._build()
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行过程（执行中）")

    def test_queue_length_shown_only_when_positive(self) -> None:
        with_queue = self._build(queue_length=3)
        self.assertIn("还有 3 条 prompt 排队中", "\n".join(_collect_markdown(with_queue)))
        without_queue = self._build(queue_length=0)
        self.assertNotIn("排队中", "\n".join(_collect_markdown(without_queue)))

    def test_tool_region_panel(self) -> None:
        card = self._build(tool_lines=["- `Bash` pytest -q", "- `Edit` kite/cards.py"])
        panels = _collect_panels(card)
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["header"]["title"]["content"], "工具调用（2）")
        text = "\n".join(_collect_markdown(panels[0]))
        self.assertIn("pytest -q", text)

    def test_no_tool_region_without_lines(self) -> None:
        card = self._build(tool_lines=[])
        self.assertEqual(_collect_panels(card), [])

    def test_frozen_done_state(self) -> None:
        card = self._build(state=EXECUTION_STATE_FROZEN_DONE)
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行过程（已结束）")

    def test_frozen_unknown_state_shows_troubleshooting_hint(self) -> None:
        card = self._build(state=EXECUTION_STATE_FROZEN_UNKNOWN)
        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行过程（状态未知）")
        text = "\n".join(_collect_markdown(card))
        self.assertIn("状态未知", text)
        self.assertIn("kitectl session status", text)

    def test_prompt_snippet_is_shortened(self) -> None:
        card = self._build(prompt_text="长" * 500)
        text = "\n".join(_collect_markdown(card))
        self.assertIn("长" * 199 + "…", text)
        self.assertNotIn("长" * 500, text)

    def test_markdown_is_sanitized(self) -> None:
        card = self._build(tool_lines=["1. **步骤**\n   继续"])
        text = "\n".join(_collect_markdown(card))
        self.assertIn("1. **步骤**<br>\n   继续", text)


class TerminalCardTests(unittest.TestCase):
    def test_completed_card(self) -> None:
        card = build_terminal_card(outcome="completed", text="全部完成。")
        json.dumps(card)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行结果")
        self.assertEqual(_collect_markdown(card), ["全部完成。"])

    def test_aborted_card(self) -> None:
        card = build_terminal_card(outcome="aborted", text="已生成一半。")
        self.assertEqual(card["header"]["template"], "grey")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行结果（已中止）")

    def test_failed_card_shows_upstream_msg(self) -> None:
        card = build_terminal_card(outcome="failed", text="kap error 50001: boom")
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 执行结果（失败）")
        self.assertIn("boom", _collect_markdown(card)[0])

    def test_empty_text_fallbacks(self) -> None:
        for outcome, marker in (
            ("completed", "无最终输出"),
            ("aborted", "已中止"),
            ("failed", "执行失败"),
        ):
            card = build_terminal_card(outcome=outcome, text="")  # type: ignore[arg-type]
            self.assertIn(marker, _collect_markdown(card)[0])

    def test_element_id_stamped_with_result_id(self) -> None:
        card = build_terminal_card(
            outcome="completed",
            text="done",
            terminal_result_id="abc123",
        )
        element = card["body"]["elements"][0]
        self.assertEqual(
            element["element_id"],
            terminal_result_element_id("abc123", terminal_result_checksum("done")),
        )
        self.assertTrue(element["element_id"].startswith("kite_tr_"))

    def test_no_element_id_without_result_id(self) -> None:
        card = build_terminal_card(outcome="completed", text="done")
        self.assertNotIn("element_id", card["body"]["elements"][0])

    def test_checksum_helper(self) -> None:
        import hashlib

        self.assertEqual(terminal_result_checksum(""), "")
        self.assertEqual(
            terminal_result_checksum("done"),
            hashlib.sha256("done".encode("utf-8")).hexdigest(),
        )
        self.assertEqual(len(terminal_result_checksum("done")), 64)

    def test_element_id_requires_both_parts(self) -> None:
        self.assertEqual(terminal_result_element_id("", "abc"), "")
        self.assertEqual(terminal_result_element_id("id", ""), "")
        self.assertEqual(
            terminal_result_element_id("ID", "ABCDEF"),
            "kite_tr_id_abcdef",
        )


class ApprovalCardTests(unittest.TestCase):
    def test_three_buttons_carry_ids_and_decisions(self) -> None:
        card = build_approval_card(
            approval_id="a-1",
            prompt_id="p-1",
            tool_name="Bash",
            action="run command",
            detail="```bash\nrm -rf build\n```",
        )
        json.dumps(card)
        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 审批请求")

        buttons = _collect_buttons(card)
        self.assertEqual(len(buttons), 3)
        approve, reject, feedback = buttons
        self.assertEqual(approve["text"]["content"], "批准")
        self.assertEqual(approve["type"], "primary")
        self.assertEqual(
            approve["value"],
            {
                "action": ACTION_APPROVAL_RESOLVE,
                "decision": "approved",
                "approval_id": "a-1",
                "prompt_id": "p-1",
            },
        )
        self.assertEqual(reject["text"]["content"], "拒绝")
        self.assertEqual(reject["type"], "danger")
        self.assertEqual(
            reject["value"],
            {
                "action": ACTION_APPROVAL_RESOLVE,
                "decision": "rejected",
                "approval_id": "a-1",
                "prompt_id": "p-1",
            },
        )
        self.assertEqual(feedback["text"]["content"], "拒绝并反馈")
        self.assertEqual(
            feedback["value"],
            {
                "action": ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
                "approval_id": "a-1",
                "prompt_id": "p-1",
            },
        )

    def test_body_shows_tool_action_detail_and_timeout(self) -> None:
        card = build_approval_card(
            approval_id="a-1",
            prompt_id="p-1",
            tool_name="Bash",
            action="run command",
            detail="`pytest -q`",
            timeout_seconds=300,
        )
        text = "\n".join(_collect_markdown(card))
        self.assertIn("**工具**：`Bash`", text)
        self.assertIn("**操作**：run command", text)
        self.assertIn("`pytest -q`", text)
        self.assertIn("5 分钟", text)
        self.assertIn("不会自动批准", text)

    def test_empty_detail_fallback(self) -> None:
        card = build_approval_card(approval_id="a-1", prompt_id="p-1")
        self.assertIn("上游未提供审批详情", "\n".join(_collect_markdown(card)))

    def test_resolved_card_labels(self) -> None:
        for decision, label in (
            ("approved", "已批准"),
            ("rejected", "已拒绝"),
            ("cancelled", "已取消"),
            ("something-else", "已处理"),
        ):
            card = build_approval_resolved_card(decision=decision)
            self.assertEqual(card["header"]["template"], "grey")
            self.assertIn(label, _collect_markdown(card)[0])
            self.assertEqual(_collect_buttons(card), [])

    def test_resolved_card_with_feedback(self) -> None:
        card = build_approval_resolved_card(decision="rejected", feedback="请不要删除数据")
        text = _collect_markdown(card)[0]
        self.assertIn("已拒绝", text)
        self.assertIn("请不要删除数据", text)

    def test_expired_card(self) -> None:
        card = build_approval_expired_card(reason="服务重启后无法恢复")
        json.dumps(card)
        self.assertEqual(card["header"]["template"], "grey")
        self.assertEqual(card["header"]["title"]["content"], "Kimi 审批请求（已过期）")
        text = _collect_markdown(card)[0]
        self.assertIn("服务重启后无法恢复", text)
        self.assertIn("请重新发起操作，或在本地直接处理", text)
        self.assertEqual(_collect_buttons(card), [])

    def test_expired_card_default_reason(self) -> None:
        card = build_approval_expired_card()
        self.assertIn("该审批已过期。", _collect_markdown(card)[0])

    def test_already_processed_notice_is_a_notice(self) -> None:
        self.assertIn("已处理", APPROVAL_ALREADY_PROCESSED_NOTICE)


class QuestionTextTests(unittest.TestCase):
    def test_single_question_numbered_options(self) -> None:
        text = build_question_text(
            [
                QuestionItemSpec(
                    header="选择环境",
                    question="要在哪个环境执行？",
                    options=(
                        QuestionOptionSpec("开发", "本地开发环境"),
                        QuestionOptionSpec("生产"),
                    ),
                )
            ]
        )
        self.assertIn("**选择环境**", text)
        self.assertIn("要在哪个环境执行？", text)
        self.assertIn("1. 开发 — 本地开发环境", text)
        self.assertIn("2. 生产", text)
        self.assertIn("回复选项编号即可（如 `1`）。", text)
        self.assertIn("5 分钟内未回复将自动关闭（dismiss）。", text)

    def test_multi_question_reply_format(self) -> None:
        text = build_question_text(
            [
                QuestionItemSpec(question="问题一？", options=(QuestionOptionSpec("甲"), QuestionOptionSpec("乙"))),
                QuestionItemSpec(question="问题二？", options=(QuestionOptionSpec("丙"), QuestionOptionSpec("丁"))),
            ]
        )
        self.assertIn("**问题 1**", text)
        self.assertIn("**问题 2**", text)
        self.assertIn("`问题号:选项号`", text)

    def test_multi_select_and_allow_other_notes(self) -> None:
        text = build_question_text(
            [
                QuestionItemSpec(
                    question="选哪些？",
                    options=(QuestionOptionSpec("甲"), QuestionOptionSpec("乙")),
                    multi_select=True,
                    allow_other=True,
                )
            ]
        )
        self.assertIn("可多选", text)
        self.assertIn("其他：你的内容", text)

    def test_zero_timeout_omits_timeout_note(self) -> None:
        text = build_question_text(
            [QuestionItemSpec(question="Q?", options=(QuestionOptionSpec("甲"), QuestionOptionSpec("乙")))],
            timeout_seconds=0,
        )
        self.assertNotIn("dismiss", text)

    def test_empty_labels_are_skipped(self) -> None:
        text = build_question_text(
            [
                QuestionItemSpec(
                    question="Q?",
                    options=(QuestionOptionSpec(""), QuestionOptionSpec("有效")),
                )
            ]
        )
        self.assertIn("1. 有效", text)


if __name__ == "__main__":
    unittest.main()
