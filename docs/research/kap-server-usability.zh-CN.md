# kap-server 可用性调查

> 类型：research（证据材料，非合同）。调查时间：2026-07-21。
> 调查对象：kimi-code monorepo 内 `packages/kap-server` 及周边
> （`apps/kimi-code`、`apps/kimi-web`、`packages/klient`、`packages/agent-core-v2`)。
> kap-server 演进极快，本文事实可能过时；与上游最新代码冲突时以上游代码为准。

## 结论

作为 KITE 的共享后端，kap-server 的 API 面覆盖度已足够支撑完整桥接闭环
（REST 写入 + WS 订阅 + 审批/表单响应）。它非常年轻（0.0.2,2026-07-12 首个
commit)，无稳定性承诺，需要版本跟随（不钉死，见 `docs/architecture/kite-design.md` §10)+ 快照 diff 护栏。

## 1. 生命周期与部署形态

- `kimi web` **同进程**拉起 kap-server(`await startServer(...)`)，非子进程：
  `apps/kimi-code/src/cli/sub/web/run.ts:236-312`(`runServerInProcess`，注释
  "The server always runs in the current process, attached to the terminal")。
- **无 daemon/service 支持**，前台进程，Ctrl+C 即停；官方注释 "there is no
  kill/ps subcommand"(`apps/kimi-code/src/cli/sub/web/index.ts:5-10`)。
  0.28.0 之前的遗留后台 server 用 `kimi server kill` 清理（`legacy-kill.ts`)。
- **库形式导出**:`packages/kap-server/src/index.ts:6` 导出 `startServer()`;
  不传 `webAssetsDir` 就是纯 API server(`start.ts:114-119`)。
- 端口：默认 58627(`start.ts:139-140`);EADDRINUSE 时 port+1 重试，上限 100 次
  (`start.ts:548`,`listenWithPortRetry` 577-611);`port: 0` 走 ephemeral。
- 多实例共存无单实例锁：每实例写 `<home>/server/instances/<serverId>.json`
  (`instanceRegistry.ts`,pid 探活 + 15s 心跳 + lazy sweep);
  `listLiveServerInstances()` 可发现活实例。
- 优雅关停：SIGINT/SIGTERM → `app.close()` + `core.dispose()` + 注销实例；
  另有 `POST /api/v1/shutdown`(loopback 默认启用，非 loopback 需
  `--allow-remote-shutdown`)。

### Token 认证

- 位置 `~/.kimi-code/server.token`(0600，目录 0700，原子 rename 写入）;
  256-bit random → base64url 43 字符（`services/auth/persistentToken.ts:28-31`)。
- 首次 boot 自动生成，跨重启复用；`kimi web rotate-token` 原子重写；
  运行中 server 靠 mtime+inode 变化在下一次 auth check **热加载，无需重启**
  (`services/auth/tokenStore.ts:34-66`)。
- **无权限位**：单一全权 bearer。可选第二凭证 `KIMI_CODE_PASSWORD`(bcrypt)
  与启动选项 `rpcToken`;`--dangerous-bypass-auth` 可整体关 auth。
- WS 认证在 upgrade 时完成：`Authorization: Bearer` 或 `sec-websocket-protocol`
  子协议携带（`start.ts:421-458`)；连接后 `client_hello` 可再带 token（防御纵深）。

## 2. API 面对桥接需求的覆盖度

全部路由挂在 `/api/v1`(`routes/registerApiV1Routes.ts`)，统一 envelope
`{code, msg, data, request_id}`(HTTP 恒 200，业务结果看 code)。

