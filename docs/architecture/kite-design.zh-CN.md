# KITE 总体架构设计（草案）

> 状态：**active**(2026-07-21 首轮对齐 + spike 验证完成，见 `docs/verification/spike-results.md`)。本文与代码不一致即 contract gap。

## 1. 目标与非目标

### 目标

- 飞书会话（先单聊，后群聊）驱动 kimi-code session：发起 prompt、流式观察、
  审批、查询/切换 session。
- 共享后端单一：实例内所有飞书会话通过同一个 kap-server 操作 session。
- 行为可推理：每条状态轴、每个失败模式都有显式合同；fail-closed。

### 非目标（Non-goals)

- 本地 TUI wrapper（对应 FOCUS 的 `focus`/`fcodex`)——见
  `docs/decisions/process-shape-and-language.md`。
- 记忆、语音 ASR、设备操控、文生图、MCP/Skills 热更新——见
  `docs/research/okbot-vs-focus.md`。
- session 删除（上游无此能力，不绕过）。
- 多实例 / 跨实例协调（MVP 单实例为前提；见
  `docs/decisions/concurrency-model.md`)。

## 2. 进程形态

```
┌─────────────────────────────────────────────┐
│ kited (Python daemon, systemd --user 等管理)  │
│  ┌───────────────────────────────────────┐  │
│  │ 飞书传输层 → 应用层 → 适配层            │  │
│  └──────────────┬────────────────────────┘  │
│                 │ 拉起并看管（managed 子进程） │
│                 ▼                           │
│  ┌───────────────────────────────────────┐  │
│  │ kap-server (kimi web --no-open,        │  │
│  │ 兼作本地 web UI 操作面)                 │  │
│  │  REST /api/v1  +  WS /api/v1/ws       │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
kitectl —— 本地管理面（配置/启停/binding/session/prompt/image)
```

- `kited` 是 kap-server 的父进程：负责拉起、端口冲突重试、token 读取、
  崩溃重启、优雅关停。kap-server 自身无 daemon 模式，KITE 的 managed 形态
  恰好补掉这一缺口（同构于 FOCUS 对 codex app-server 的 managed 模式）。
- kap-server 不单独注册 service；它随 `kited` 生灭。
- 认证沿用 kap-server 自家 token(`~/.kimi-code/server.token`),KITE 不造
  第二套凭证。WS 连接经 `Authorization: Bearer` 认证。
- 同一 kap-server 兼作**本地操作面**：`kimi web` 附带的 web UI 是纯
  /api/v1 客户端，与 KITE 地位平等，天然共享同一批 session（排队与广播
  由上游保证，见 `docs/decisions/concurrency-model.md`)。默认绑
  127.0.0.1;LAN/手机访问（`--host` 暴露）默认关闭，见末节已对齐 5。

## 3. 分层

| 层 | 职责 | 关键约束 |
| --- | --- | --- |
| 飞书传输层 | lark-oapi WS 长连接；消息去重、收发、卡片 patch、附件下载 | 只依赖飞书 SDK；不认识 kap-server |
| 应用层 | 命令路由、binding 解析、状态机、卡片模型、审批路由 | 所有跨连接/跨任务状态变更经 **RuntimeLoop** 单线程串行化 |
| 适配层 | kap-server REST 客户端 + WS 订阅客户端；类型归一化；resync 纪律 | kap 的 schema/envelope/DomainEvent **只允许出现在本层** |
| 本地状态层 | binding、UI 临时态、事件 cursor 等 JSON stores（原子写 + 文件锁） | session 元数据以 kimi-code 为单一事实源，本地不复制 |

词汇一律采用 kimi-code 原生术语：**session、agent、prompt、approval、
question**。不引入 codex 时代的 thread/turn 命名（FOCUS 资产移植时须改名的
第一批符号就是这批）。注意 kap-server 的 session 内含 agent 维度，prompt
发给 session 内的 agent——建模状态轴时必须保留这一层。

## 4. 状态轴

MVP 只承认四条轴，每条都有明确 owner:

