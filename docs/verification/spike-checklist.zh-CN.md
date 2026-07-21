# Spike 验证清单（第 0 里程碑）

> 类型：verification（验证支撑，非产品语义）。
> 目的：在写任何业务代码之前，用脚本打真实 kap-server，验证
> `docs/research/kap-server-usability.md` §7 列出的"需实测"项。
> **全部通过才允许开工；任何一项不达预期，回到对应 decision 文档重议。**

## 通用约定

- 环境：验证时所用 kimi-code 版本（**kimi 0.28.1**,2026-07-21 验证；仅作"已验证版本"记录，不构成锁定，见 kite-design §10)，`kimi web
  --no-open` 本地 loopback 拉起，独立临时 `KIMI_CODE_HOME`。
- 形态：纯脚本（Python 标准库 + websockets，或直接 curl + wscat)，不入库
  业务代码；脚本可放 `scripts/spike/` 留存。
- 每项记录：实测行为、与预期差异、结论（通过/需设计调整）。

## S1. 多客户端并发与审批路由

- **验证**:session 上同时挂两个 WS 客户端；客户端 A 经 REST 发 prompt;
  触发 approval 后客户端 A resolve。
- **观察**:B 是否收到 `event.approval.requested` 与 `event.approval.resolved`;
  A resolve 后 `GET .../approvals?status=pending` 是否即时清空；重复 resolve
  是否返回 40902。
- **通过标准**：广播到达、幂等语义如文档所述。
- **影响**:`kite-design.md` §4 prompt 归属路由、§6 审批卡合同。

## S2. prompt 队列与 abort/steer 边界

- **验证**：同一 session 连续 enqueue 3 个 prompt;active 期间尝试
  (a) abort active、(b) abort queued、(c) steer 一个 queued prompt、
  (d) 对空队列 steer。
- **观察**：各操作的错误码与事件序（`prompt.submitted/aborted/steered`);
  abort queued 是否支持；steer 后队列状态。
- **通过标准**：行为确定且可映射到 KITE 的卡片状态机；不允许出现无法
  归因到 prompt_id 的事件。
- **影响**:mvp-scope §3 并发行为；验证 `/abort` 的卡片状态机映射（已进 MVP,2026-07-21 对齐）。

## S3. durable 重放窗口与 epoch 语义

- **验证**：产生超过 1000 条 durable 事件的长 turn（或调小窗口），期间
  WS 断开；带旧 cursor 重连。另测 kited 侧崩溃后 kap-server 重启（journal
  跨进程重启）的 epoch 行为。
- **观察**：何时返回 `resync_required(buffer_overflow)`、何时
  `epoch_changed`;snapshot 重建所需字段是否齐全（`as_of_seq`、
  `in_flight_turn`、work state)。
- **通过标准**：超窗/重启路径都能确定性地落入 snapshot 重建，且 snapshot
  足以重绘执行卡。
- **影响**:`kite-design.md` §5 事件消费策略；适配层 resync 纪律。

## S4. managed 子进程全流程

- **验证**：以子进程方式拉起 `kimi web --no-open`（或 startServer shim):
  端口被占时的 +1 行为、token 文件生成与权限位、rotate-token 热加载、
  SIGTERM 优雅关停、异常退出后的端口/实例注册表残留。
- **通过标准**：拉起/关停/冲突/轮换全链路无需人工干预；实例注册表不
  误导后续发现。
- **影响**:`kite-design.md` §2 进程形态；验证已选定的拉起方式（`kimi web --no-open`,2026-07-21 对齐）。

## S5. snapshot 重建进行中 session

- **验证**:prompt 进行中（含 pending approval）断开所有 WS，直接调
  `GET .../snapshot`；另在无任何 WS 订阅时对"冷"session 调 snapshot。
- **观察**:in_flight_turn、pending_interaction、最近事件、队列内容是否
  齐全；冷 session 是否被隐式加载（副作用）。
- **通过标准**:kited 重启后仅凭 snapshot + `GET .../prompts` 即可重建
  执行卡与审批卡；若字段缺失，记录缺口并回 `kite-design.md` §4 调整
  "prompt 归属重建"条款。
- **影响**:mvp-scope §4.6 重启恢复条款。

## S6.（附加）question 触发频率摸底

- **验证**：跑一组代表性 prompt（写代码、装依赖、联网搜索），统计
  question.requested 出现频率与形态。
- **通过标准**：无硬性标准；产出作为 question 表单卡布局的设计输入（question 表单已进 MVP,2026-07-21 对齐）。
