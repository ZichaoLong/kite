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
| `/effort <off\|low\|medium\|high\|xhigh\|max>` | 读写 binding 级 thinking effort(kap `thinking`)；与 permission mode 同样持久化，每个 prompt 显式携带 |
| `/goal [文本\|pause\|resume\|cancel\|off]` | session 的上游目标：文本经 `POST .../profile {agent_config.goal_objective}` 创建（已有活跃目标时透出 40913);`pause`/`resume`/`cancel` 经 `{agent_config.goal_control}`;`off` 即 cancel；无参经 `GET .../goal` 读回。本地不持久化（2026-07-27 修正，复审 N2-HIGH-1:prompt 提交路由静默丢弃 goal_* 字段） |
| `/compact` | 压缩绑定 session 的上下文（kap `:compact` 透传）；确认完成或回显上游错误文本 |
| `/rename 〈title〉` | 重命名绑定 session 的标题（kap `:profile` 透传） |
| `/archive` / `/restore` | 归档/恢复绑定 session(kap `:archive` / `:restore` 透传）；已归档绑定按 §4.7 行为（下一条消息报错并提示 `/sessions`，不隐式重建） |
| `/status` | 展示 binding、session、work state、排队情况 |
| `/last` | 重发当前会话最近一次终态答复文本（本地终态 store，超过 15000 字符截断） |
| `/abort` | 中断 active prompt；仅该 prompt 发起者与管理员可用；对已完成 prompt 再 abort 得上游 40402(not pending)→ 提示"已结束"，执行卡不转失败（spike S2) |
| `/btw 〈文本〉` | 旁路：将文本发给 session 的 `:btw` agent（按需启动，每 session 内存缓存），不排队、不打断主 turn；绑定模式照常携带，审批路由不变 |
| `/help` | 命令导航 |
| `/whoami` | 展示发送者身份（open_id、显示名、是否管理员）与 chat/绑定状态；非管理员可用 |
| `kitectl` | config / service（启停、status、log)/ binding(list)/ session(list、status)/ prompt send |

### 不包含（Non-goals,MVP 期内明确拒绝）

- 群聊（整个 Phase 2)
- 图片/附件入站与出站（Phase 2/3)
- volatile 流式卡片（Phase 2)
- 本地 TUI wrapper(`kite`/`kcode` 命令）
- 多实例、多飞书应用
- session 删除、fork、undo（上游能力存在，但 MVP 不暴露；
  暴露即需各自的合同与测试）
- 记忆、语音、设备操控、MCP/Skills 管理（永久 Non-goal)

## 3. 并发行为（与 concurrency-model.md 互为引用）

- 同一会话连发多条消息：全部入 kap 的 prompt FIFO，执行卡展示 active
  prompt，队列长度在卡片上可见。`/abort` 进 MVP：仅 active prompt 的发起
  者与管理员可用。`/btw 〈文本〉` 是获准的旁路：不排队、不打断主
  turn——文本发给 session 的旁路（`:btw`)agent（按需启动），其答复经
  独立 prompt 返回（已对齐 13)。
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
5. prompt 失败有两个显式出口（2026-07-25 按审查改写——旧措辞是 FOCUS
   "提交即建卡"模型的遗留）：**提交期**业务错误码（submit REST 拒绝）→
   会话内回复明确的错误文本（此时还没有执行卡——卡片由事件驱动创建）;
   **执行期** `error` durable 事件帧 → 执行卡直接转终态（失败），展示
   上游 msg。
6. kited 重启 → binding/permission mode/plan mode/cursor 从 store 恢复；
   内存中的
   prompt 归属尽量从 `GET .../prompts` + snapshot 重建；建不回的审批卡
   显式过期（卡片 patch 为"已失效，请重新发起或本地处理")。
7. session 在上游被 archive → 下一条消息报错并提示 `/sessions` 切换，
   不自动新建（**不替用户做隐式决定**)。

## 5. 权限与身份

- 首个管理员通过在飞书内发送 `/init <token>` 登记（init token 由 kited
  首次启动时生成——`kitectl config init-token` 可查看 token 及其存放
  位置；流程仿 FOCUS)；管理员集合存实例配置。
