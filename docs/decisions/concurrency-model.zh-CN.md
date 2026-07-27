# 决策：并发模型——队列语义 + prompt 级归属

> 状态：已决（MVP 范围内）。本文与 `docs/contracts/mvp-scope.md` §3
> 互为引用；冲突时以 contracts 为准并修订本文。

## 问题

多个飞书会话（以及潜在的其他 kap 客户端）同时操作同一批 session 时，
KITE 应该提供什么样的并发语义？FOCUS 的答案是"多订阅广播 + interaction
owner 独占写入 + 跨实例租约"，是否原样移植？

## 决定：不移植 owner 租约，采用上游原生语义

kap-server 的原生语义（证据：`docs/research/kap-server-usability.md` §4):

- 多 WS 客户端订阅同一 session：原生支持，广播收敛。
- 并发 prompt:per-agent FIFO 排队，不拒绝；无 busy 错误。
- 没有任何 ownership/lease 概念；官方 web UI 多标签页就是平等并发。

KITE 顺势采用：

1. **写入不独占**:attached chat 都可发 prompt，全部入服务端 FIFO;
   卡片展示 active prompt 与队列长度。
2. **交互按 prompt 归属路由**：审批/表单只发给发起该 prompt 的 chat;
   其他 chat 得到只读提示。这条规则与 FOCUS 群聊合同"普通成员只能处理
   自己发起的 turn"一脉相承，但实现从"租约"简化为"路由"。
3. **abort 权限**：仅 prompt 发起者与管理员（2026-07-21 已决，进 MVP)。
4. **web UI 是平等客户端**（2026-07-21 核查）：kimi-web 是纯 /api/v1
   客户端，无特权通道、无独占假设（证据：
   `docs/research/kap-server-usability.md` 补充核查）。本地 web UI 与飞书桥
   天然共享同一批 session：排队由服务端 FIFO 保证，审批幂等（40902）使
   双端同时处理安全收敛。KITE 不拦截、不感知 web UI 的写入；两者的事件
   广播各自收敛。

## 为什么不实现 interaction owner

- **上游没有抓手。** owner 语义要求"拒绝非 owner 的写入"，而 kap 的 prompt
  端点对任何 bearer 持有者开放；桥内实现独占 = 在每个写路径上加自制闸门，
  还要处理 owner 失联、租约过期、跨重启恢复——FOCUS 为这套机制付出的
  复杂度有文档可查（五状态轴、两层准入、租约 store)。
- **产品价值未被证明。** FOCUS 需要 owner，是因为 codex app-server 的
  交互请求只有一个出口，必须有确定路由；kap 把 approval/question 做成了
  可查询、幂等响应的 REST 资源，路由问题简化为"按 prompt 归属定向"。
- **克制原则。** 先跑简单语义，让真实使用暴露是否真需要独占；需要时
  再按 FOCUS 设计加回（概念已预留，见 `kite-design.md` §4)。

## 风险与边界

| 风险 | 处置 |
| --- | --- |
| 跨进程 session 撕裂（kap live 时 TUI/`kimi web` 并发写同一 session) | 单写者前提，现按实例各自成立：各实例 kap home 相互隔离（见 `docs/decisions/multi-instance.md`),"裸 kimi 不在合同内"条款仍然适用（见 `process-shape-and-language.md`)；不做 loaded gate |
| 多实例（多飞书应用） | 已定案（2026-07-26,`docs/decisions/multi-instance.md`)：命名实例完全隔离（配置/数据/kap home/守护租约），本文语义按实例各自成立；跨实例协调仍不在范围内 |
| 队列被长 prompt 堵住，用户体验差 | 卡片显式展示队列深度；`/abort`（发起者/管理员）提供出口；不自动超时取消 |
| prompt 归属跨重启丢失 | 经 `GET .../prompts` + snapshot 尽力重建；建不回的审批卡显式过期（fail-closed，见 mvp-scope §4.6) |
| loopback 下 `POST /api/v1/shutdown` 对任何 token 持有者开放 | KITE 仅允许 service stop 路径调用；managed 形态负责意外关停后的拉起 |

## 何时回来重议

出现以下任一信号时，重开本文档：

1. 真实用户反馈"别人乱动我的 session"类问题；
2. 跨实例协调（实例间共享 session/状态）需求进入路线图；
3. 上游引入 ownership/锁原语（届时优先用上游的，不自建）。
