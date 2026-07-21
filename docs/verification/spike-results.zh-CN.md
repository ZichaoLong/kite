# Spike 结果（第 0 里程碑）

> 类型：verification（验证支撑，非产品语义）。
> 执行日期：2026-07-21。已验证版本：**kimi 0.28.1**(kap-server 0.0.2)。
> 脚本：`scripts/spike/`（共享辅助 `kap.py` + 每项一个脚本）。
> 规格：`docs/verification/spike-checklist.md`。
>
> **结论：全部通过（S1–S5 硬门槛；S6 摸底完成）。** 引起文档改动的发现
> 见 §8，各项已于 2026-07-21 折入对应文档，`contracts/`、
> `architecture/`、`decisions/` 同步转为 active。

## 0. 环境与供给

- 每次运行：`kimi web --no-open` 子进程 + 全新临时 `KIMI_CODE_HOME`
  (`mktemp -d`)，仅 loopback；真实 `~/.kimi-code` 未被触碰；运行结束后
  无残留 `kimi` 进程。
- **供给注意（与 kited 相关）**：隔离的 `KIMI_CODE_HOME` 没有 provider
  配置，提交 prompt 会失败 `40110`。agent-core-v2 读取 env 覆盖层
  (`KIMI_MODEL_NAME` / `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL`;
  `app/provider/configSection.ts` + `app/model/envOverlay.ts`)。此外，
  经 REST 创建的 session **不**继承覆盖层的 `defaultModel`——脚本对每个
  prompt 显式传 `model: "__kimi_env_model__"`。所用模型：
  `kimi-for-coding`。

## S4. managed 子进程全流程——通过（6/6)

1. 端口 58627 被占 → 服务绑到 **58628**(+1 重试）；实例注册表
   `<home>/server/instances/<serverId>.json` 中 pid/port 正确。
2. `server.token`:43 字符 base64url，文件 **0600**，目录 **0700**。
3. `kimi web rotate-token`（同一 `KIMI_CODE_HOME`）重写 token 文件；
   运行中的服务**热加载**：旧 token → `/meta` 返回 `40101`/HTTP 401,
   新 token → code 0，无需重启。
4. SIGTERM → rc 0；实例条目注销（目录清空）。
5. `kill -9` → 残留陈旧条目；同一 home 重新拉起成功，并在注册时
   **惰性清扫死 pid 条目**。
6. loopback 上 `POST /api/v1/shutdown` → `{code:0, data:{ok:true}}`,
   rc 0，注销。客户端侧注意：该 POST 不能带 `Content-Type:
   application/json` 的空 body(Fastify 会 50001)——非服务端 bug。

## S3. durable 重放窗口与 epoch——通过（含设计相关细节）

- **窗口运行时不可配**:`DEFAULT_MAX_BUFFER_SIZE = 1000`
  (`transport/ws/v1/sessionEventBroadcaster.ts:180`)，仅构造参数；
  `start.ts` 未传，无 CLI/env。超窗用 1050 次
  `POST /sessions/{id}/profile` 改名制造（durable
  `session.meta.updated`，无需模型调用）。
- cursor 落后 >1000 → `resync_required`(`reason=buffer_overflow`、
  `current_seq`、`epoch`）以独立帧**先于** subscribe ack 到达（等 ack
  期间必须能缓存帧）。通过。
- cursor 落后 10 → 恰好重放缺失的 10 条；无 resync。通过。
- **服务重启后对冷 session 订阅 → 无解释的 `resync_required`**（无
  reason 帧、无服务端 cursor):broadcaster 经
  `ISessionLifecycleService.get` 惰性激活 session（非 `resume`)。规避：
  订阅前先触一条 resume 语义的 REST 路由（`GET /sessions/{id}/prompts`)。
  已记入 `kite-design.md` §5。
