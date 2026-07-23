# 决策：kitectl 与 kited 之间的 loopback 控制面

> 状态：已决（2026-07-23，由 FOCUS 资产盘点发现的线上 bug 驱动；
> 见 `docs/research/focus-assets-map.md` §0.1)。

## 问题

`kitectl prompt send`(MVP 交付，作为后期定时能力的控制面入口）直连
kap-server REST，成为 prompt 归属状态轴（kite-design §4 轴 4）上的
**第二写者**:daemon 的内存归属表永远学不到 CLI prompt 的发起者，其触发
的审批没有确定归属，只能走 fail-closed 路径——审批卡被按**过期**发出。
CLI 发起的 prompt 在飞书侧实际上无法审批。

次要问题：kitectl 的 REST 错误处理无法区分"请求未到达"与"已发出、响应
丢失"。对非幂等的 prompt 提交盲目重试可能重复入队。

## 决策

在 `kited` 内增加 **loopback 控制面**，仿 FOCUS 的
`service_control_plane.py`（改名移植）:

- loopback TCP 上的 JSON-lines，单行请求/响应，1 MB 上限，token 校验
  (daemon 签发的 token，与其他实例密钥同处 0600 存放），仅 loopback——
  不出主机，无需 TLS。
- 端点经小型元数据文件发布（端口 + token 路径）,kitectl 发现活 daemon
  而非相信记录的端口（"活进程优先于记录"，与 kap 实例注册表同理）。
- `kitectl prompt send` 变为 **daemon 的客户端**：提交经 RuntimeLoop 串行
  化，归属记录与飞书发起的 prompt 完全一致；卡片/审批行为与入口面无关。
- 错误分类为三态：连接拒绝 → "确定未送达"（可安全重试）；已发出但无有效
  响应 → **outcome unknown**（不盲目重试，提示用 `kitectl session status`
  核实）；业务错误 → 透传上游 msg。

只读 kitectl 查询（`session list/status`、`binding list`）继续直连 kap
REST / stores——只有必须经 daemon 串行化的变更走控制面。活态内省（待决
审批、归属视图）后续可经同一通道暴露，不在本批。

## 为什么不选替代方案

- **直连 REST + 幂等键**:kap 的 prompt 提交无幂等键，且仍无法在 daemon
  记录归属。两个问题都不解。
- **文件/队列 IPC**：无法同步回答"daemon 是否已受理"，也拿不到活内存
  状态。
- **让 kitectl 给自己发飞书消息**：循环、脆弱，且破坏管理面分层。

## 后果

- 控制面是 kitectl→kited 唯一的变更通道；凡改动 daemon 所属状态的操作
  必须经它（归属轴恢复单写者纪律）。
- `kitectl prompt send` 语义变化：回复变为"daemon 已受理"（带 prompt id,
  当可知）;outcome-unknown 如实报告。MVP 合同中 kitectl 行仍然有效，
  行为注记在实现时记入 mvp-scope 已对齐一节。
- 定时 prompts(Phase 3）走同一通道——这正是 `kitectl prompt send` 的
  初衷，本决策将其补全。
