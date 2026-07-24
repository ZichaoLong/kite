"""Attachment domain contract tests (docs/contracts/images.md §2, §4).

A real PendingAttachmentStore over a temp data dir and a real temp session
cwd; the download/reply/resolve_cwd ports are fakes and the domain clock is
injected, so staging, TTL expiry, cwd-mismatch, consume-once/restore and
prompt composition are exercised deterministically.
"""

from __future__ import annotations

import base64
import os
import pathlib
import re
import tempfile
import unittest

from kite.adapters.kap_server import KapTransportError
from kite.attachment_domain import (
    ATTACHMENT_STAGE_DIRNAME,
    AttachmentDomain,
    AttachmentPorts,
)
from kite.feishu_transport import DownloadedMessageResource, InboundAttachment
from kite.stores.pending_attachment_store import PendingAttachmentStore

SENDER = "ou_a"
CHAT_ID = "oc_a"
IMAGE_BYTES = b"\x89PNG-fake-image-bytes"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AttachmentDomainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name) / "data"
        self.work_dir = pathlib.Path(self._tmp.name) / "work"
        self.work_dir.mkdir()
        self.store = PendingAttachmentStore(self.data_dir)
        self.clock = FakeClock()
        self.replies: list[dict] = []
        self.downloads: list[tuple[str, str]] = []
        self.download_result = DownloadedMessageResource(
            content=IMAGE_BYTES, file_name="photo.png", content_type="image/png"
        )
        self.download_error: Exception | None = None
        self.cwds: dict[str, str] = {CHAT_ID: str(self.work_dir)}
        self.cwd_error: Exception | None = None
        self.domain = self._make_domain()

    def _make_domain(self, *, ttl_seconds: float = 600.0, max_bytes: int = 20 * 1024 * 1024) -> AttachmentDomain:
        return AttachmentDomain(
            ports=AttachmentPorts(
                download=self._download,
                reply=self._reply,
                resolve_cwd=self._resolve_cwd,
            ),
            store=self.store,
            ttl_seconds=ttl_seconds,
            max_bytes=max_bytes,
            now=self.clock,
        )

    # -- fake ports ----------------------------------------------------------

    def _download(self, message_id: str, resource_key: str) -> DownloadedMessageResource:
        self.downloads.append((message_id, resource_key))
        if self.download_error is not None:
            raise self.download_error
        return self.download_result

    def _reply(self, chat_id: str, text: str, *, parent_message_id: str = "") -> None:
        self.replies.append(
            {"chat_id": chat_id, "text": text, "parent_message_id": parent_message_id}
        )

    def _resolve_cwd(self, chat_id: str) -> str:
        if self.cwd_error is not None:
            raise self.cwd_error
        return self.cwds.get(chat_id, "")

    # -- helpers ---------------------------------------------------------------

    def _attachment(
        self,
        *,
        message_id: str = "om_1",
        attachment_type: str = "image",
        resource_key: str = "img_key",
        sender: str = SENDER,
        chat_id: str = CHAT_ID,
        file_name: str = "",
    ) -> InboundAttachment:
        return InboundAttachment(
            message_id=message_id,
            chat_id=chat_id,
            chat_type="p2p",
            attachment_type=attachment_type,
            resource_key=resource_key,
            file_name=file_name,
            sender_open_id=sender,
            sender_user_id="u_1",
            sender_type="user",
            thread_id="",
            root_id="",
            parent_id="",
            create_time=0,
        )

    def _stage_dir(self) -> pathlib.Path:
        return self.work_dir / ATTACHMENT_STAGE_DIRNAME

    def _staged_files(self) -> list[pathlib.Path]:
        stage_dir = self._stage_dir()
        if not stage_dir.is_dir():
            return []
        return sorted(path for path in stage_dir.iterdir() if not path.name.startswith("."))

    def _last_reply(self) -> str:
        assert self.replies, "expected at least one reply"
        return self.replies[-1]["text"]