| 桥接需求 | 接口 | 证据 |
| --- | --- | --- |
| session 创建（指定 cwd) | `POST /sessions`,body `{workspace_id \| metadata.cwd, title?}` | `routes/sessions.ts:248-341` |
| resume | 无显式端点——任何 per-session 路由内部 `resume()` 冷加载，对客户端透明 | `routes/prompts.ts:103-113` |
| list | `GET /sessions`(id-cursor 分页；busy/archive/workspace 过滤） | `routes/sessions.ts:343-463` |
| read 历史 | `GET /sessions/{id}/messages`;turn 粒度 `GET .../transcript`;IM 式全量重建 `GET .../snapshot`（含 `as_of_seq`+`epoch`+`in_flight_turn`) | `routes/messages.ts:85-117`、`routes/transcript.ts:93`、`routes/snapshot.ts:86-99` |
| rename | `POST /sessions/{id}/profile`(title/metadata/agent_config/permission_rules) | `routes/sessions.ts:557-602` |
| archive / restore | `POST /sessions/{id}:archive` / `:restore` | `routes/sessions.ts:747-773` |
| delete | **无**（全 server 仅 workspaces/files/oauth 有 DELETE) | grep 确认 |
| 其他 action | `:fork` `:compact` `:undo` `:abort` `:btw`（侧 channel)、children | `routes/sessions.ts:604-916` |
| 发起 turn | `POST /sessions/{id}/prompts`(content: text/image/video/file；可携带 per-request `model`/`thinking`/`permission_mode`/`plan_mode` override) | `routes/prompts.ts:163-229`、`protocol/rest-prompt.ts:31-42` |
| 队列查询 | `GET /sessions/{id}/prompts`(active + queued) | `routes/prompts.ts:140-161` |
| 中断 | `POST .../prompts/{pid}:abort`;`POST /sessions/{id}:abort` | `routes/prompts.ts:260-302`、`routes/sessions.ts:721-728` |
| steer | `POST /sessions/{id}/prompts::steer`——**只能把已排队 prompt 注入活动 turn**;engine 的 `inject()` 未暴露 REST | `routes/prompts.ts:231-258`、`agent-core-v2/.../promptService.ts:115-137` |
| approval 推送 | WS `event.approval.requested` / `event.approval.resolved` | `sessionEventBroadcaster.ts:1281-1348` |
| approval 查询/响应 | `GET .../approvals?status=pending`;`POST .../approvals/{aid}` `{decision: approved\|rejected\|cancelled, scope?, feedback?}`；幂等：60s 内重复 resolve → 40902 | `routes/approvals.ts`、`protocol/approval.ts:5-30` |
| question 推送/响应 | WS `event.question.requested/answered/dismissed`;`GET/POST .../questions/{qid}`、`:dismiss` | `routes/questions.ts:1-42` |
| 运行时设置 | 全局 `GET/POST /config`;`GET /models`、`POST /models/{alias}:set_default`。**per-session 设置无独立端点**，只能随 prompt 携带或走 profile.agent_config | `routes/config.ts`、`routes/modelCatalog.ts` |
| 事件流 | WS `/api/v1/ws`，协议版本 2(`protocol/ws-control.ts:19`) | 见下节 |

## 3. WS 事件模型

帧格式 `{type, seq, epoch, volatile?, offset?, session_id, timestamp, payload}`。

- **durable**（编号进 journal，可重放）:`turn.started/ended`、`turn.step.*`、
  `tool.call.started`、`tool.result`、`prompt.submitted/completed/aborted/steered`、
  `subagent.*`、`compaction.*`、`task.*`、`event.session.work_changed`
  (busy/main_turn_active/pending_interaction)、`event.session.created`、
  `session.meta.updated`、`event.config.changed`、`event.approval/question.*`。
- **volatile**（不占 seq、不重放）:`assistant.delta`、`thinking.delta`、
  `tool.call.delta`、`tool.progress`、`shell.*`、`agent.status.updated`;
  文本增量带累计 `offset` 供缺口检测（`ws-control.ts:46-53`)。
- 订阅语义：按 session 订阅（可多个），可选 `agent_filter`；控制帧
  `client_hello`/`subscribe`/`unsubscribe`/`ack`/`resync_required`。