- 触过 resume 路由后，带重启前 cursor 重订阅会**从磁盘 journal 按相同
  epoch** 重放缺失事件——单纯重启从不轮换 epoch(journal JSONL 位于
  `<home>/server/events/<sid>.jsonl`;epoch 仅在 journal 损坏时轮换）。
  通过。
- 外来 epoch → `resync_required(epoch_changed)` 带当前 epoch。通过。
- snapshot 携带 `as_of_seq`、`epoch`、`session(busy/pending_interaction)`、
  `messages{items,has_more}`、`in_flight_turn`、`pending_approvals`、
  `pending_questions`、`subagents`——足以重绘卡片。注意：默认
  `KIMI_SNAPSHOT_READER=auto` 模式下 `GET /snapshot` 直接读盘，**不会**
  resume 活 session（不能当预热触发）。

## S1. 多客户端并发与审批路由——通过

同一 session 挂两个 WS 客户端均被接受；manual 模式 prompt 触发
`event.approval.requested`(**两端**都收到）;`GET
approvals?status=pending` 列出；A 经 REST resolve →
`event.approval.resolved`(**两端**);pending 列表即时清空；重复
resolve → **40902** `{resolved:false}`；未知 approval id → **40404**。

## S2. prompt 队列与 abort/steer 边界——通过（含合同注意）

- 三连 enqueue → `running, queued, queued`;`GET prompts` 显示
  active + FIFO 队列；并发提交只排队、不拒绝。
- abort **queued** prompt → code 0 `{aborted:true}`；队列缩短。
- **steer URL 为单冒号** `POST /sessions/{id}/prompts:steer`；双冒号
  `prompts::steer` 是路由注册拼法，调用返回 40001。kimi-web 用单冒号。
- steer 非 pending id → **40402** "one or more prompts are not pending"。
- abort active → code 0。**对已完成 prompt 再 abort → 40402**，而非文档
  记载的 40903（记录已从队列移除）。
- `prompt.aborted`(queued 与 active abort）与 `prompt.steered` 均为
  durable 且可归因（`promptId` / `activePromptId+promptIds`)；队列排空至
  `{active:null, queued:[]}`。
- **缺口：`prompt.submitted` 与 `prompt.completed` 在 agent-core-v2 中
  无生产者**（仅 schema 定义，grep 确认）——线上从不出现。prompt 生命
  周期归因必须靠 REST submit ack + `prompt.aborted`/`prompt.steered` +
  `turn.*`。已折入 `kite-design.md` §5。

## S5. snapshot 重建进行中 session——通过

- 热 session(prompt 进行中、approval 待处理、一个排队 prompt、WS 全
  断）:snapshot 返回 `in_flight_turn{turn_id, current_prompt_id,
  running_tools, assistant_text}`、`pending_approvals=[aid]`、
  `session.busy=true`、`pending_interaction=approval`;`GET prompts`
  显示 active + queued。仅凭 snapshot + prompts 可重建卡片。
- 冷 session（服务重启后）:snapshot code 0,`as_of_seq`/epoch 来自
  journal;**无隐式激活**（未创建 journal 文件，reader 模式）。未知
  session → 40401。

## S6. question 触发频率摸底——完成（n=3，未触发）

auto 模式 prompt（写代码 / pip 安装 / 联网搜索）:**0 次
`event.question.requested`,0 次 `event.approval.requested`**；所有
turn 无人值守完成。样本太小不足以估频率；这些代表性流程未触发
question 表单。（question 表单按 mvp-scope 已对齐 2 留在 MVP，按钮卡
布局仍是设计方向。)

## 7. 与 `docs/research/kap-server-usability.md` 的上游漂移

1. `sessionEventBroadcaster.ts` 移至 `src/transport/ws/v1/`。
2. steer 线上 URL：单冒号 `prompts:steer`。
3. REST `session.last_seq` 是硬编码占位 0。
4. 重复 abort 返回 40402，非文档记载的 40903。
5. 重启后冷订阅 → 无解释 `resync_required`（惰性激活；原文未记载）。
6. `prompt.submitted`/`prompt.completed` 无生产者。

全部修正已录入 `kap-server-usability.md`「Spike 修正（2026-07-21)」。
其余各项（envelope、错误码 40902/40402/40404、cursor `{seq,epoch}`
语义、WS upgrade 认证、shutdown 路由、实例注册表、token 热加载）与调研
文档及代码一致。

## 8. 已折入文档的设计调整（2026-07-21)

- `kite-design.md` §5:durable 驱动列表不再依赖
  `prompt.submitted/completed`;resync 纪律补充 pre-ack 帧与冷 session
  预热两条细节；cursor 事实源钉为 WS ack / snapshot `as_of_seq`（禁用
  `session.last_seq`)。
- `mvp-scope.md`:`/abort` 行补充 40402 重复 abort 行为。
- 已验证版本回填：`kite-design.md` §10 与 `spike-checklist.md` 通用约定
  → **kimi 0.28.1**。

## 9. 复现

`python3 scripts/spike/s4_lifecycle.py`（其余各项类推——每项一脚本；
共享辅助 `scripts/spike/kap.py`)。需要环境中有 `KIMI_API_KEY` /
`KIMI_BASE_URL`（模型相关项）；脚本总是拉起自己的隔离 home 服务。
