"""Group chat ingress + activation + /abort gating contract tests.

Covers the group-chat contract (docs/contracts/group-chat.md):
- the full ingress matrix (§3.2): p2p/group × admin/activated member/
  stranger × @/no-@ × slash/text, every cell with an explicit outcome;
- activation (§3.1): admin-only, persists, first activation of an unbound
  group creates+binds with the instance default cwd, deactivate stops member
  prompting immediately;
- /abort gating in groups (§3.4): initiator or admin, everyone else denied;
- fail-closed corruption (§4.3) and missing identity (§4.4).

The harness reuses the AppHandler fakes from test_app_handler (a real
BindingStore / GroupConfigStore in a temp dir and a real RuntimeLoop).
"""

from __future__ import annotations

import dataclasses
import unittest

from test_app_handler import (
    ADMIN_OPEN_ID,
    CHAT_ID,
    DEFAULT_CWD,
    AppHandlerTestCase,
)

from kite.feishu_transport import InboundAttachment, InboundMessage
from kite.stores.group_config_store import GROUP_MODE_MENTION_ONLY, GroupConfigStore

GROUP_CHAT_ID = "oc_group"
MEMBER_OPEN_ID = "ou_member"
BYSTANDER_OPEN_ID = "ou_bystander"
STRANGER_OPEN_ID = "ou_stranger"


