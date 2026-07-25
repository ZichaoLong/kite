# 合同：图片入站与出站（Phase 2)

> 状态：已准入（2026-07-23，通过下方承载力门槛）；随实现转 active。
> 证据：`docs/research/focus-assets-map.md`(FOCUS 附件管线）;kap
> prompt content part 已经上游验证（`packages/protocol/src/message.ts:70-78`:
> `image` / `video` / `file`)。

## 1. 功能承载力门槛

1. **归哪一层？** 飞书传输层（下载/上传——已移植）、应用层（暂存域 +
   prompt 组合）、本地状态层（新待消费附件 store)、适配层（仅原生
   content part 形态）。
2. **动哪条状态轴？** 新增一条，开工前已在 kite-design §4 登记：
   **附件暂存**——持久化、带 TTL、以 `(sender_open_id, chat_id)` 为键。
   它是"短暂但落盘"的状态，类似 scoped binding 条目；不与 work state、
   prompt 归属交互。
3. **崩溃/重启怎么恢复？** 记录落盘保留；消费时校验文件（存在、且仍在
   当前 session cwd)；过期或缺失 → 显式阻断该 prompt("附件已过期，请
   重新发送")，绝不静默丢弃。孤儿文件有界，由 TTL 清扫。
4. **用什么测试锁？** 见 §4。

## 2. 入站合同

1. **第一刀只做图片**。其他附件类型（file、audio、media、folder、
   sticker、merge_forward）给出按类型的显式拒绝文案；不下载、不猜测。
2. **管线**:`on_attachment` → 要求已有 binding（未绑定 → 引导先绑定）→
   `download_message_resource` → 暂存到 `<session
   cwd>/_feishu_attachments/`，文件名消毒且防撞（剥离 NUL/斜杠/不可打印
   字符、后缀白名单、时间戳 + message-id 前缀、`-1/-2` 碰撞后缀）→ 写入
   带 TTL 的记录（默认 10 分钟）→ 回复"已保存，发送文字即可附带"。
3. **消费**：同一 `(sender, chat)` 的下一条文字 prompt 取走匹配记录；
   校验（文件存在、仍在当前 session cwd——cwd 已变则阻断）;prompt 组合
   为 kap 原生 `image` content part + 用户文字 + 以文本列出的暂存路径；
   记录消费一次，**提交失败时回滚**，重试仍可消费。
4. **上限**：下载后显式字节上限（默认 20 MB)——超限拒绝并在消息中
   注明大小，fail-closed。
5. **卫生**：每次新附件/新消息做 TTL 惰性清扫；过期记录删文件；消费
   成功删已消费文件。
6. 群聊落地后：待消费附件按 `(sender, chat)` 隔离——群成员的附件互不
   混淆。**本刀群内附件暂存仅管理员**（2026-07-25 对齐，审查 L19/D5):
   已激活群内仅管理员发送的附件会被暂存；非管理员成员发送的附件一律
   静默忽略（fail-closed，不逐条回复——与群入站矩阵同一立场）。

## 3. 出站合同

1. **原语**：本地路径 → `upload_image` 一次 → 以图片消息发到该绑定
   session 的每个 attached chat；单个 chat 失败在结果中隔离，不上抛
   (FOCUS `thread_image_delivery` 纪律）。
2. **命令面**:`kitectl image send --chat <id> --path <file>`（控制面
   入口，按 `docs/decisions/control-plane.md` 经 daemon 路由）。
3. agent 主动发图（FOCUS 式 skill）为后期候选，不在本合同。

## 4. 锁定行为的测试

- 文件名消毒 + 碰撞后缀。
- 不支持类型 → 按类型拒绝文案；无任何暂存。
- TTL 过期：记录与文件清除；prompt 被阻断并提示过期文案。
- cwd 不匹配：在上一个 cwd 暂存的 → 阻断。
- 消费一次：提交抛错（kap 错误）时记录回滚，重试仍可消费。
- 超限下载 → 拒绝，无暂存。
- prompt 组合：报文体含 image part + 文字 + 路径。
- 出站扇出：两个 attached chat，一个失败 → 另一个仍收到；失败被报告。
- 重启：记录保留；消费时文件缺失 → fail-closed 阻断。

## 5. fail-closed 清单

1. 消费时 kap 不可达 → 记录回滚，提示重试。
2. 下载失败 → 显式错误，无记录、无残件（暂存写 tmp+rename)。
3. 不支持/超限 → 拒绝文案；不猜测、不截断。
