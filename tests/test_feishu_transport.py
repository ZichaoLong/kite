import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from kite.feishu_transport import (
    CardAction,
    CardActionResponse,
    FeishuTransport,
    InboundAttachment,
    InboundMessage,
    TransportHandler,
)


class _RecordingHandler(TransportHandler):
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.attachments: list[InboundAttachment] = []
        self.card_actions: list[CardAction] = []
        self.card_action_response = CardActionResponse()
        self.card_action_error: Exception | None = None
        self.message_error: Exception | None = None
        self.recalled: list[tuple[str, str]] = []
        self.unavailable: list[tuple[str, str]] = []
        self.menu_clicks: list[tuple[str, str]] = []

    def on_message(self, message: InboundMessage) -> None:
        if self.message_error is not None:
            raise self.message_error
        self.messages.append(message)

    def on_attachment(self, attachment: InboundAttachment) -> None:
        self.attachments.append(attachment)

    def on_card_action(self, action: CardAction) -> CardActionResponse:
        self.card_actions.append(action)
        if self.card_action_error is not None:
            raise self.card_action_error
        return self.card_action_response

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        self.recalled.append((chat_id, message_id))

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        self.unavailable.append((chat_id, reason))

    def on_bot_menu(self, open_id: str, event_key: str) -> None:
        self.menu_clicks.append((open_id, event_key))


def _message_event(
    *,
    message_id: str,
    chat_id: str = "oc-1",
    chat_type: str = "p2p",
    msg_type: str = "text",
    sender_user_id: str = "u-1",
    sender_open_id: str = "ou-1",
    sender_type: str = "user",
    content: object = None,
    raw_content: str | None = None,
    mentions: list | None = None,
    create_time: int = 1712476800000,
    thread_id: str = "",
    root_id: str = "",
    parent_id: str = "",
) -> P2ImMessageReceiveV1:
    if raw_content is not None:
        content_str = raw_content
    else:
        content_str = json.dumps(content if content is not None else {"text": "hello"}, ensure_ascii=False)
    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {"user_id": sender_user_id, "open_id": sender_open_id},
                    "sender_type": sender_type,
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "message_type": msg_type,
                    "content": content_str,
                    "mentions": mentions or [],
                    "create_time": create_time,
                    "thread_id": thread_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                },
            }
        }
    )


def _card_action_event(
    *,
    open_id: str = "ou-1",
    user_id: str = "u-1",
    chat_id: str = "oc-1",
    message_id: str = "om-card",
    value: dict | None = None,
    form_value: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(user_id=user_id, open_id=open_id),
            context=SimpleNamespace(open_chat_id=chat_id, open_message_id=message_id),
            action=SimpleNamespace(value=value if value is not None else {"action": "approve"}, form_value=form_value),
        )
    )


def _make_transport(handler: _RecordingHandler | None = None, **kwargs) -> FeishuTransport:
    handler = handler or _RecordingHandler()
    kwargs.setdefault("bot_open_id", "ou-bot")
    return FeishuTransport("app-id", "app-secret", handler, **kwargs)


def _ok_response(**data_attrs) -> Mock:
    response = Mock()
    response.success.return_value = True
    for key, value in data_attrs.items():
        setattr(response.data, key, value)
    return response


def _fail_response(code: object = 500, msg: str = "err", raw: object = None) -> Mock:
    response = Mock()
    response.success.return_value = False
    response.code = code
    response.msg = msg
    response.raw = raw
    return response