def make_group_message(
    text: str,
    *,
    sender: str = MEMBER_OPEN_ID,
    chat_id: str = GROUP_CHAT_ID,
    mentioned: bool = True,
    message_id: str = "om_g1",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        msg_type="text",
        text=text,
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        bot_mentioned=mentioned,
        mentions=[],
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


def make_group_attachment(
    *,
    sender: str = MEMBER_OPEN_ID,
    chat_id: str = GROUP_CHAT_ID,
    message_id: str = "om_gatt",
) -> InboundAttachment:
    return InboundAttachment(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        attachment_type="image",
        resource_key="img_key",
        file_name="",
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


class GroupChatTestCase(AppHandlerTestCase):
    def send_group(self, text: str, **kwargs) -> None:
        self.handler.on_message(make_group_message(text, **kwargs))

    def activate_group(self, chat_id: str = GROUP_CHAT_ID, *, by: str = ADMIN_OPEN_ID) -> None:
        self.group_config_store.activate(chat_id, activated_by=by)

    def bind_group(self, session_id: str = "s-1", chat_id: str = GROUP_CHAT_ID) -> None:
        self.bind(session_id, chat_id=chat_id)

    def assert_no_reaction(self) -> None:
        """The 'silently ignored' cell: no reply, no kap REST call."""
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.rest.calls, [])


# ---------------------------------------------------------------------------
# p2p control column (behavior unchanged)
# ---------------------------------------------------------------------------


class P2pIngressControlTests(GroupChatTestCase):
    def test_p2p_admin_text_submits_prompt(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("做点事")
        self.assertEqual(len(self.rest.submissions), 1)

    def test_p2p_admin_mentioned_text_submits_prompt(self) -> None:
        # bot_mentioned is meaningless in p2p; the text still submits.
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.handler.on_message(
            dataclasses.replace(
                make_group_message("做点事", sender=ADMIN_OPEN_ID, chat_id=CHAT_ID),
                chat_type="p2p",
            )
        )
        self.assertEqual(len(self.rest.submissions), 1)

    def test_p2p_admin_slash_command_runs(self) -> None:
        self.send("/status")
        self.assertIn("尚未绑定会话", self.transport.last_text())

    def test_p2p_stranger_text_is_rejected(self) -> None:
        self.send("你好", sender=STRANGER_OPEN_ID)
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_p2p_stranger_slash_command_is_rejected(self) -> None:
        self.send("/status", sender=STRANGER_OPEN_ID)
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_p2p_stranger_help_is_allowed(self) -> None:
        self.send("/help", sender=STRANGER_OPEN_ID)
        self.assertIn("KITE 命令导航", self.transport.last_text())


# ---------------------------------------------------------------------------
# Non-activated group: everything ignored except admin slash commands
# ---------------------------------------------------------------------------


class NonActivatedGroupIngressTests(GroupChatTestCase):
    def test_admin_slash_command_runs(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("/status", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("绑定会话", self.transport.last_text())

    def test_admin_mentioned_text_gets_activate_hint_and_never_prompts(self) -> None:
        self.send_group("做点事", sender=ADMIN_OPEN_ID, mentioned=True)
        self.assertIn("尚未激活", self.transport.last_text())
        self.assertIn("/group activate", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_admin_plain_text_is_silently_ignored(self) -> None:
        self.send_group("做点事", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assert_no_reaction()

    def test_member_slash_command_gets_admin_only_hint(self) -> None:
        self.send_group("/status", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_member_mentioned_text_gets_not_activated_hint_and_never_prompts(self) -> None:
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertIn("尚未激活", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_member_plain_text_is_silently_ignored(self) -> None:
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assert_no_reaction()

    def test_member_group_activate_is_denied_and_nothing_is_written(self) -> None:
        self.send_group("/group activate", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        self.assertFalse(self.group_config_store.is_activated(GROUP_CHAT_ID))
        self.assertEqual(self.rest.calls, [])


# ---------------------------------------------------------------------------
# Activated group: members prompt via @bot+text; slash stays admin-only
# ---------------------------------------------------------------------------


class ActivatedGroupIngressTests(GroupChatTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_group()

    def test_admin_slash_command_runs(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("/status", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("绑定会话", self.transport.last_text())

    def test_member_slash_command_gets_admin_only_hint(self) -> None:
        self.send_group("/status", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_member_mentioned_text_submits_prompt_and_first_use_binds(self) -> None:
        self.send_group("帮我看看这段代码", sender=MEMBER_OPEN_ID, mentioned=True)
        methods = [(method, path) for method, path, _ in self.rest.calls]
        self.assertEqual(
            methods,
            [("POST", "/sessions"), ("GET", "/sessions/s-1"), ("POST", "/sessions/s-1/prompts")],
        )
        create_body = self.rest.calls[0][2]
        self.assertEqual(create_body["metadata"]["cwd"], DEFAULT_CWD)
        binding = self.store.load(GROUP_CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        submission = self.rest.submissions[0]
        self.assertEqual(
            submission["body"]["content"],
            [{"type": "text", "text": "帮我看看这段代码"}],
        )
        # The ownership record carries the initiating member (axis 4 sender).
        entry = self.handler.prompt_ownership.entry_of(submission["prompt_id"])
        assert entry is not None
        self.assertEqual(entry.chat_id, GROUP_CHAT_ID)
        self.assertEqual(entry.sender_open_id, MEMBER_OPEN_ID)
        self.assertIn("已提交", self.transport.last_text())

    def test_admin_mentioned_text_submits_prompt(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("做点事", sender=ADMIN_OPEN_ID, mentioned=True)
        self.assertEqual(len(self.rest.submissions), 1)

    def test_member_mentioned_empty_text_gets_prompt_hint(self) -> None:
        self.send_group("", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertIn("@我", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_member_plain_text_is_silently_ignored(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("随便聊聊", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assert_no_reaction()

    def test_admin_plain_text_is_silently_ignored(self) -> None:
        # The non-@ rule has no admin exception (§3.2).
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("做点事", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assert_no_reaction()

    def test_missing_identity_mentioned_text_never_prompts(self) -> None:
        # §4.4: sender identity missing -> treated as a non-member.
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("做点事", sender="", mentioned=True)
        self.assertIn("无法识别你的身份", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_member_attachment_is_silently_ignored(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.handler.on_attachment(make_group_attachment(sender=MEMBER_OPEN_ID))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.downloads, [])

    def test_admin_attachment_is_staged(self) -> None:
        # Staging needs the bound session's cwd to be a real directory.
        work_dir = self.data_dir / "work"
        work_dir.mkdir()
        self.bind_group("s-1")
        self.rest.add_session("s-1", cwd=str(work_dir))
        self.handler.on_attachment(make_group_attachment(sender=ADMIN_OPEN_ID))
        self.assertEqual(len(self.transport.downloads), 1)
        self.assertTrue(self.transport.replies)

    def test_non_activated_group_attachment_is_silently_ignored(self) -> None:
        self.group_config_store.deactivate(GROUP_CHAT_ID)
        self.handler.on_attachment(make_group_attachment(sender=ADMIN_OPEN_ID))
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.downloads, [])


# ---------------------------------------------------------------------------
# Activation (§3.1)
# ---------------------------------------------------------------------------


class GroupActivationTests(GroupChatTestCase):
    def test_activate_writes_config_and_persists_across_restart(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("/group activate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("已激活", self.transport.last_text())
        self.assertTrue(self.group_config_store.is_activated(GROUP_CHAT_ID))
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["activated_by"], ADMIN_OPEN_ID)
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)
        self.assertGreater(config["activated_at"], 0.0)
        # Restart simulation: a fresh store instance on the same data dir.
        reloaded = GroupConfigStore(self.data_dir)
        self.assertTrue(reloaded.is_activated(GROUP_CHAT_ID))

    def test_first_activation_of_unbound_group_creates_and_binds(self) -> None:
        self.send_group("/group activate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("已激活", self.transport.last_text())
        # The session was created with the instance default cwd and bound
        # before the activation was written (contract §3.1).
        methods = [(method, path) for method, path, _ in self.rest.calls]
        self.assertEqual(methods, [("POST", "/sessions")])
        create_body = self.rest.calls[0][2]
        self.assertEqual(create_body["metadata"]["cwd"], DEFAULT_CWD)
        binding = self.store.load(GROUP_CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        self.assertEqual(self.bound_sessions, ["s-1"])
        self.assertTrue(self.group_config_store.is_activated(GROUP_CHAT_ID))
        # ...and members can prompt right away.
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertEqual(len(self.rest.submissions), 1)

    def test_activate_is_admin_only(self) -> None:
        self.send_group("/group activate", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        self.assertFalse(self.group_config_store.is_activated(GROUP_CHAT_ID))

    def test_group_command_in_p2p_is_rejected(self) -> None:
        self.send("/group activate")
        self.assertIn("仅在群聊中可用", self.transport.last_text())
        self.assertFalse(self.group_config_store.is_activated(CHAT_ID))

    def test_group_without_subcommand_shows_usage(self) -> None:
        self.send_group("/group", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("用法", self.transport.last_text())

    def test_deactivate_stops_member_prompting_immediately(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("/group activate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertEqual(len(self.rest.submissions), 1)

        self.send_group("/group deactivate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("已停用", self.transport.last_text())
        self.assertFalse(self.group_config_store.is_activated(GROUP_CHAT_ID))

        # Immediate effect (§5): the very next member message is back to the
        # non-activated cell — hint on @, never a prompt.
        replies_before = len(self.transport.replies)
        self.send_group("再来一件", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertEqual(len(self.rest.submissions), 1)
        self.assertEqual(len(self.transport.replies), replies_before + 1)
        self.assertIn("尚未激活", self.transport.last_text())

    def test_status_shows_group_activation_state(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.send_group("/status", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("群聊：未激活", self.transport.last_text())
        self.send_group("/group activate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.send_group("/status", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn(f"群聊：已激活（{GROUP_MODE_MENTION_ONLY}）", self.transport.last_text())

    def test_corrupt_config_reads_as_non_activated_at_ingress(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group()
        # §4.3: corruption fails closed to silence, never to open.
        (self.data_dir / "group_configs.json").write_text("{ not json", encoding="utf-8")
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=True)
        self.assertIn("尚未激活", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_help_lists_group_command(self) -> None:
        self.send("/help")
        self.assertIn("/group", self.transport.last_text())


# ---------------------------------------------------------------------------
# /abort in groups (§3.4): initiator or admin
# ---------------------------------------------------------------------------


class GroupAbortTests(GroupChatTestCase):
    def _member_prompt_in_flight(self, sender: str = MEMBER_OPEN_ID) -> str:
        """A member submits a prompt in the activated group; returns prompt_id."""
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group()
        self.send_group("干活", sender=sender, mentioned=True)
        prompt_id = self.rest.submissions[-1]["prompt_id"]
        self.rest.set_prompts("s-1", active=prompt_id)
        return prompt_id

    def test_initiator_member_can_abort(self) -> None:
        prompt_id = self._member_prompt_in_flight()
        self.send_group("/abort", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertEqual(self.rest.aborts, [("s-1", prompt_id)])
        self.assertIn("已中止", self.transport.last_text())

    def test_bystander_member_cannot_abort(self) -> None:
        self._member_prompt_in_flight()
        self.send_group("/abort", sender=BYSTANDER_OPEN_ID, mentioned=False)
        self.assertEqual(self.rest.aborts, [])
        self.assertIn("发起者或管理员", self.transport.last_text())

    def test_admin_can_abort_members_prompt(self) -> None:
        prompt_id = self._member_prompt_in_flight()
        self.send_group("/abort", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertEqual(self.rest.aborts, [("s-1", prompt_id)])

    def test_member_abort_in_non_activated_group_is_admin_only(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.rest.set_prompts("s-1", active="p-1")
        self.send_group("/abort", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        self.assertEqual(self.rest.aborts, [])

    def test_abort_with_unknown_initiator_fails_closed_to_admin_only(self) -> None:
        # A prompt recorded without a sender (e.g. control-plane submit): no
        # member can claim initiator rights on it (fail-closed).
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group()
        self.handler.prompt_ownership.record("p-1", GROUP_CHAT_ID)
        self.rest.set_prompts("s-1", active="p-1")
        self.send_group("/abort", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertEqual(self.rest.aborts, [])
        self.assertIn("发起者或管理员", self.transport.last_text())
        # ...but the admin still can.
        self.send_group("/abort", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertEqual(self.rest.aborts, [("s-1", "p-1")])


if __name__ == "__main__":
    unittest.main()
