# Contract: Images Inbound and Outbound (Phase 2)

> Status: admitted (2026-07-23, passed the carrying-capacity gate below);
> turns active with its implementation.
> Evidence: `docs/research/focus-assets-map.md` (FOCUS attachment pipeline),
> kap prompt content parts verified upstream
> (`packages/protocol/src/message.ts:70-78`: `image` / `video` / `file`).

## 1. Carrying-Capacity Gate

1. **Which layer?** Feishu transport (download/upload — already ported),
   application (staging domain + prompt composition), local state (new
   pending-attachment store), adapter (native content-part shape only).
2. **Which state axis?** One NEW axis, registered in kite-design §4 before
   code: **attachment staging** — persisted, TTL'd, keyed by
   `(sender_open_id, chat_id)`. It is transient-but-persisted state, like a
   scoped binding store entry; it does not interact with work state or
   prompt ownership.
3. **Crash/restart recovery?** Records survive on disk; files are validated
   at consume time (exists, same session cwd); expired or missing → the
   prompt is explicitly blocked ("附件已过期，请重新发送"), never silently
   dropped. Orphaned files are bounded leftovers swept by TTL.
4. **Which tests?** §4 below.

## 2. Inbound Contract

1. **First cut: images only.** Other attachment kinds (file, audio, media,
   folder, sticker, merge_forward) get a per-type explicit rejection text;
   nothing is downloaded or guessed.
2. **Pipeline**: `on_attachment` → require an existing binding (unbound →
   guidance to bind first) → `download_message_resource` → stage into
   `<session cwd>/_feishu_attachments/` with a sanitized, collision-proof
   filename (strip NUL/slashes/non-printables, suffix whitelist, timestamp +
   message-id prefix, `-1/-2` collision suffix) → persist a TTL'd record
   (default 10 min) → reply "已保存，发送文字即可附带".
3. **Consumption**: the next text prompt from the same `(sender, chat)`
   takes matching records; validate (file exists, still in the current
   session cwd — a stale cwd blocks); compose the prompt as native kap
   `image` content parts plus the user text plus the staged paths as text
   context; records are consumed once and **restored if the submit fails**.
4. **Limits**: explicit post-download byte cap (default 20 MB) — over cap →
   reject with the size in the message, fail-closed.
5. **Hygiene**: TTL lazy sweep on each new attachment/message; expired
   records delete their files; consumption deletes consumed files.
6. In groups (once the group contract lands): pending attachments are
   per-`(sender, chat)` — members' attachments never mix.

## 3. Outbound Contract

1. **Primitive**: local path → `upload_image` once → image message to every
   attached chat of the bound session; one chat's failure is isolated in the
   result, not raised (FOCUS's `thread_image_delivery` discipline).
2. **Command surface**: `kitectl image send --chat <id> --path <file>`
   (control-plane entry; routes through the daemon per
   `docs/decisions/control-plane.md`).
3. Agent-initiated image sending (a FOCUS-style skill) is a later candidate,
   not in this contract.

## 4. Tests That Lock the Behavior

- Filename sanitization + collision suffixing.
- Unsupported types → per-type rejection text; nothing staged.
- TTL expiry: record + file removed; prompt blocked with the expiry text.
- cwd-mismatch: staged under a previous cwd → blocked.
- Consume-once: records restored when the submit raises (kap error), so a
  retry still has them.
- Over-cap download → rejection, nothing staged.
- Prompt composition: image parts + text + paths in the wire body.
- Outbound fan-out: two attached chats, one failing → other still receives;
  failure reported.
- Restart: records survive; a missing file at consume time blocks
  fail-closed.

## 5. Fail-Closed List

1. kap unreachable at consume → records restored, user told to retry.
2. Download failure → explicit error, no record, no partial file (staging
   writes are tmp+rename).
3. Unsupported/oversized → rejection text; nothing is guessed or truncated.