- **断线补偿**：客户端带 `{seq, epoch}` cursor 重订阅，server 从内存 tail 或
  磁盘 journal(`<home>/server/events/`，跨重启保留 seq）增量重放；超窗
  （默认 1000 条）或 epoch 不符 → `resync_required` → 客户端走 REST snapshot
  重建（`sessionEventBroadcaster.ts:516-563`)。即 IM 式多端同步模型。
- WS **无 server 心跳**（无 ping/pong)，连接活性需客户端自检（kimi-web 用
  stale 检测）。

## 4. 多客户端并发语义

- **多 WS 客户端订阅同一 session：原生支持。** broadcaster 按连接独立保存
  filter/grades,fan-out 给所有 targets；慢客户端有 per-connection
  backpressure + delta 合并。
- **没有 ownership/lease/active-client 概念。** REST 写路径只验 bearer token;
  `GET /connections` 仅只读列出连接。
- **两客户端同时向同一 session 发 prompt：排队，不拒绝。**
  `IAgentPromptService.enqueue` 进 per-agent FIFO 串行消费
  (`agent-core-v2/src/agent/prompt/promptService.ts:84-109`)。`session.busy`
  错误码在 v2 只有定义、无抛出点。
- **官方 web UI 多标签页**（现成证据）：每标签页各自开 WS,**无 leader
  election / 标签页协调**，断线按 cursor 重连 + snapshot 重种子。即官方前端
  就是"多客户端平等并发 + 服务端排队 + 事件广播收敛"模式。
- **⚠ 跨进程无 session 锁**：两个 kap-server 实例共享 homeDir 时可同时 resume
  同一 session，各自维护内存态、各自写 journal/wire 文件。kap-server live 持有
  某 session 时用 TUI `kimi -S` resume = 两进程写同一 session 目录。

## 5. 成熟度与稳定性信号

- **Dogfooding**:kimi-web 完全走 `/api/v1` REST + `/api/v1/ws`。反例：vscode
  扩展与交互式 TUI 不走 kap-server（进程内 SDK harness)。
- **测试**:55 个测试文件、约 671 个 test/it（含 boot/auth/ws 单测与若干
  e2e);`test/apiSurface.snapshot.test.ts` 用 `/openapi.json` 派生路由表快照做
  API 面回归护栏。
- **OpenAPI/AsyncAPI 均代码生成**（路由级 zod schema 经 @fastify/swagger;
  AsyncAPI 由 ws-control 操作目录生成）,schema 单一来源，非手写文档。
- **版本**:0.0.2,CHANGELOG 仅两条，**无稳定性承诺**;git 历史 43 个 commit
  (2026-07-12 起，两周）。`/api/v2` RPC surface 曾出现又在 5ae60fa 被移除——
  API 面仍在快速演进。
- **"v1"前缀含义**：继承自已删除旧 v1 server 的 wire 兼容面；`/meta` 里
  `backend: 'v2'` 指引擎是 agent-core-v2。API 代数（v1）与引擎代数（v2）正交；
  WS 侧另有显式 `protocol_version: 2`。
- **引擎同一性**:kap-server = agent-core-v2；交互式 TUI 默认 agent-core v1
  (`main.ts:98`);`kimi -p` 可用 env flag 切 v2。**TUI 与 server 引擎不同代。**

## 6. TUI 远程接续与 klient

- 交互式 `kimi` TUI **不能**连 kap-server，无 `codex --remote` 类模式。
- `kimi -S/--session` resume 的是**磁盘会话**，本地进程续跑，不能 attach 到
  kap-server 的 live session。
- `packages/klient` 的 ipc 传输（unix socket + ndjson,`serveKlientIpc`)**仓内
  无任何生产使用者**，只有 examples 与自身测试；kap-server 自身不暴露 ipc。
