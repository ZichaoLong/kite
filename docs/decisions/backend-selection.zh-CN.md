# 决策：选 kap-server 作为共享后端

> 状态：已决（开工前以 spike 复核，见 `docs/verification/spike-checklist.md`)。
> 证据：`docs/research/kap-server-usability.md`。

## 问题

KITE 需要一个长驻的、多客户端可共享的 kimi-code 运行时后端，支撑飞书侧
"多会话操作同一批 session"的形态。kimi-code 提供了多种程序化接入面，
选哪一个作为桥的基石？

## 候选与排除

| 候选 | 结论 | 理由 |
| --- | --- | --- |
| `kimi -p`(headless print) | 排除 | 单次执行，无会话共享、无审批交互、无事件订阅 |
| stdio wire 协议 | 排除 | kimi-code 已显式移除（"held back")，当前不存在 |
| `kimi acp`(ACP 协议） | 排除 | 面向编辑器集成；审批/终端语义按 IDE 场景设计，且构建在 node-sdk 之上，能力面是 kap-server 的子集 |
| `@moonshot-ai/kimi-code-sdk`(node-sdk，进程内） | 排除（形态层面） | 能力等价但要求 KITE 与引擎同进程同语言（TS)；语言结论见 `process-shape-and-language.md` |
| `packages/klient` ipc 传输 | 排除 | 仓内无生产使用者，kap-server 不暴露 ipc，外部无处可挂 |
| **kap-server**(REST + WS) | **选用** | 见下 |

## 理由

1. **能力闭环完整**:session CRUD（除 delete)、prompt FIFO、abort/steer、
   approval/question 的推送 + REST 响应、durable+volatile 双层事件、
   cursor 重放 + snapshot 重建——桥接所需全部原语齐备，且被自家 kimi-web
   真实使用（dogfooding)。
2. **事件模型与 IM 场景同构**:durable journal + cursor resync + snapshot
   就是 IM 多端同步模型；FOCUS 需要在 app-server 之上自建的部分同步纪律，
   kap-server 原生提供。
3. **per-prompt override 原生支持**:`model`/`permission_mode`/`plan_mode`
   随 prompt 携带，直接落实"binding 级设置每轮显式应用"的合同，无需
   FOCUS 那样的 one-shot override 迂回。
4. **契约单一来源**:OpenAPI/AsyncAPI 从 zod schema 代码生成，上游自己有
   API 面快照回归测试——这个面是上游认账的，适合作为外部依赖。
5. **认证与发现现成**:token 文件热加载、端口自动 +1、实例注册表，正好
   被 KITE 的 managed 子进程形态接管。

## 已知代价与对策

| 代价 | 对策 |
| --- | --- |
| 0.0.2，无稳定性承诺，API 面两周内出现过 `/api/v2` 即删 | 不硬钉版本（2026-07-21 对齐，见 kite-design §10)；CI 跑 `/openapi.json` + WS 操作目录快照 diff 感知漂移，显式适配 |
| 无 ownership/单写者语义 | KITE 采用队列语义 + prompt 级归属（见 `concurrency-model.md`)，不向上游要求独占 |
| 跨进程无 session 锁 | 单实例部署前提 + "裸 kimi 不在合同内"条款（见 `process-shape-and-language.md`) |
| 无 daemon 模式 | KITE 作为父进程 managed 看管 |
| 无 session delete | Non-goal，不绕过 |
| volatile 事件不可靠、WS 无心跳 | MVP 只消费 durable；stale 检测客户端自检 |

## 备选保留

若 spike 暴露 kap-server 的硬缺陷，退路是：KITE 进程内嵌一层薄 TS sidecar
（调 `startServer()` 或 node-sdk)，Python 侧仍走同一套 REST/WS 词汇——
适配层边界不变，损失可控。