1. **binding**（本地，持久）:chat ↔ session 的逻辑书签。kited 重启后保留。
2. **attached/detached**（本地，持久）:chat 是否接收该 session 的飞书推送。
3. **work state**（上游，订阅所得）:session 是否 busy、是否有
   pending_interaction——来自 `event.session.work_changed`,KITE 不自行推断，
   掉线时用 REST snapshot 重建。
4. **prompt 归属**（本地，内存）:active/queued prompt 各由哪个 chat 发起，
   决定审批/表单卡片路由给谁。重启后经 `GET .../prompts` + snapshot 尽力
   重建；建不回的审批卡片做显式过期收口（fail-closed)。

**预留概念（不实现）**:interaction owner（写入独占租约）、跨实例 loaded
gate。登记在 `docs/decisions/concurrency-model.md`，等产品证明需要时再引入；
届时新增轴必须先改本文档再改代码。

## 5. 事件消费策略

**durable 优先，volatile 后补。**

- MVP 卡片更新的唯一驱动是 durable 事件
  （turn.started / tool.call.* / turn.ended / prompt.aborted / prompt.steered /
  approval.* / question.* / session.work_changed)。这条路径没有不可靠事件。
  `prompt.submitted` / `prompt.completed` 仅有 schema 定义，agent-core-v2
  **无生产者**(spike S2)：提交由 REST 响应确认；完成经
  `turn.ended` / `prompt.aborted` 观察。
- 断线补偿纪律集中在适配层一处：带 `{seq, epoch}` cursor 重订阅 →
  `resync_required` 或超窗 → REST snapshot 重建 → 卡片按重建结果整体刷新。
  适配层须处理的 spike S3 细节：`resync_required` 帧可能**先于**
  subscribe ack 到达（等 ack 期间须缓存帧）；服务重启后对冷 session
  订阅会收到无解释的 `resync_required`（惰性激活）——订阅前先用
  resume 语义的 REST 调用（`GET .../prompts`）预热 session;journal
  跨重启保留同一 epoch(epoch 仅在 journal 损坏时轮换）。
- cursor 事实源：WS subscribe ack 与 snapshot `as_of_seq`。REST 的
  `session.last_seq` 是硬编码占位 0(spike S3)——禁用。
- WS 无心跳：适配层实现 stale 检测（超过 N 秒无任何帧即主动重连）。
- **volatile 流式**(assistant.delta 逐字 patch）是独立增强，Phase 2 再做；
  届时用 offset 缺口检测，缺一口即落入 snapshot 重建路径，不自己猜。

## 6. 卡片模型

沿用 FOCUS 经验，按 kap 事件语义重写：

- **单锚点执行卡**：同一 chat 任一时刻最多一张当前执行卡，由
  `{chat_id, session_id, prompt_id, card_message_id}` 锚定；prompt-scoped
  事件必须匹配 prompt_id 才能改卡（kap 的 prompt FIFO 语义使这比 FOCUS 的
  turn 匹配更简单： queued prompt 不建卡，started 才建）。
- **终态卡**:prompt 完成（completed/aborted/失败）后发独立终态卡，执行卡
  定格；终态文本落本地 store 供 `/last` 类命令读取。
- **审批卡**:approval.requested → 三键卡（批准/拒绝/反馈）,REST 响应后
  patch 定格；60s 幂等窗口内重复点击按"已处理"提示，不报错。
- question 表单卡：MVP 透传文本化（列出选项，回复编号选择）;Phase 2 再做
  富表单。

## 7. 持久化

- 全部 JSON 文件 + 原子写（tmp + rename)；不用 SQLite。写串行化：单写者
  (kited)+ 进程内锁（FOCUS 已验证的纪律）；仅在文件可能跨进程写入时
  使用劝告式文件锁。原子 rename 使跨进程读（如 `kitectl`）无需锁也
  安全。
- stores:binding store(chat ↔ session、attached、permission mode、plan
  mode)、终态结果 store、事件 cursor store（每 session 的 `{seq, epoch}`)、
  附件 staging store（后期）。