- MVP 只有两级：**管理员**（全部命令 + `kitectl`）与**非管理员**（不可
  使用，`/help` 与 `/whoami` 除外）。允许名单（多用户）是 Phase 2 候选。
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
4. 管理员登记采用 FOCUS 式 init token 流程（token 由 kited 首次启动时
   生成——2026-07-25 更正，原写"安装时生成";`kitectl config init-token`
   可查看——飞书内 `/init <token>` 登记首个管理员）。
5. `/mode` 枚举按上游修正为 `auto/manual/yolo`（证据：
   `packages/protocol/src/rest/prompt.ts:41`）；`plan` 不是
   `permission_mode` 取值，而是独立的 `plan_mode` 布尔字段，以
   `/plan [on|off]` 暴露（2026-07-21 对照上游代码修正）。
6. Spike 第 0 里程碑在 kimi 0.28.1 上通过（2026-07-21;
   `docs/verification/spike-results.md`);`/abort` 行已补充实测的 40402
   重复 abort 行为。
7. `/new` 在有 active prompt 时拒绝（fail-closed 防在途工作失去可见性）(2026-07-23)。
8. kited 停止时对全部待处理审批/问题做 fail-close 收口：上游应答（审批→拒绝、
   问题→dismiss），kap 不可达时卡片仍在本地 patch 为已过期/已关闭
   (2026-07-23)。
9. 执行中的执行卡带"取消执行"按钮（与 `/abort` 同权限：发起者或管理员）;
   点击幂等——已结束的 prompt 提示"已结束"(40402)(2026-07-24)。
10. `kitectl interaction sweep [--session <id>] [--yes]` 对上游陈旧的待处理
   审批/question 做拒绝/dismiss 收口（不带 `--yes` 时仅预览）；这些是上游
   kap 资源，故直连 kap REST(2026-07-24)。
11. `/switch`（含 `/sessions` 卡片按钮）在有 active prompt 时拒绝（与
   `/new`（对齐项 7）同理由：在途执行卡、终态结果与审批路由会失去可见
   性）(2026-07-25)。
12. 准入 `/effort`、`/goal`、`/compact`、`/rename`、`/archive`、`/restore`
   (2026-07-25):binding 级 `effort`(thinking）与 `goal_objective` 与
   permission mode 同样落 binding store；生命周期动作为 kap 透传。
   `kitectl`/`kited` 的 shell 补全随安装提供（bash/zsh/fish 生成器，
   仿 FOCUS 的 `shell_completion.py` 形态）。
13. 准入 `/btw`(2026-07-26)：旁路面。上游 `:btw` 启动的是旁路
   **agent**（而非插入便签）;`/btw 〈文本〉` 按需启动（每 session 内存
   缓存）并以该 `agent_id` 提交。**事件路由（2026-07-27 修正，复审
   N3-HIGH-1)**：管线按 `agent_id` 分流——主 agent 驱动既有卡片管线;
   btw agent 的事件走轻量路径：不创建/接管执行卡，不污染主卡流式文本,
   其答复在 `turn.ended` 时以纯文本（自 volatile 流累积）发给发起
   chat。错误帧只作用于本 agent;work state 只跟踪主 agent。归属记到
   本 chat，审批路由不变。不排队、不打断主 turn。本刀边界：btw prompt
   不支持 `/abort`；重启后 btw agent 按需重建，旧 agent 交由上游卫生
   机制处理（2026-07-27 登记）。
14. `/goal` 重接线（2026-07-27，复审 N2-HIGH-1):prompt 提交路由解析但
   静默丢弃 `goal_*` 字段——上游真实 goal 路径为
   `POST .../profile {agent_config.goal_objective|goal_control}` 与
   `GET .../goal`。`/goal` 改用这些路由，本地不再持久化（binding store
   的 `goal_objective` 字段与 per-prompt 携带模型已移除）。
15. `/archive` 在有 active prompt 时拒绝（与 /switch 同理由）(2026-07-27)。