class StagingTests(AttachmentDomainTestCase):
    def test_image_stages_persists_and_acks(self) -> None:
        self.domain.handle_attachment(self._attachment())

        self.assertEqual(self.downloads, [("om_1", "img_key")])
        staged = self._staged_files()
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_bytes(), IMAGE_BYTES)
        self.assertRegex(staged[0].name, r"^\d{8}-\d{6}-om1-photo\.png$")
        records = self.store.list_all()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.sender_open_id, SENDER)
        self.assertEqual(record.chat_id, CHAT_ID)
        self.assertEqual(record.local_path, str(staged[0].resolve()))
        self.assertEqual(record.media_type, "image/png")
        self.assertEqual(record.expires_at - record.created_at, 600.0)
        reply = self._last_reply()
        self.assertIn("已保存，发送文字即可附带", reply)
        self.assertIn(f"{ATTACHMENT_STAGE_DIRNAME}/", reply)
        self.assertEqual(self.replies[-1]["parent_message_id"], "om_1")

    def test_second_pending_image_reports_count(self) -> None:
        self.domain.handle_attachment(self._attachment(message_id="om_1"))
        self.domain.handle_attachment(self._attachment(message_id="om_2"))

        self.assertIn("当前待附带图片：2 张", self._last_reply())

    def test_filename_is_sanitized(self) -> None:
        self.download_result = DownloadedMessageResource(
            content=IMAGE_BYTES,
            file_name="..\\../we\x00ird na\tme.PnG",
            content_type="image/png",
        )

        self.domain.handle_attachment(self._attachment(message_id="om_123"))

        staged = self._staged_files()
        self.assertEqual(len(staged), 1)
        self.assertNotRegex(staged[0].name, r"[\s/\\\\\x00-\x1f]")
        self.assertRegex(staged[0].name, r"^\d{8}-\d{6}-om123-weird_na_me\.PnG$")

    def test_name_fallback_from_content_type(self) -> None:
        self.download_result = DownloadedMessageResource(
            content=IMAGE_BYTES, file_name="", content_type="image/jpeg; charset=binary"
        )

        self.domain.handle_attachment(self._attachment())

        self.assertTrue(self._staged_files()[0].name.endswith("-image.jpg"))

    def test_collision_gets_dash_suffix(self) -> None:
        # Same message id + name + frozen clock => the base name collides.
        self.domain.handle_attachment(self._attachment(message_id="om_1"))
        self.domain.handle_attachment(self._attachment(message_id="om_1"))

        names = [path.name for path in self._staged_files()]
        self.assertEqual(len(names), 2)
        # Lexical sort puts "-photo-1.png" before "-photo.png" ('-' < '.').
        self.assertRegex(names[0], r"-photo-1\.png$")
        self.assertRegex(names[1], r"-photo\.png$")
        self.assertEqual(len(self.store.list_all()), 2)

    def test_unsupported_types_get_per_type_rejections(self) -> None:
        expectations = {
            "file": "暂不支持文件附件",
            "audio": "暂不支持音频附件",
            "media": "暂不支持音视频附件",
            "folder": "文件夹消息无法通过飞书 API 下载",
            "sticker": "暂不支持表情包",
            "post": "暂不支持 `post` 类型的附件",
        }
        for attachment_type, expected in expectations.items():
            with self.subTest(attachment_type=attachment_type):
                self.replies.clear()
                self.domain.handle_attachment(self._attachment(attachment_type=attachment_type))
                self.assertIn(expected, self._last_reply())

        # Nothing downloaded, staged, or persisted for any of them.
        self.assertEqual(self.downloads, [])
        self.assertEqual(self._staged_files(), [])
        self.assertEqual(self.store.list_all(), ())

    def test_missing_resource_key_is_rejected(self) -> None:
        self.domain.handle_attachment(self._attachment(resource_key=""))

        self.assertIn("缺少资源 key", self._last_reply())
        self.assertEqual(self.downloads, [])

    def test_unbound_chat_gets_bind_guidance(self) -> None:
        self.cwds.clear()

        self.domain.handle_attachment(self._attachment())

        self.assertIn("尚未绑定会话", self._last_reply())
        self.assertEqual(self.downloads, [])

    def test_kap_unreachable_at_cwd_resolution_is_rejected(self) -> None:
        self.cwd_error = KapTransportError("connection refused")

        self.domain.handle_attachment(self._attachment())

        self.assertIn("无法确认会话工作目录", self._last_reply())
        self.assertEqual(self.downloads, [])

    def test_missing_working_dir_is_rejected(self) -> None:
        self.cwds[CHAT_ID] = str(self.work_dir / "gone")

        self.domain.handle_attachment(self._attachment())

        self.assertIn("会话工作目录不存在或不是目录", self._last_reply())
        self.assertEqual(self.downloads, [])

    def test_download_failure_leaves_nothing_behind(self) -> None:
        self.download_error = RuntimeError("boom")

        self.domain.handle_attachment(self._attachment())

        self.assertIn("下载图片失败：boom", self._last_reply())
        self.assertEqual(self._staged_files(), [])
        self.assertEqual(self.store.list_all(), ())

    def test_over_cap_download_is_rejected_with_sizes(self) -> None:
        self.download_result = DownloadedMessageResource(
            content=b"x" * 11, file_name="big.png", content_type="image/png"
        )
        self.domain = self._make_domain(max_bytes=10)

        self.domain.handle_attachment(self._attachment())

        reply = self._last_reply()
        self.assertIn("图片过大", reply)
        self.assertIn("11 字节", reply)
        self.assertIn("10 字节", reply)
        self.assertEqual(self._staged_files(), [])
        self.assertEqual(self.store.list_all(), ())