class InboundDispatchTests(unittest.TestCase):
    def test_text_message_dispatches_normalized_inbound(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(_message_event(message_id="om-1"))

        self.assertEqual(len(handler.messages), 1)
        message = handler.messages[0]
        self.assertEqual(message.message_id, "om-1")
        self.assertEqual(message.chat_id, "oc-1")
        self.assertEqual(message.chat_type, "p2p")
        self.assertEqual(message.msg_type, "text")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.sender_open_id, "ou-1")
        self.assertEqual(message.sender_user_id, "u-1")
        self.assertEqual(message.sender_type, "user")
        self.assertFalse(message.bot_mentioned)
        self.assertEqual(message.mentions, [])
        self.assertEqual(message.create_time, 1712476800000)

    def test_duplicate_message_dispatched_once(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)
        event = _message_event(message_id="om-dup")

        transport._on_raw_message(event)
        transport._on_raw_message(event)

        self.assertEqual(len(handler.messages), 1)

    def test_post_message_extracts_paragraph_text(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(
            _message_event(
                message_id="om-post",
                msg_type="post",
                content={
                    "title": "",
                    "content": [
                        [{"tag": "text", "text": "first"}],
                        [],
                        [{"tag": "text", "text": "- "}, {"tag": "text", "text": "second"}],
                    ],
                },
            )
        )

        self.assertEqual(handler.messages[0].text, "first\n\n- second")

    def test_group_mention_normalized_and_bot_mentioned(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)
        mentions = [
            {"key": "@_user_1", "name": "KiteBot", "id": {"open_id": "ou-bot"}},
            {"key": "@_user_2", "name": "Alice", "id": {"open_id": "ou-alice"}},
        ]

        transport._on_raw_message(
            _message_event(
                message_id="om-group",
                chat_type="group",
                content={"text": "@_user_1 @_user_2 hi there"},
                mentions=mentions,
            )
        )

        message = handler.messages[0]
        self.assertTrue(message.bot_mentioned)
        self.assertEqual(message.text, "@Alice hi there")
        self.assertEqual(
            message.mentions,
            [
                {"key": "@_user_1", "name": "KiteBot", "open_id": "ou-bot"},
                {"key": "@_user_2", "name": "Alice", "open_id": "ou-alice"},
            ],
        )

    def test_group_message_without_bot_mention(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)
        mentions = [{"key": "@_user_2", "name": "Alice", "id": {"open_id": "ou-alice"}}]

        transport._on_raw_message(
            _message_event(
                message_id="om-group-2",
                chat_type="group",
                content={"text": "@_user_2 ping"},
                mentions=mentions,
            )
        )

        self.assertFalse(handler.messages[0].bot_mentioned)

    def test_unparseable_content_dropped(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(_message_event(message_id="om-bad", raw_content="{not json"))

        self.assertEqual(handler.messages, [])

    def test_merge_forward_skipped_at_transport(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(
            _message_event(
                message_id="om-fwd",
                msg_type="merge_forward",
                raw_content="Merged and Forwarded Message",
            )
        )

        self.assertEqual(handler.messages, [])
        self.assertEqual(handler.attachments, [])

    def test_image_message_dispatches_attachment(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(
            _message_event(
                message_id="om-img",
                msg_type="image",
                content={"image_key": "img-key-1"},
            )
        )

        self.assertEqual(handler.messages, [])
        self.assertEqual(len(handler.attachments), 1)
        attachment = handler.attachments[0]
        self.assertEqual(attachment.attachment_type, "image")
        self.assertEqual(attachment.resource_key, "img-key-1")
        self.assertEqual(attachment.file_name, "")
        self.assertEqual(attachment.message_id, "om-img")

    def test_file_message_dispatches_attachment_with_name(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(
            _message_event(
                message_id="om-file",
                msg_type="file",
                content={"file_key": "file-key-1", "file_name": "report.pdf"},
            )
        )

        attachment = handler.attachments[0]
        self.assertEqual(attachment.attachment_type, "file")
        self.assertEqual(attachment.resource_key, "file-key-1")
        self.assertEqual(attachment.file_name, "report.pdf")

    def test_handler_exception_is_swallowed(self) -> None:
        handler = _RecordingHandler()
        handler.message_error = RuntimeError("boom")
        transport = _make_transport(handler)

        transport._on_raw_message(_message_event(message_id="om-err"))  # must not raise

    def test_interactive_message_extracts_empty_text(self) -> None:
        # Card text projection is application-layer business; the transport
        # passes interactive messages through with empty text.
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message(
            _message_event(
                message_id="om-card",
                msg_type="interactive",
                content={"elements": [], "header": {"title": {"content": "t"}}},
            )
        )

        self.assertEqual(handler.messages[0].text, "")
        self.assertEqual(handler.messages[0].msg_type, "interactive")


class CardActionTests(unittest.TestCase):
    def test_card_action_dispatches_and_builds_response(self) -> None:
        handler = _RecordingHandler()
        card = {"elements": [{"tag": "markdown", "content": "done"}]}
        handler.card_action_response = CardActionResponse(card=card, toast="approved", toast_type="success")
        transport = _make_transport(handler)

        response = transport._on_raw_card_action(
            _card_action_event(value={"action": "approve"}, form_value={"reason": "ok"})
        )

        self.assertEqual(len(handler.card_actions), 1)
        action = handler.card_actions[0]
        self.assertEqual(action.operator_open_id, "ou-1")
        self.assertEqual(action.operator_user_id, "u-1")
        self.assertEqual(action.chat_id, "oc-1")
        self.assertEqual(action.message_id, "om-card")
        self.assertEqual(action.value["action"], "approve")
        self.assertEqual(action.value["_operator_open_id"], "ou-1")
        self.assertEqual(action.value["_operator_user_id"], "u-1")
        self.assertEqual(action.value["_form_value"], {"reason": "ok"})

        self.assertIsInstance(response, P2CardActionTriggerResponse)
        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.toast.content, "approved")
        self.assertEqual(response.card.type, "raw")
        self.assertEqual(response.card.data, card)

    def test_card_action_default_response_is_empty(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        response = transport._on_raw_card_action(_card_action_event())

        self.assertIsNone(response.toast)
        self.assertIsNone(response.card)

    def test_card_action_handler_exception_returns_empty_response(self) -> None:
        handler = _RecordingHandler()
        handler.card_action_error = RuntimeError("boom")
        transport = _make_transport(handler)

        response = transport._on_raw_card_action(_card_action_event())

        self.assertIsInstance(response, P2CardActionTriggerResponse)
        self.assertIsNone(response.toast)
        self.assertIsNone(response.card)


class LifecycleEventTests(unittest.TestCase):
    def test_message_recalled_dispatches(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_message_recalled(
            SimpleNamespace(event=SimpleNamespace(message_id="om-1", chat_id="oc-1"))
        )

        self.assertEqual(handler.recalled, [("oc-1", "om-1")])

    def test_chat_disbanded_dispatches_and_purges_thread_cache(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)
        transport._on_raw_message(_message_event(message_id="om-t", thread_id="t-1"))
        self.assertEqual(transport._lookup_message_thread("om-t"), "t-1")

        transport._on_raw_chat_disbanded(SimpleNamespace(event=SimpleNamespace(chat_id="oc-1")))

        self.assertEqual(handler.unavailable, [("oc-1", "disbanded")])
        self.assertEqual(transport._lookup_message_thread("om-t"), "")

    def test_bot_removed_dispatches(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_chat_member_bot_deleted(
            SimpleNamespace(event=SimpleNamespace(chat_id="oc-9"))
        )

        self.assertEqual(handler.unavailable, [("oc-9", "bot_removed")])

    def test_bot_menu_dispatches(self) -> None:
        handler = _RecordingHandler()
        transport = _make_transport(handler)

        transport._on_raw_bot_menu(
            SimpleNamespace(
                event=SimpleNamespace(
                    operator=SimpleNamespace(
                        operator_id=SimpleNamespace(user_id="u-1", open_id="ou-1")
                    ),
                    event_key="settings",
                )
            )
        )

        self.assertEqual(handler.menu_clicks, [("ou-1", "settings")])


class OutboundSendTests(unittest.TestCase):
    def _transport_with_mock_client(self) -> tuple[FeishuTransport, Mock]:
        transport = _make_transport()
        transport.client = Mock()
        return transport, transport.client

    def test_send_message_get_id_request_shape(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.create.return_value = _ok_response(message_id="om-new")

        message_id = transport.send_message_get_id("oc-1", "text", json.dumps({"text": "hi"}))

        self.assertEqual(message_id, "om-new")
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "chat_id")
        self.assertEqual(request.request_body.receive_id, "oc-1")
        self.assertEqual(request.request_body.msg_type, "text")
        self.assertEqual(json.loads(request.request_body.content), {"text": "hi"})

    def test_send_message_detects_open_id_receive_type(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.create.return_value = _ok_response(message_id="om-new")

        transport.send_message("ou_target", "text", json.dumps({"text": "hi"}))

        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "open_id")
        self.assertEqual(request.request_body.receive_id, "ou_target")

    def test_send_message_failure_returns_none(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.create.return_value = _fail_response()

        self.assertIsNone(transport.send_message_get_id("oc-1", "text", "{}"))

    def test_send_message_sdk_exception_returns_none(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.create.side_effect = RuntimeError("network down")

        self.assertIsNone(transport.send_message_get_id("oc-1", "text", "{}"))

    def test_reply_card_sends_interactive_content(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.create.return_value = _ok_response(message_id="om-card")
        card = {"elements": [{"tag": "markdown", "content": "hi"}]}

        transport.reply_card("oc-1", card)

        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "interactive")
        self.assertEqual(json.loads(request.request_body.content), card)

    def test_reply_to_message_request_shape_and_thread_flag(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.reply.return_value = _ok_response(message_id="om-reply")

        reply_id = transport.reply_to_message("om-parent", "text", json.dumps({"text": "hi"}))

        self.assertEqual(reply_id, "om-reply")
        request = client.im.v1.message.reply.call_args.args[0]
        self.assertEqual(request.message_id, "om-parent")
        self.assertEqual(request.request_body.msg_type, "text")
        self.assertFalse(request.request_body.reply_in_thread)

    def test_reply_inherits_thread_from_parent_context(self) -> None:
        transport, client = self._transport_with_mock_client()
        transport._on_raw_message(_message_event(message_id="om-parent", thread_id="t-1"))
        client.im.v1.message.reply.return_value = _ok_response(message_id="om-reply")

        transport.reply_get_id("oc-1", "hi", parent_message_id="om-parent")

        request = client.im.v1.message.reply.call_args.args[0]
        self.assertTrue(request.request_body.reply_in_thread)

    def test_explicit_reply_in_thread_wins(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.reply.return_value = _ok_response(message_id="om-reply")

        transport.reply_get_id("oc-1", "hi", parent_message_id="om-unknown", reply_in_thread=True)

        request = client.im.v1.message.reply.call_args.args[0]
        self.assertTrue(request.request_body.reply_in_thread)

    def test_delete_message(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.delete.return_value = _ok_response()

        self.assertTrue(transport.delete_message("om-1"))
        request = client.im.v1.message.delete.call_args.args[0]
        self.assertEqual(request.message_id, "om-1")

    def test_delete_message_failure(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.delete.return_value = _fail_response()

        self.assertFalse(transport.delete_message("om-1"))


class PatchMessageTests(unittest.TestCase):
    def _transport_with_mock_client(self) -> tuple[FeishuTransport, Mock]:
        transport = _make_transport()
        transport.client = Mock()
        return transport, transport.client

    def test_patch_success(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.patch.return_value = _ok_response()

        result = transport.patch_message_result("om-1", "{}")

        self.assertTrue(result.ok)
        self.assertFalse(result.retryable)
        request = client.im.v1.message.patch.call_args.args[0]
        self.assertEqual(request.message_id, "om-1")
        self.assertEqual(request.request_body.content, "{}")
        self.assertTrue(transport.patch_message("om-1", "{}"))

    def test_patch_rate_limited_is_retryable(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.patch.return_value = _fail_response(code="230020", raw={"ext": "limited"})

        result = transport.patch_message_result("om-1", "{}")

        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertGreater(result.retry_after_seconds, 0)

    def test_patch_timeout_exception_is_retryable(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.patch.side_effect = TimeoutError("timed out")

        result = transport.patch_message_result("om-1", "{}")

        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)

    def test_patch_other_failure(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message.patch.return_value = _fail_response(code=230001)

        result = transport.patch_message_result("om-1", "{}")

        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertFalse(transport.patch_message("om-1", "{}"))


class AttachmentDownloadTests(unittest.TestCase):
    def _transport_with_mock_client(self) -> tuple[FeishuTransport, Mock]:
        transport = _make_transport()
        transport.client = Mock()
        return transport, transport.client

    def test_download_message_resource(self) -> None:
        transport, client = self._transport_with_mock_client()
        response = _ok_response()
        response.file = io.BytesIO(b"file-bytes")
        response.file_name = "report.pdf"
        response.raw = SimpleNamespace(headers={"Content-Type": "application/pdf"})
        client.im.v1.message_resource.get.return_value = response

        resource = transport.download_message_resource("om-1", "file-key-1", resource_type="file")

        self.assertEqual(resource.content, b"file-bytes")
        self.assertEqual(resource.file_name, "report.pdf")
        self.assertEqual(resource.content_type, "application/pdf")
        request = client.im.v1.message_resource.get.call_args.args[0]
        self.assertEqual(request.message_id, "om-1")
        self.assertEqual(request.file_key, "file-key-1")
        self.assertEqual(request.type, "file")

    def test_download_file_returns_bytes(self) -> None:
        transport, client = self._transport_with_mock_client()
        response = _ok_response()
        response.file = io.BytesIO(b"img-bytes")
        response.file_name = ""
        response.raw = SimpleNamespace(headers={})
        client.im.v1.message_resource.get.return_value = response

        self.assertEqual(transport.download_file("om-1", "file-key-1"), b"img-bytes")

    def test_download_failure_raises(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message_resource.get.return_value = _fail_response()

        with self.assertRaises(RuntimeError):
            transport.download_message_resource("om-1", "file-key-1", resource_type="file")

    def test_download_sdk_exception_raises(self) -> None:
        transport, client = self._transport_with_mock_client()
        client.im.v1.message_resource.get.side_effect = RuntimeError("network down")

        with self.assertRaises(RuntimeError):
            transport.download_message_resource("om-1", "file-key-1", resource_type="file")


class ImageUploadTests(unittest.TestCase):
    def test_upload_image_missing_path_returns_none(self) -> None:
        transport = _make_transport()

        self.assertIsNone(transport.upload_image("/nonexistent/definitely-missing.png"))

    def test_upload_image_success_and_send_by_key(self) -> None:
        transport = _make_transport()
        transport.client = Mock()
        transport.client.im.v1.image.create.return_value = _ok_response(image_key="img-key-9")
        transport.client.im.v1.message.create.return_value = _ok_response(message_id="om-img")

        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            image_path = pathlib.Path(tmp) / "a.png"
            image_path.write_bytes(b"\x89PNG")

            image_key = transport.upload_image(str(image_path))

        self.assertEqual(image_key, "img-key-9")
        message_id = transport.send_image_by_key("oc-1", "img-key-9")
        self.assertEqual(message_id, "om-img")
        request = transport.client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.request_body.msg_type, "image")
        self.assertEqual(json.loads(request.request_body.content), {"image_key": "img-key-9"})


class StartTests(unittest.TestCase):
    def test_start_applies_proxy_policy_and_starts_ws_client(self) -> None:
        transport = _make_transport(feishu_ws_proxy="disabled")
        ws_client = Mock()

        with patch("kite.feishu_transport.configure_feishu_ws_proxy") as configure_mock, patch(
            "kite.feishu_transport.lark.ws.Client", return_value=ws_client
        ):
            transport.start()

        configure_mock.assert_called_once_with("disabled")
        ws_client.start.assert_called_once_with()

    def test_invalid_proxy_mode_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            _make_transport(feishu_ws_proxy="bogus")


class BotIdentityTests(unittest.TestCase):
    def test_fetch_bot_open_id_success(self) -> None:
        transport = _make_transport()
        response = Mock()
        response.success.return_value = True
        response.raw.content = json.dumps({"bot": {"open_id": "ou_discovered"}})
        transport.client = Mock()
        transport.client.request.return_value = response

        self.assertEqual(transport.fetch_bot_open_id(), "ou_discovered")

    def test_fetch_bot_open_id_failure_returns_none(self) -> None:
        transport = _make_transport()
        response = Mock()
        response.success.return_value = False
        response.code = 500
        response.msg = "err"
        transport.client = Mock()
        transport.client.request.return_value = response

        self.assertIsNone(transport.fetch_bot_open_id())

    def test_fetch_bot_open_id_exception_returns_none(self) -> None:
        transport = _make_transport()
        transport.client = Mock()
        transport.client.request.side_effect = RuntimeError("boom")

        self.assertIsNone(transport.fetch_bot_open_id())

    def test_set_bot_open_id_enables_mention_detection(self) -> None:
        transport = _make_transport(bot_open_id="")
        mentions = [{"key": "@_user_1", "name": "KiteBot", "id": {"open_id": "ou_x"}}]
        self.assertFalse(transport._is_bot_mentioned(mentions))

        transport.set_bot_open_id("ou_x")

        self.assertTrue(transport._is_bot_mentioned(mentions))

    def test_fetch_user_name_success(self) -> None:
        transport = _make_transport()
        response = Mock()
        response.success.return_value = True
        response.raw.content = json.dumps({"user": {"name": "张三"}})
        transport.client = Mock()
        transport.client.request.return_value = response

        self.assertEqual(transport.fetch_user_name("ou_x"), "张三")

    def test_fetch_user_name_falls_back_to_nickname(self) -> None:
        transport = _make_transport()
        response = Mock()
        response.success.return_value = True
        response.raw.content = json.dumps({"user": {"name": "", "nickname": "小三"}})
        transport.client = Mock()
        transport.client.request.return_value = response

        self.assertEqual(transport.fetch_user_name("ou_x"), "小三")

    def test_fetch_user_name_failure_returns_none(self) -> None:
        transport = _make_transport()
        response = Mock()
        response.success.return_value = False
        response.code = 500
        transport.client = Mock()
        transport.client.request.return_value = response

        self.assertIsNone(transport.fetch_user_name("ou_x"))


if __name__ == "__main__":
    unittest.main()