- kimi-cli 时代的 `--wire`(stdio JSON-RPC）在 kimi-code 中**已移除**，测试注释
  写明 "held back from the first release … for when those flags return"——
  未来可能回归。

## 7. 风险与缺口（站在 KITE 共享后端立场）

### 硬缺口

1. 无 session delete（只能 archive)
2. 无 ownership/lease/单写者语义
3. per-session model/permission 无独立读写端点（只能随 prompt 携带）
4. steer 只能注入"已排队"prompt，不能直接塞进进行中 turn
5. token 无细粒度权限，无 per-client 身份（仅自报 user_agent)
6. 跨进程无 session 锁——与 TUI/`kimi web` 并存操作同一 session 有撕裂风险
7. WS 是纯订阅面：写操作全走 REST，两条通道都要维护

### 软风险

1. 0.0.2、两周历史、无稳定性承诺；升级需跟随快照 diff
2. 无 daemon 模式，进程管理自理（或由 KITE 作为子进程看管）
3. WS 无心跳，连接活性客户端自检
4. volatile 事件不可靠：靠 offset 缺口检测 → resync → snapshot 重建
5. 非 loopback 部署无 TLS;rate limit 仅覆盖 auth 失败路径

### 需实测验证的点

见 `docs/verification/spike-checklist.md`。

---

## 补充核查(2026-07-21):web UI 共存、产品重心、LAN 访问

### A. web UI 与桥接共存

- kimi-web 是纯 /api/v1 + /api/v1/ws 客户端,服务端无特权处理:静态资源
  走 auth 豁免(`src/middleware/auth.ts:64-79`),API 调用用同一个
  server.token;`X-Kimi-Client-*` 头只出现在 CORS allow-headers,不参与
  授权(`src/middleware/origin.ts:28`)。
- token 进入 web UI 三途径(`apps/kimi-web/src/api/daemon/serverAuth.ts`
  头注释):URL `#token=`(`kimi web` 开浏览器时拼接,读后即抹除)、登录
  弹窗手输、localStorage(7 天 TTL)。
- 无独占后端假设:`apps/kimi-web` 全库 grep 无 shutdown/connections 调用;
  注释明确多客户端设计(`useKimiWebClient.ts:958` 等);CHANGELOG 0.20.0
  #1081 跨客户端同步标题为一等场景。
- `POST /api/v1/shutdown`:loopback 默认挂载(任何 token 持有者可关停
  整个 server);非 loopback 默认 404,需 `--allow-remote-shutdown`
  (`src/start.ts:170`、`routes/registerApiV1Routes.ts:168-172`)。

### B. 产品重心信号

- TUI 内置 tip:`apps/kimi-code/src/tui/constant/tips.ts:33`
  `/web: use the Web UI for a better experience`,同时出现在 working tips
  与 footer 轮转(每 10s),非一次性。
- 另有远程 tips banner 通道(`cdn.kimi.com/kimi-code-tips/tips.json`,
  支持版本定向/时间窗/冷却),当前未推 web。
- 官方口径:根 AGENTS.md:18 web UI 是 "a peer to the TUI";README/guides
  仍以 TUI 为主。
- 投入信号:0.24.0→0.28.1 CHANGELOG 中 `web:` 前缀条目 87 条;0.28.0 以
  `kimi web` 取代整个 `kimi server` 命令树;移动端适配持续投入(mobile
  shell、bottom sheet、iOS 防缩放)。
- 引擎:TUI 仍 agent-core v1 进程内;无 TUI 迁 v2 或 TUI 连 server 的
  路线图证据(无 klip 类文档)。
- "wire held back" 完整注释见
  `test/cli/session-flag-picker.test.ts:70-74`:--print/--wire 是暂缓发布
  的 flag,源码中保留 validateOptions 守卫。

### C. 局域网/手机访问