- binding 级 **permission mode**（对应 kap `permission_mode`:auto/manual/yolo)
  与 **plan mode**（kap `plan_mode`，独立的布尔字段）持久化，落盘后
  **不随实例默认漂移**，每个 prompt 显式携带（kap 的
  per-prompt override 原生支持，正好落实 FOCUS"每 turn 显式重新应用"
  的合同）。**模型**同样在每个 prompt 上显式携带——REST 创建的 session
  既不继承 env 覆盖层也不继承 `config.toml` 的 `default_model`（见
  spike-results §0)；解析顺序：`kap.model` 配置 → `config.toml`
  `default_model`。
- kimi-code 侧的 session 元数据（id、cwd、title、历史）以
  `~/.kimi-code` 为单一事实源；KITE 不复制、不 mirror。

## 8. 命令面（草案）

| 命令 | 作用 |
| --- | --- |
| `kited` | daemon 入口，由 service manager 调用 |
| `kitectl` | 本地管理面：config / service / binding / session / prompt / image |

飞书斜杠命令见 `docs/contracts/mvp-scope.md`。

**明确没有** `kite` / `kcode` 本地 TUI wrapper 命令（见 decisions)。
若未来上游支持远程 attach，再按 FOCUS 的 wrapper 设计补回，命令名预留
`kite`（本地入口）与 `kcode`（强调 Kimi Code 薄壳的别名）。

## 9. 服务与部署

- 平台分派沿用 FOCUS 的 service_manager 设计：Linux systemd --user、
  macOS launchd、Windows Task Scheduler。
- 安装：`install.sh` → 受管 venv + wrapper + service 定义（只写不启动）;
  禁止 `pip install .` / `-e .`（同 FOCUS 纪律）。
- 服务环境凭证：daemon 不继承用户 shell 环境，provider 密钥从 env 文件
  读取（默认 `~/.config/kite/env`,0600;`KITE_ENV_FILE` 可覆盖）——绝不
  写进 unit 定义。
- 单实例为前提：配置/数据目录一份；多实例（多飞书应用）登记为 Phase 3
  候选，届时需先补跨实例并发合同。

## 10. 上游依赖管理（2026-07-21 对齐：跟随，不钉死）

- **不硬性钉死 kimi-code 版本。** kimi-code 与 kap-server 都在快速演进
  (kap-server 从出现到调查仅两周），KITE 作为全新项目选择跟随演进：
  不指望长期停留在一个旧版本上——那反而不利于 KITE 的后续演进；保留
  随时重来的自由度。
- 安装/启动时检测上游版本，与"已验证版本"不符时**警告但不阻止运行**。
  当前已验证版本：**kimi 0.28.1**(2026-07-21 spike 通过；
  `docs/verification/spike-results.md`)。
- CI 护栏：对目标 kap-server 拉取 `/openapi.json` 与 WS 操作目录做快照
  diff，并跑适配层合同测试（loopback 真实 kap-server)。快照 diff 是
  **漂移感知**手段；发现漂移后在适配层内显式适配，并更新已验证版本。
- 适配层是全仓库唯一允许感知上游 schema 的地方；任何上游漂移适配改动
  必须限制在适配层内。

## 已对齐（2026-07-21)

1. kap-server 拉起方式：`kimi web --no-open` 子进程。它同时是后端与本地
   web UI 操作面（见 `docs/decisions/concurrency-model.md`);TS shim 路线
   放弃。
2. 配置/数据目录：KITE 独立目录（仿 FOCUS 的 platform_paths:
   `~/.config/kite` + `~/.local/share/kite`)，不挂在 `~/.kimi-code` 下。
3. Python 侧复用方式：从 FOCUS fork-copy 模块后按 KITE 词汇重命名改造；
   不抽公共包（避免两个仓库互相拖拽）。
4. `kitectl prompt send` 包含在 MVP（后期定时能力的控制面入口）。
5. LAN 暴露：默认 loopback；是否提供 `kitectl` 配置项开启 `--host` 暴露
   推迟到 Phase 2 再议（开启时必须同时提示设置 `KIMI_CODE_PASSWORD`
   与无 TLS 风险）。
6. Spike 第 0 里程碑在 kimi 0.28.1 上通过（2026-07-21;
   `docs/verification/spike-results.md`)；其发现已折入 §5（事件消费）与
   §10（已验证版本）。