class ConsumptionTests(AttachmentDomainTestCase):
    def _stage(self, *, message_id: str = "om_1") -> None:
        self.domain.handle_attachment(self._attachment(message_id=message_id))

    def test_prepare_without_pending_is_plain_text(self) -> None:
        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hello", cwd=str(self.work_dir)
        )

        self.assertEqual(prepared.text, "hello")
        self.assertEqual(prepared.images, ())
        self.assertFalse(prepared.has_attachments)
        self.assertEqual(prepared.blocking_text, "")

    def test_prepare_composes_text_and_image_payloads(self) -> None:
        self._stage(message_id="om_1")
        self._stage(message_id="om_2")

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER,
            chat_id=CHAT_ID,
            text="看看这两张",
            cwd=str(self.work_dir),
        )

        self.assertEqual(prepared.blocking_text, "")
        self.assertTrue(prepared.has_attachments)
        self.assertEqual(len(prepared.consumed), 2)
        self.assertEqual(len(prepared.images), 2)
        # Text context: both staged paths + the user request.
        for record in prepared.consumed:
            self.assertIn(record.local_path, prepared.text)
            self.assertIn(record.display_name, prepared.text)
        self.assertIn("用户请求：\n看看这两张", prepared.text)
        # Image payloads: base64 of the staged bytes, in record order.
        for record, image in zip(prepared.consumed, prepared.images):
            self.assertEqual(image.local_path, record.local_path)
            self.assertEqual(image.media_type, "image/png")
            self.assertEqual(base64.b64decode(image.data_base64), IMAGE_BYTES)
        # Consume-once: the store is drained.
        self.assertEqual(self.store.list_all(), ())

    def test_consume_once_with_restore(self) -> None:
        self._stage()

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )
        self.assertTrue(prepared.has_attachments)
        self.assertEqual(self.store.list_all(), ())

        # Submit failed -> restore puts the record back; a retry consumes it.
        self.domain.restore_consumed(prepared.consumed)
        self.assertEqual(len(self.store.list_all()), 1)
        retry = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )
        self.assertTrue(retry.has_attachments)
        self.assertEqual(retry.blocking_text, "")

    def test_expired_record_blocks_and_sweeps(self) -> None:
        self.domain = self._make_domain(ttl_seconds=10.0)
        self._stage()
        staged = self._staged_files()
        self.assertEqual(len(staged), 1)
        self.clock.advance(11.0)

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )

        self.assertEqual(prepared.blocking_text, "附件已过期，请重新发送。")
        self.assertFalse(prepared.has_attachments)
        # Record + staged file are gone.
        self.assertEqual(self.store.list_all(), ())
        self.assertEqual(self._staged_files(), [])

    def test_new_attachment_sweeps_expired_records(self) -> None:
        self.domain = self._make_domain(ttl_seconds=10.0)
        self._stage(message_id="om_old")
        stale_path = self._staged_files()[0]
        self.clock.advance(11.0)

        self._stage(message_id="om_new")

        self.assertFalse(stale_path.exists())
        records = self.store.list_all()
        self.assertEqual([r.message_id for r in records], ["om_new"])

    def test_cwd_mismatch_blocks_and_deletes(self) -> None:
        self._stage()
        staged = self._staged_files()[0]
        other_cwd = pathlib.Path(self._tmp.name) / "other"
        other_cwd.mkdir()

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(other_cwd)
        )

        self.assertEqual(prepared.blocking_text, "附件属于切换前的工作目录，已失效，请重新发送。")
        self.assertFalse(staged.exists())
        self.assertEqual(self.store.list_all(), ())

    def test_missing_file_at_consume_blocks_fail_closed(self) -> None:
        self._stage()
        os.remove(self._staged_files()[0])

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )

        self.assertEqual(prepared.blocking_text, "附件已过期，请重新发送。")
        self.assertEqual(self.store.list_all(), ())

    def test_records_survive_restart_and_still_consume(self) -> None:
        # Restart survival (contract §4): a fresh store + domain over the
        # same data dir sees the persisted record and consumes it.
        self._stage()
        self.store = PendingAttachmentStore(self.data_dir)
        self.domain = self._make_domain()

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )

        self.assertTrue(prepared.has_attachments)
        self.assertEqual(len(prepared.images), 1)

    def test_missing_file_after_restart_blocks(self) -> None:
        self._stage()
        os.remove(self._staged_files()[0])
        self.store = PendingAttachmentStore(self.data_dir)
        self.domain = self._make_domain()

        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )

        self.assertEqual(prepared.blocking_text, "附件已过期，请重新发送。")

    def test_per_sender_chat_scoping(self) -> None:
        self._stage(message_id="om_1")

        # A different sender in the same chat must not consume (contract §2.6).
        prepared = self.domain.prepare_prompt(
            sender_open_id="ou_b", chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )

        self.assertFalse(prepared.has_attachments)
        self.assertEqual(len(self.store.list_all()), 1)

    def test_discard_consumed_files_deletes(self) -> None:
        self._stage()
        prepared = self.domain.prepare_prompt(
            sender_open_id=SENDER, chat_id=CHAT_ID, text="hi", cwd=str(self.work_dir)
        )
        self.assertEqual(len(self._staged_files()), 1)

        self.domain.discard_consumed_files(prepared.consumed)

        self.assertEqual(self._staged_files(), [])


if __name__ == "__main__":
    unittest.main()