- `kimi web --host`:省略=127.0.0.1;裸参=0.0.0.0;可指定 LAN IP
  (`apps/kimi-code/src/cli/sub/web/shared.ts:90-94`)。绑 0.0.0.0 时启动
  banner 为每网卡打印带 `#token=` 的 Network URL(`access-urls.ts:62-85`),
  注释明示用于"在另一台设备上打开链接并自动认证"。
- Host header 校验(DNS rebinding 防线):默认放行 localhost/字面 IP/
  绑定地址/白名单(`--allowed-host` 或 KIMI_CODE_ALLOWED_HOSTS),拒绝
  40301(`src/middleware/hostnames.ts:119-153`);HTTP 与 WS 均强制。
- 非 loopback 加固:auth 失败按源 IP 限流(42901)、安全响应头、
  shutdown/terminals 默认 404;不强制 password,仅警告
  (`src/start.ts:216-227`)。
- 无内置 TLS;官方姿态:反代/隧道终结(`src/start.ts:167,217-220`)。
- 手机登录:打开带 token 的 URL 自动认证;或 /login 弹 ServerAuthDialog
  输 token/password(`apps/kimi-web/src/composables/useAuthGate.ts:19-47`)。
- 移动端适配:viewport-fit=cover;≤640px 单栏 mobile shell
  (`useIsMobile.ts` + `components/mobile/` 三组件);CHANGELOG 有移动专项
  条目(0.28.0 mobile permission sheet 等)。

---

## Spike 修正（2026-07-21,kimi 0.28.1)

由执行 `docs/verification/spike-checklist.md` 发现（详见
`docs/verification/spike-results.md`)。本节与上文冲突时，以本节为准。

1. `sessionEventBroadcaster.ts` 移至
   `src/transport/ws/v1/sessionEventBroadcaster.ts`。
2. steer 线上 URL 为单冒号 `POST /sessions/{id}/prompts:steer`;§2 中
   `prompts::steer` 是路由注册拼法，调用返回 40001。
3. REST `session.last_seq` 是硬编码占位 0
   (`routes/sessions.ts:1069`)；真实 journal seq 只能从 WS ack cursor
   或 snapshot `as_of_seq` 获得。
4. 对已完成 prompt 重复 abort 返回 **40402**（记录已从队列移除）;
   40903 幂等路径在 0.28.1 中疑似为死代码。
5. 服务重启后对冷 session 订阅返回无解释的 `resync_required`（经
   `ISessionLifecycleService.get` 惰性激活，非 `resume`)；先触一条
   resume 语义的 REST 路由可规避。单纯重启从不轮换 epoch(journal
   JSONL 保留）;epoch 仅在 journal 损坏时轮换。
6. `prompt.submitted` / `prompt.completed` 在 agent-core-v2 中**无生产
   者**（仅 schema 定义），线上从不出现；归因用 REST submit ack +
   `prompt.aborted` / `prompt.steered` + `turn.*`。
7. 重放窗口（1000）仅为构造参数——运行时无 CLI/env 可配;
   `resync_required` 帧可能**先于** subscribe ack 到达（等 ack 期间须
   缓存帧）。
8. 供给：隔离 `KIMI_CODE_HOME` 的模型配置来自 `KIMI_MODEL_NAME` /
   `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` env 覆盖层，且经 REST
   创建的 session 不继承覆盖层 `defaultModel`——每个 prompt 显式传
   `model`。
9. prompt 提交路由解析但静默丢弃 `goal_*` 字段（2026-07-27 对照
   `routes/prompts.ts` 验证：无 goal 消费者；§2 旧行中的 `goal_*` 论断
   有误）。真实 goal 路径为
   `POST /sessions/{id}/profile` 携带
   `agent_config.goal_objective` / `agent_config.goal_control`（触发
   createGoal/pause/resume/cancel）与 `GET /sessions/{id}/goal`。相对
   地，`thinking` 确实被提交路由消费（`routes/prompts.ts:154-173`)。
