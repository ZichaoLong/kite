"""Inbound attachment staging domain (docs/contracts/images.md §2).

Owns the Feishu-side lifecycle of inbound attachments:

- validate the attachment kind (first cut: images only; every other kind
  gets an explicit per-type rejection, nothing is downloaded or guessed);
- require an existing binding (unbound chats get bind guidance);
- download via the transport port, enforce the post-download byte cap
  (fail-closed), and stage into ``<session cwd>/_feishu_attachments/`` with
  a sanitized, collision-proof name (tmp+rename writes);
- persist a TTL'd pending record and ack ("已保存，发送文字即可附带");
- on the next text prompt from the same ``(sender_open_id, chat_id)``:
  consume the pending records, validate them (file exists, still under the
  current session cwd — expired/missing/stale-cwd blocks the prompt,
  fail-closed), and prepare the prompt payload (composed text + image
  bytes for the native kap ``image`` content parts);
- consumed records are restored when the submit fails, and their staged
  files are deleted once the submit succeeds (consumption deletes files).

Ported from FOCUS ``bot/file_message_domain.py``, cut to images and re-keyed
to ``(sender_open_id, chat_id)`` per the KITE contract. All methods run on
the RuntimeLoop (wired by AppHandler).
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import pathlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from kite.adapters.kap_server import KapError, KapTransportError
from kite.feishu_transport import DownloadedMessageResource, InboundAttachment
from kite.stores.pending_attachment_store import (
    ATTACHMENT_TYPE_IMAGE,
    PendingAttachmentRecord,
    PendingAttachmentStore,
)

logger = logging.getLogger("kite.attachments")

ATTACHMENT_STAGE_DIRNAME = "_feishu_attachments"

# Per-type explicit rejection texts (contract §2.1). file/audio/media are
# downloadable by the transport but not admitted in the first cut;
# folder/sticker cannot be downloaded over the Feishu API at all.
# merge_forward is not an attachment: it dispatches to on_merge_forward and
# never reaches this domain (kite/forward_aggregator.py).
_UNSUPPORTED_ATTACHMENT_TEXTS = {
    "file": "暂不支持文件附件（当前仅支持图片）。如需处理本地文件，请放入会话工作目录后用文字说明路径。",
    "audio": "暂不支持音频附件（当前仅支持图片）。",
    "media": "暂不支持音视频附件（当前仅支持图片）。",
    "folder": "文件夹消息无法通过飞书 API 下载，暂不支持。",
    "sticker": "暂不支持表情包作为附件。",
}

_EXPIRED_BLOCK_TEXT = "附件已过期，请重新发送。"
_CWD_MISMATCH_BLOCK_TEXT = "附件属于切换前的工作目录，已失效，请重新发送。"
_NO_CWD_BLOCK_TEXT = "无法确认会话当前的工作目录，附件未提交，请重新发送图片后再试。"
_UNBOUND_TEXT = "尚未绑定会话。请先发送一条文字消息创建并绑定会话，再发送图片。"
_KAP_UNREACHABLE_TEXT = "无法连接 kap-server，无法确认会话工作目录，图片未保存。请稍后再试。"


@dataclass(frozen=True, slots=True)
class AttachmentPorts:
    """The domain's outward seams (wired by AppHandler).

    - ``download``: (message_id, resource_key) -> the resource bytes;
    - ``reply``: (chat_id, text, *, parent_message_id="") -> None;
    - ``resolve_cwd``: chat_id -> the bound session's cwd ("" when the chat
      has no binding); may raise KapError/KapTransportError.
    """

    download: Callable[[str, str], DownloadedMessageResource]
    reply: Callable[..., None]
    resolve_cwd: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class StagedImagePayload:
    """A consumed image, read and ready for the native kap content part."""

    local_path: str
    media_type: str
    data_base64: str


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    """The consumption result for one text prompt.

    ``blocking_text`` non-empty means the prompt is blocked (fail-closed);
    nothing may be submitted. ``consumed`` carries the taken records so the
    caller can restore them on submit failure / delete their files on
    success.
    """

    text: str
    images: tuple[StagedImagePayload, ...] = ()
    consumed: tuple[PendingAttachmentRecord, ...] = ()
    blocking_text: str = ""

    @property
    def has_attachments(self) -> bool:
        return bool(self.consumed)


class AttachmentDomain:
    def __init__(
        self,
        *,
        ports: AttachmentPorts,
        store: PendingAttachmentStore,
        ttl_seconds: float,
        max_bytes: int,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ports = ports
        self._store = store
        self._ttl_seconds = max(float(ttl_seconds), 1.0)
        self._max_bytes = max(int(max_bytes), 1)
        self._now = now

    # ------------------------------------------------------------------
    # Staging (attachment message in)
    # ------------------------------------------------------------------

    def handle_attachment(self, attachment: InboundAttachment) -> None:
        self._sweep_expired()

        attachment_type = str(attachment.attachment_type or "").strip().lower()
        if attachment_type != ATTACHMENT_TYPE_IMAGE:
            self._reply_rejected(
                attachment,
                _UNSUPPORTED_ATTACHMENT_TEXTS.get(
                    attachment_type,
                    f"暂不支持 `{attachment_type or 'unknown'}` 类型的附件（当前仅支持图片）。",
                ),
            )
            return
        resource_key = str(attachment.resource_key or "").strip()
        if not resource_key:
            self._reply_rejected(attachment, "图片消息缺少资源 key，无法下载，请重新发送。")
            return

        try:
            cwd = self._ports.resolve_cwd(attachment.chat_id)
        except (KapError, KapTransportError) as exc:
            logger.info("attachment cwd resolution failed chat=%s: %s", attachment.chat_id, exc)
            self._reply_rejected(attachment, _KAP_UNREACHABLE_TEXT)
            return
        if not cwd:
            self._reply_rejected(attachment, _UNBOUND_TEXT)
            return
        working_dir = pathlib.Path(cwd).expanduser()
        if not working_dir.is_dir():
            self._reply_rejected(
                attachment,
                f"会话工作目录不存在或不是目录：`{working_dir}`，图片未保存。",
            )
            return

        try:
            downloaded = self._ports.download(attachment.message_id, resource_key)
        except Exception as exc:
            self._reply_rejected(attachment, f"下载图片失败：{exc}")
            return

        size = len(downloaded.content)
        if size > self._max_bytes:
            self._reply_rejected(
                attachment,
                f"图片过大（{size} 字节，超过上限 {self._max_bytes} 字节），未保存。",
            )
            return

        display_name = self._resolve_display_name(
            display_name=attachment.file_name,
            downloaded_name=downloaded.file_name,
            content_type=downloaded.content_type,
        )
        try:
            staged_path = self._stage(
                working_dir=working_dir,
                message_id=attachment.message_id,
                display_name=display_name,
                content=downloaded.content,
            )
        except OSError as exc:
            self._reply_rejected(attachment, f"保存图片到本地失败：{exc}")
            return

        now = self._now()
        record = PendingAttachmentRecord(
            sender_open_id=attachment.sender_open_id,
            chat_id=attachment.chat_id,
            message_id=attachment.message_id,
            attachment_type=ATTACHMENT_TYPE_IMAGE,
            display_name=display_name,
            media_type=self._resolve_media_type(downloaded.content_type, display_name),
            local_path=str(staged_path),
            created_at=now,
            expires_at=now + self._ttl_seconds,
        )
        self._store.add(record)
        pending_count = self._pending_count(
            sender_open_id=attachment.sender_open_id, chat_id=attachment.chat_id, now=now
        )
        suffix = f"\n当前待附带图片：{pending_count} 张。" if pending_count > 1 else ""
        ttl_minutes = max(int(self._ttl_seconds // 60), 1)
        self._ports.reply(
            attachment.chat_id,
            f"图片已保存，发送文字即可附带（{ttl_minutes} 分钟内有效）。\n"
            f"暂存位置：`{self._display_path(staged_path, working_dir)}`{suffix}",
            parent_message_id=attachment.message_id,
        )

    # ------------------------------------------------------------------
    # Consumption (text prompt in)
    # ------------------------------------------------------------------

    def prepare_prompt(
        self,
        *,
        sender_open_id: str,
        chat_id: str,
        text: str,
        cwd: str,
    ) -> PreparedPrompt:
        active, expired = self._store.take(
            sender_open_id=sender_open_id, chat_id=chat_id, now=self._now()
        )
        self._delete_staged_files(expired)
        if not active:
            if expired:
                return PreparedPrompt(text=text, blocking_text=_EXPIRED_BLOCK_TEXT)
            return PreparedPrompt(text=text)

        if not str(cwd or "").strip():
            self._delete_staged_files(active)
            return PreparedPrompt(text=text, blocking_text=_NO_CWD_BLOCK_TEXT)
        expected_stage_dir = (
            pathlib.Path(cwd).expanduser() / ATTACHMENT_STAGE_DIRNAME
        ).resolve()

        payloads: list[StagedImagePayload] = []
        missing = False
        mismatch = False
        for record in active:
            path = pathlib.Path(record.local_path)
            try:
                stage_dir = path.parent.resolve()
                data = path.read_bytes()
            except OSError:
                missing = True
                continue
            if stage_dir != expected_stage_dir:
                mismatch = True
                continue
            payloads.append(
                StagedImagePayload(
                    local_path=record.local_path,
                    media_type=record.media_type or "image/png",
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            )
        if missing or mismatch:
            # Blocked consumption: the records are consumed, so their staged
            # files can never be consumed again — delete them (hygiene).
            self._delete_staged_files(active)
            blocking_text = _CWD_MISMATCH_BLOCK_TEXT if (mismatch and not missing) else _EXPIRED_BLOCK_TEXT
            return PreparedPrompt(text=text, blocking_text=blocking_text)

        return PreparedPrompt(
            text=self._compose_text(text, active),
            images=tuple(payloads),
            consumed=active,
        )

    def restore_consumed(self, records: tuple[PendingAttachmentRecord, ...]) -> None:
        """Put consumed records back after a failed submit (retry keeps them).

        Records keep their original expiry; a restored-but-expired record is
        swept by the next take/cleanup pass.
        """
        if records:
            self._store.add_many(records)

    @staticmethod
    def discard_consumed_files(records: tuple[PendingAttachmentRecord, ...]) -> None:
        """Delete staged files after a successful submit (consumption deletes)."""
        AttachmentDomain._delete_staged_files(records)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sweep_expired(self) -> None:
        self._delete_staged_files(self._store.cleanup_expired(now=self._now()))

    @staticmethod
    def _compose_text(text: str, records: tuple[PendingAttachmentRecord, ...]) -> str:
        lines = [
            f"用户通过飞书发送了 {len(records)} 张图片（已作为图片内容随本条消息提交）："
        ]
        for record in records:
            lines.append(f"- {record.display_name or '图片'}（本地暂存：{record.local_path}）")
        lines.append("")
        lines.append("用户请求：")
        lines.append(text)
        return "\n".join(lines)

    def _pending_count(self, *, sender_open_id: str, chat_id: str, now: float) -> int:
        return sum(
            1
            for record in self._store.list_all()
            if record.sender_open_id == sender_open_id
            and record.chat_id == chat_id
            and record.expires_at > now
        )

    def _reply_rejected(self, attachment: InboundAttachment, reason: str) -> None:
        self._ports.reply(
            attachment.chat_id,
            reason,
            parent_message_id=attachment.message_id,
        )

    def _stage(
        self,
        *,
        working_dir: pathlib.Path,
        message_id: str,
        display_name: str,
        content: bytes,
    ) -> pathlib.Path:
        stage_dir = working_dir / ATTACHMENT_STAGE_DIRNAME
        stage_dir.mkdir(parents=True, exist_ok=True)
        file_name = self._build_staged_file_name(
            message_id=message_id, display_name=display_name
        )
        path = stage_dir / file_name
        base = pathlib.Path(file_name)
        suffix_text = "".join(base.suffixes)
        base_stem = base.name[: -len(suffix_text)] if suffix_text else base.name
        collision = 1
        while path.exists():
            path = stage_dir / f"{base_stem}-{collision}{suffix_text}"
            collision += 1
        # tmp+rename: a failed write never leaves a partial staged file.
        tmp_path = stage_dir / f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
        try:
            tmp_path.write_bytes(content)
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return path.resolve()

    def _build_staged_file_name(self, *, message_id: str, display_name: str) -> str:
        base_name = pathlib.Path(display_name).name
        safe_stem = self._sanitize_file_stem(pathlib.Path(base_name).stem) or "image"
        safe_stem = safe_stem[:80]
        safe_suffixes = "".join(
            self._sanitize_suffix(part) for part in pathlib.Path(base_name).suffixes
        )
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        message_suffix = re.sub(r"[^A-Za-z0-9]+", "", str(message_id or ""))[-10:] or "msg"
        return f"{timestamp}-{message_suffix}-{safe_stem}{safe_suffixes}"

    @staticmethod
    def _resolve_display_name(
        *, display_name: str, downloaded_name: str, content_type: str
    ) -> str:
        for candidate in (display_name, downloaded_name):
            normalized = pathlib.Path(str(candidate or "").strip()).name
            if normalized:
                return normalized
        extension = ""
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized_content_type:
            extension = mimetypes.guess_extension(normalized_content_type) or ""
        if extension == ".jpe":
            extension = ".jpg"
        return f"image{extension}"

    @staticmethod
    def _resolve_media_type(content_type: str, display_name: str) -> str:
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized.startswith("image/"):
            return normalized
        guessed, _ = mimetypes.guess_type(display_name)
        if guessed and guessed.startswith("image/"):
            return guessed
        # kap-server sniffs the bytes and corrects a wrong label upstream
        # (packages/kap-server/src/routes/prompts.ts), so a generic default
        # is safe here.
        return "image/png"

    @staticmethod
    def _sanitize_file_stem(stem: str) -> str:
        normalized = str(stem or "").strip().replace("\x00", "")
        normalized = re.sub(r"[\\/]+", "_", normalized)
        normalized = "".join(ch if ch.isprintable() else "_" for ch in normalized)
        normalized = re.sub(r"\s+", "_", normalized)
        normalized = normalized.strip("._")
        return normalized

    @staticmethod
    def _sanitize_suffix(suffix: str) -> str:
        normalized = str(suffix or "").strip().replace("\x00", "")
        normalized = re.sub(r"[^A-Za-z0-9.]+", "", normalized)
        if normalized and not normalized.startswith("."):
            normalized = "." + normalized
        return normalized

    @staticmethod
    def _display_path(staged_path: pathlib.Path, working_dir: pathlib.Path) -> str:
        try:
            return str(staged_path.relative_to(working_dir))
        except ValueError:
            return str(staged_path)

    @staticmethod
    def _delete_staged_files(records: tuple[PendingAttachmentRecord, ...]) -> None:
        for record in records:
            try:
                os.remove(record.local_path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("failed to delete staged attachment %s: %s", record.local_path, exc)
