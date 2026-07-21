# MVP 功能合同（草案）

> 状态：**active**(2026-07-21 首轮对齐 + spike 验证完成，见
> `docs/verification/spike-results.md`)。本文定义 KITE MVP 做什么、不做什么、每个行为的
> 失败模式。代码行为与本文不一致即 contract gap。

## 1. 功能承载力门槛

任何功能（含 MVP 之后的）进入开发前，必须先在合同文档中回答：

1. **归哪一层？**（飞书传输 / 应用 / 适配 / 本地状态）
2. **动哪条状态轴？**（binding / attached / work state / prompt 归属；
   新轴必须先改 `kite-design.md`)
3. **崩溃/重启后怎么恢复？**（durable 事件 + snapshot 能否重建？）
4. **用什么测试锁住行为？**

答不出来就砍需求，不加例外。

## 2. MVP 范围

### 包含

| 功能 | 行为合同 |
| --- | --- |
| 单聊文本对话 | 普通文本 → `POST /sessions/{id}/prompts`；执行卡由 durable 事件驱动更新；prompt 完成发终态卡 |
| 首次自动建 session | 未绑定会话的首条消息：以其 cwd（实例 `default_working_dir`）建 session 并绑定 |
| 审批卡片 | approval.requested → 三键卡（批准/拒绝/拒绝并反馈）→ REST 响应 → 卡片 patch 定格 |
| question 表单卡 | question.requested → 选项按钮卡（按钮作答，编号回复兜底）；超时自动 dismiss |
| `/new` | 解绑当前 session，建新 session 并绑定；旧 session 原样保留（不 archive；上游无 delete) |
| `/sessions` | 列出 kap-server 上可见 session（标题/cwd/busy)，按钮切换绑定 |
| `/switch <id>` | 切换 binding 到既有 session（自动 attached) |
| `/detach` / `/attach` | 暂停/恢复当前 binding 的飞书推送，binding 本身保留 |
| `/mode <auto\|manual\|yolo>` | 读写 binding 级 permission mode（kap `permission_mode`)；每个 prompt 显式携带 |
| `/plan [on\|off]` | 查看/切换 binding 级 plan mode（kap `plan_mode`，与 permission mode 正交）；每个 prompt 显式携带 |
| `/status` | 展示 binding、session、work state、排队情况 |
| `/abort` | 中断 active prompt；仅该 prompt 发起者与管理员可用；对已完成 prompt 再 abort 得上游 40402(not pending)→ 提示"已结束"，执行卡不转失败（spike S2) |
| `/help` | 命令导航 |
| `kitectl` | config / service（启停、status、log)/ binding(list)/ session(list、status)/ prompt send |

### 不包含（Non-goals,MVP 期内明确拒绝）

- 群聊（整个 Phase 2)
- 图片/附件入站与出站（Phase 2/3)
- volatile 流式卡片（Phase 2)
- 本地 TUI wrapper(`kite`/`kcode` 命令）
- 多实例、多飞书应用
- session 删除、fork、compact、undo（上游能力存在，但 MVP 不暴露；
  暴露即需各自的合同与测试）
- 记忆、语音、设备操控、MCP/Skills 管理（永久 Non-goal，见
  `docs/research/okbot-vs-focus.md`)

## 3. 并发行为（与 concurrency-model.md 互为引用）

- 同一会话连发多条消息：全部入 kap 的 prompt FIFO，执行卡展示 active
  prompt，队列长度在卡片上可见；**不做"新消息打断"**(MVP 不暴露 steer
  用户面）。`/abort` 进 MVP：仅 active prompt 的发起者与管理员可用。
- 多个 chat 绑定同一 session（管理员显式操作才可达）： prompt 都入队；
  普通输出广播给所有 attached chat；审批/表单卡只发给**发起该 prompt 的
  chat**，其他 chat 看到"等待 #N 号 prompt 的发起者处理审批"的只读提示。
- 审批超时（默认 5 分钟，可配）：按 rejected 响应上游并显式告知发起者；
  不自动批准（**永不 fail-open**)。

## 4. fail-closed 清单

以下情形一律显式报错、显式收口，禁止"尽力而为"式静默降级：

1. kap-server 不可达 / token 失效 → 回复明确错误，不入队、不建卡。
2. WS 事件流断裂 → stale 检测重连 + snapshot 重建；重建失败时执行卡定格为
   "状态未知"，附 `kitectl session status` 排查提示，**不猜状态**。
3. `resync_required`（超窗/epoch 变更）→ snapshot 重建，同第 2 条。
4. 审批/表单响应 REST 返回幂等冲突（40902) → 提示"已被处理"，卡片定格。
5. prompt REST 返回业务错误码 → 执行卡直接转终态（失败），展示上游 msg。
6. kited 重启 → binding/permission mode/plan mode/cursor 从 store 恢复；
   内存中的
   prompt 归属尽量从 `GET .../prompts` + snapshot 重建；建不回的审批卡
   显式过期（卡片 patch 为"已失效，请重新发起或本地处理")。
7. session 在上游被 archive → 下一条消息报错并提示 `/sessions` 切换，
   不自动新建（**不替用户做隐式决定**)。

## 5. 权限与身份

- 首个管理员通过在飞书内发送 `/init <token>` 登记（init token 安装时
  生成，流程仿 FOCUS)；管理员集合存实例配置。
- MVP 只有两级：**管理员**（全部命令 + `kitectl`）与**非管理员**（不可
  使用，`/help` 除外）。允许名单（多用户）是 Phase 2 候选。
- binding 级 permission mode 默认 `auto`;`yolo` 需要管理员显式设置，
  且每次设置都在会话内明示"本会话已开启自动批准"。

## 6. 度量与可观测

- 结构化日志：每条 prompt 生命周期（submitted/started/ended)、每次审批
  决议、每次 resync/snapshot 重建，均有单行日志含
  `{chat_id, session_id, prompt_id}`。
- `kitectl session status` 输出：binding 映射、work state、队列深度、
  WS 连接龄期与最近 resync 时间。

## 已对齐（2026-07-21)

1. `/abort` 进 MVP，仅 active prompt 发起者与管理员可用。
2. question 表单进 MVP，实现为选项按钮卡（编号回复兜底）。spike S6 的
   摸底保留，但用途从"取舍依据"变为"设计输入"（有哪些问题类型、选项
   形态，决定卡片布局）。
3. `/sessions` MVP 一页 + 按最近活跃排序；session 数增长后再议分页。
4. 管理员登记采用 FOCUS 式 init token 流程（安装时生成 token，飞书内
   `/init <token>` 登记首个管理员）。
5. `/mode` 枚举按上游修正为 `auto/manual/yolo`（证据：
   `packages/protocol/src/rest/prompt.ts:41`）；`plan` 不是
   `permission_mode` 取值，而是独立的 `plan_mode` 布尔字段，以
   `/plan [on|off]` 暴露（2026-07-21 对照上游代码修正）。
6. Spike 第 0 里程碑在 kimi 0.28.1 上通过（2026-07-21;
   `docs/verification/spike-results.md`);`/abort` 行已补充实测的 40402
   重复 abort 行为。
