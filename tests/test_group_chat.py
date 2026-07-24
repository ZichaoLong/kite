"""Group chat ingress + activation + /abort gating contract tests.

Covers the group-chat contract (docs/contracts/group-chat.md):
- the full ingress matrix (§3.2): p2p/group × admin/activated member/
  stranger × @/no-@ × slash/text × mode(mention_only/assistant), every cell
  with an explicit outcome;
- activation (§3.1): admin-only, persists, first activation of an unbound
  group creates+binds with the instance default cwd, deactivate stops member
  prompting immediately;
- /group-mode (§2): admin-only, activated-group-only, validated values,
  shown in /status;
- assistant mode (§3.2/§3.3): member text is logged (the bot's own and
  identity-less messages never enter), @bot+text triggers with the
  log+backfill context envelope, the boundary advances only after a
  successful submit, and a history fetch failure blocks with the explicit
  notice (§4.5);
- /abort gating in groups (§3.4): initiator or admin, everyone else denied;
- fail-closed corruption (§4.3) and missing identity (§4.4).

The harness reuses the AppHandler fakes from test_app_handler (a real
BindingStore / GroupConfigStore in a temp dir and a real RuntimeLoop), plus
a real GroupLogStore and a GroupHistoryRecovery with a scripted
FakeListMessages from test_group_history.
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
from test_group_history import (
    FakeListMessages,
    make_history_item,
    make_name_of,
    make_render_text,
)

from kite.adapters.kap_server import KapError
from kite.feishu_transport import InboundAttachment, InboundMessage, ListedMessagesPage
from kite.group_history import GroupHistoryRecovery
from kite.identity_names import IdentityNames
from kite.stores.group_config_store import (
    GROUP_MODE_ASSISTANT,
    GROUP_MODE_MENTION_ONLY,
    GroupConfigStore,
)
from kite.stores.group_log_store import GroupLogStore

GROUP_CHAT_ID = "oc_group"
MEMBER_OPEN_ID = "ou_member"
BYSTANDER_OPEN_ID = "ou_bystander"
STRANGER_OPEN_ID = "ou_stranger"
BOT_OPEN_ID = "ou_kite_bot"
APP_ID = "cli_kite_self"

_DISPLAY_NAMES = {
    ADMIN_OPEN_ID: "管理员",
    MEMBER_OPEN_ID: "成员小王",
    BYSTANDER_OPEN_ID: "路人甲",
}


def make_group_message(
    text: str,
    *,
    sender: str = MEMBER_OPEN_ID,
    chat_id: str = GROUP_CHAT_ID,
    mentioned: bool = True,
    message_id: str = "om_g1",
    sender_type: str = "user",
    create_time: int = 0,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        msg_type="text",
        text=text,
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type=sender_type,
        bot_mentioned=mentioned,
        mentions=[],
        thread_id="",
        root_id="",
        parent_id="",
        create_time=create_time,
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
    def _make_handler(self, *, admins: set[str] | None = None, **overrides):
        # kited always wires the assistant-mode axis: a real GroupLogStore
        # plus a GroupHistoryRecovery whose REST fetch is scripted per test.
        if not hasattr(self, "group_log_store"):
            self.group_log_store = GroupLogStore(self.data_dir)
        if not hasattr(self, "history_fetch"):
            self.history_fetch = FakeListMessages()
        kwargs = dict(
            names=IdentityNames(lambda open_id: _DISPLAY_NAMES.get(open_id)),
            group_log_store=self.group_log_store,
            group_history=GroupHistoryRecovery(
                list_messages=self.history_fetch,
                render_text=make_render_text,
                name_of=make_name_of,
                log_store=self.group_log_store,
                app_id=APP_ID,
            ),
        )
        kwargs.update(overrides)
        return super()._make_handler(admins=admins, **kwargs)

    def send_group(self, text: str, **kwargs) -> None:
        self.handler.on_message(make_group_message(text, **kwargs))

    def activate_group(
        self,
        chat_id: str = GROUP_CHAT_ID,
        *,
        by: str = ADMIN_OPEN_ID,
        mode: str = GROUP_MODE_MENTION_ONLY,
    ) -> None:
        self.group_config_store.activate(chat_id, activated_by=by)
        if mode != GROUP_MODE_MENTION_ONLY:
            self.group_config_store.set_mode(chat_id, mode)

    def bind_group(self, session_id: str = "s-1", chat_id: str = GROUP_CHAT_ID) -> None:
        self.bind(session_id, chat_id=chat_id)

    def log_texts(self, chat_id: str = GROUP_CHAT_ID) -> list[str]:
        return [e["text"] for e in self.group_log_store.entries_since(chat_id, 0)]

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


# ---------------------------------------------------------------------------
# /group-mode (§2): admin-only, activated-group-only, validated
# ---------------------------------------------------------------------------


class GroupModeCommandTests(GroupChatTestCase):
    def test_group_mode_in_p2p_is_rejected(self) -> None:
        self.send("/group-mode assistant")
        self.assertIn("仅在群聊中可用", self.transport.last_text())

    def test_group_mode_is_admin_only(self) -> None:
        self.activate_group()
        self.send_group("/group-mode assistant", sender=MEMBER_OPEN_ID, mentioned=False)
        self.assertIn("仅管理员", self.transport.last_text())
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)

    def test_group_mode_requires_activated_group(self) -> None:
        self.send_group("/group-mode assistant", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("尚未激活", self.transport.last_text())
        self.assertIsNone(self.group_config_store.load(GROUP_CHAT_ID))

    def test_group_mode_switches_to_assistant_and_persists(self) -> None:
        self.activate_group()
        self.send_group("/group-mode assistant", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("assistant", self.transport.last_text())
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["mode"], GROUP_MODE_ASSISTANT)
        self.assertTrue(config["activated"])
        # Persists across a restart (fresh store instance on the same dir).
        reloaded = GroupConfigStore(self.data_dir)
        loaded = reloaded.load(GROUP_CHAT_ID)
        assert loaded is not None
        self.assertEqual(loaded["mode"], GROUP_MODE_ASSISTANT)

    def test_group_mode_switch_back_to_mention_only(self) -> None:
        self.activate_group(mode=GROUP_MODE_ASSISTANT)
        self.send_group("/group-mode mention_only", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("mention_only", self.transport.last_text())
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)

    def test_group_mode_invalid_value_shows_usage(self) -> None:
        self.activate_group()
        self.send_group("/group-mode all", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("用法", self.transport.last_text())
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)

    def test_group_mode_without_arg_shows_current_mode(self) -> None:
        self.activate_group()
        self.send_group("/group-mode", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn(f"当前群聊模式：{GROUP_MODE_MENTION_ONLY}", self.transport.last_text())

    def test_group_mode_same_mode_is_a_noop(self) -> None:
        self.activate_group(mode=GROUP_MODE_ASSISTANT)
        self.send_group("/group-mode assistant", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn("已是 assistant", self.transport.last_text())

    def test_status_shows_assistant_mode(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group(mode=GROUP_MODE_ASSISTANT)
        self.send_group("/status", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn(f"群聊：已激活（{GROUP_MODE_ASSISTANT}）", self.transport.last_text())

    def test_help_lists_group_mode_command(self) -> None:
        self.send("/help")
        self.assertIn("/group-mode", self.transport.last_text())

    def test_reactivate_restores_assistant_mode_preference(self) -> None:
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group(mode=GROUP_MODE_ASSISTANT)
        self.send_group("/group deactivate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.send_group("/group activate", sender=ADMIN_OPEN_ID, mentioned=False)
        self.assertIn(f"模式：{GROUP_MODE_ASSISTANT}", self.transport.last_text())
        config = self.group_config_store.load(GROUP_CHAT_ID)
        assert config is not None
        self.assertEqual(config["mode"], GROUP_MODE_ASSISTANT)
        self.assertTrue(config["activated"])


# ---------------------------------------------------------------------------
# Assistant mode ingress (§3.2): every member message is logged
# ---------------------------------------------------------------------------


class AssistantModeLoggingTests(GroupChatTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group(mode=GROUP_MODE_ASSISTANT)

    def test_member_plain_text_is_logged_silently(self) -> None:
        self.send_group(
            "随便聊聊", sender=MEMBER_OPEN_ID, mentioned=False,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assert_no_reaction()
        entries = self.group_log_store.entries_since(GROUP_CHAT_ID, 0)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["message_id"], "om_g1")
        self.assertEqual(entry["created_at"], 1720000001000)
        self.assertEqual(entry["sender_open_id"], MEMBER_OPEN_ID)
        self.assertEqual(entry["sender_name"], "成员小王")
        self.assertEqual(entry["text"], "随便聊聊")

    def test_admin_plain_text_is_logged_too(self) -> None:
        # Admins are members; the log rule has no admin exception (§3.2).
        self.send_group(
            "管理员也说话", sender=ADMIN_OPEN_ID, mentioned=False,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), ["管理员也说话"])

    def test_mention_only_mode_logs_nothing(self) -> None:
        self.group_config_store.set_mode(GROUP_CHAT_ID, GROUP_MODE_MENTION_ONLY)
        self.send_group("随便聊聊", sender=MEMBER_OPEN_ID, mentioned=False, message_id="om_g1")
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), [])

    def test_non_activated_group_logs_nothing(self) -> None:
        self.group_config_store.deactivate(GROUP_CHAT_ID)
        self.send_group("随便聊聊", sender=MEMBER_OPEN_ID, mentioned=False, message_id="om_g1")
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), [])

    def test_bot_own_message_never_enters_log_nor_triggers(self) -> None:
        # App senders (incl. this bot) are non-members (§3.2).
        self.send_group(
            "机器人自己的话", sender="ou_some_app", sender_type="app", mentioned=False,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assertEqual(self.log_texts(), [])
        # Even with an @mention it never triggers a prompt.
        self.send_group(
            "@自己 说话", sender="ou_some_app", sender_type="app", mentioned=True,
            message_id="om_g2", create_time=1720000002000,
        )
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), [])

    def test_own_open_id_never_enters_log(self) -> None:
        self.transport.bot_open_id = BOT_OPEN_ID
        self.send_group(
            "自己的 open_id", sender=BOT_OPEN_ID, mentioned=False,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), [])

    def test_missing_identity_never_enters_log(self) -> None:
        # §4.4: missing sender identity -> non-member.
        self.send_group(
            "匿名消息", sender="", mentioned=False,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assert_no_reaction()
        self.assertEqual(self.log_texts(), [])

    def test_missing_identity_mentioned_text_never_prompts(self) -> None:
        self.send_group(
            "做点事", sender="", mentioned=True,
            message_id="om_g1", create_time=1720000001000,
        )
        self.assertIn("无法识别你的身份", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.log_texts(), [])

    def test_mentioned_empty_text_gets_prompt_hint(self) -> None:
        self.send_group("", sender=MEMBER_OPEN_ID, mentioned=True, message_id="om_g1")
        self.assertIn("@我", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])
        # An empty trigger has nothing worth logging.
        self.assertEqual(self.log_texts(), [])


# ---------------------------------------------------------------------------
# Assistant mode trigger (§3.3): envelope, boundary discipline, fail-closed
# ---------------------------------------------------------------------------


class AssistantModeTriggerTests(GroupChatTestCase):
    T1 = 1720000001000
    T2 = 1720000002000
    T3 = 1720000003000
    T4 = 1720000004000

    def setUp(self) -> None:
        super().setUp()
        self.bind_group("s-1")
        self.rest.add_session("s-1")
        self.activate_group(mode=GROUP_MODE_ASSISTANT)

    def _chatter_and_trigger(self, trigger_text: str = "总结一下") -> None:
        self.send_group("第一条", mentioned=False, message_id="om_g1", create_time=self.T1)
        self.send_group("第二条", mentioned=False, message_id="om_g2", create_time=self.T2)
        self.send_group(trigger_text, mentioned=True, message_id="om_g3", create_time=self.T3)

    def _submitted_text(self) -> str:
        submission = self.rest.submissions[-1]
        return submission["body"]["content"][0]["text"]

    def test_trigger_submits_envelope_with_log_context(self) -> None:
        self._chatter_and_trigger()
        self.assertEqual(len(self.rest.submissions), 1)
        submitted = self._submitted_text()
        self.assertIn("<group_chat_scope>", submitted)
        self.assertIn("<group_chat_context>", submitted)
        self.assertIn("<group_chat_current_turn>", submitted)
        # The context carries the log since the (zero) boundary...
        self.assertIn("第一条", submitted)
        self.assertIn("第二条", submitted)
        # ...while the trigger message is the current turn, not the context.
        self.assertIn("sender_name: 成员小王", submitted)
        self.assertIn("总结一下", submitted)
        self.assertIn("已提交", self.transport.last_text())

    def test_boundary_advances_to_trigger_after_successful_submit(self) -> None:
        self._chatter_and_trigger()
        boundary = self.group_log_store.boundary(GROUP_CHAT_ID)
        self.assertEqual(boundary["seq"], 3)
        self.assertEqual(boundary["created_at"], self.T3)
        self.assertEqual(boundary["message_ids"], ["om_g3"])

    def test_second_trigger_context_starts_at_first_boundary(self) -> None:
        self._chatter_and_trigger()
        self.send_group("边界后", mentioned=False, message_id="om_g4", create_time=self.T4)
        self.send_group("再看看", mentioned=True, message_id="om_g5", create_time=self.T4 + 1000)
        submitted = self._submitted_text()
        self.assertIn("边界后", submitted)
        self.assertNotIn("第一条", submitted)
        self.assertNotIn("第二条", submitted)
        boundary = self.group_log_store.boundary(GROUP_CHAT_ID)
        self.assertEqual(boundary["seq"], 5)

    def test_trigger_merges_rest_backfill_into_context(self) -> None:
        self.history_fetch.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_h1", text="回捞内容", create_time=self.T2 + 500),
                ]
            )
        )
        self._chatter_and_trigger()
        submitted = self._submitted_text()
        self.assertIn("回捞内容", submitted)
        self.assertIn("第一条", submitted)

    def test_submit_failure_leaves_boundary_and_context(self) -> None:
        self.rest.submit_error = KapError(50001, "boom")
        self._chatter_and_trigger()
        self.assertIn("提交失败", self.transport.last_text())
        # Boundary untouched: the context is not lost silently.
        self.assertEqual(self.group_log_store.boundary(GROUP_CHAT_ID)["seq"], 0)
        self.assertEqual(self.log_texts(), ["第一条", "第二条", "总结一下"])

        # A later retry still sees the failed trigger's message in context.
        self.rest.submit_error = None
        self.send_group("再试试", mentioned=True, message_id="om_g4", create_time=self.T4)
        self.assertEqual(len(self.rest.submissions), 1)
        submitted = self._submitted_text()
        self.assertIn("第一条", submitted)
        self.assertIn("总结一下", submitted)
        self.assertEqual(self.group_log_store.boundary(GROUP_CHAT_ID)["seq"], 4)

    def test_fetch_failure_blocks_with_explicit_notice(self) -> None:
        # §4.5: never answer without the context; the log entry stays and no
        # kap call happens at all (the block precedes the prompt path).
        self.history_fetch.error = RuntimeError("code=230001, msg=rate limited")
        self._chatter_and_trigger()
        self.assertIn("获取群聊历史上下文失败", self.transport.last_text())
        self.assertIn("缺少上下文", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.group_log_store.boundary(GROUP_CHAT_ID)["seq"], 0)
        self.assertEqual(self.log_texts(), ["第一条", "第二条", "总结一下"])

    def test_trigger_first_use_creates_and_binds_with_raw_text_title(self) -> None:
        # Same first-use rule as mention_only (§3.1), but the session title
        # comes from the raw message, not the composed envelope.
        fresh_chat = "oc_group_fresh"
        self.rest.add_session("s-2")
        self.group_config_store.activate(fresh_chat, activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode(fresh_chat, GROUP_MODE_ASSISTANT)
        self.send_group(
            "帮我看看这段代码", sender=MEMBER_OPEN_ID, chat_id=fresh_chat,
            mentioned=True, message_id="om_f1", create_time=self.T1,
        )
        create_body = self.rest.calls[0][2]
        self.assertEqual(create_body["title"], "帮我看看这段代码")
        binding = self.store.load(fresh_chat)
        assert binding is not None
        self.assertEqual(len(self.rest.submissions), 1)

    def test_deactivate_stops_logging_and_triggering_immediately(self) -> None:
        self.send_group(
            "/group deactivate", sender=ADMIN_OPEN_ID, mentioned=False, message_id="om_g0"
        )
        self.send_group("做点事", sender=MEMBER_OPEN_ID, mentioned=True, message_id="om_g1")
        self.assertIn("尚未激活", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.log_texts(), [])

    def test_log_and_boundary_survive_restart(self) -> None:
        self._chatter_and_trigger()
        self.send_group("边界后", mentioned=False, message_id="om_g4", create_time=self.T4)
        # Restart simulation: fresh stores + handler on the same data dir.
        fresh_log_store = GroupLogStore(self.data_dir)
        fresh_handler = self._make_handler(
            group_config_store=GroupConfigStore(self.data_dir),
            group_log_store=fresh_log_store,
            group_history=GroupHistoryRecovery(
                list_messages=self.history_fetch,
                render_text=make_render_text,
                name_of=make_name_of,
                log_store=fresh_log_store,
                app_id=APP_ID,
            ),
        )
        fresh_handler.on_message(
            make_group_message("重启后触发", mentioned=True, message_id="om_g5", create_time=self.T4 + 1000)
        )
        self.assertEqual(len(self.rest.submissions), 2)
        submitted = self._submitted_text()
        # The reloaded boundary keeps pre-trigger chatter out of the context.
        self.assertIn("边界后", submitted)
        self.assertNotIn("第一条", submitted)
        # The seq counter survived: the new trigger is seq 5.
        self.assertEqual(fresh_log_store.boundary(GROUP_CHAT_ID)["seq"], 5)


if __name__ == "__main__":
    unittest.main()
