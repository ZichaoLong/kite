# Docs Index

This directory is the source of truth for repository architecture, runtime
boundaries, and feature contracts.

## Reading Rule

When code and docs disagree, treat that as a contract gap. Tighten the code,
the docs, or both.

## 当前状态

M1(MVP）已完成并经实网验证（2026-07-22);daemon 已部署（systemd
--user + autostart)。第 0 里程碑（spike 验证）已于 2026-07-21 对
**kimi 0.28.1** 全部通过（结果见 `docs/verification/spike-results.md`)。
contracts/、architecture/、decisions/ 为 **active** repository
facts——与代码不一致即 contract gap。Phase 2 已准入：volatile 流式卡、
图片进出、群聊（见各 Phase 2 合同）；实现从 FOCUS 资产盘点导出的加固批
开始（`docs/research/focus-assets-map.md`)。

## Document Types

- `docs/contracts/`
  - normative feature and runtime behavior contracts
- `docs/architecture/`
  - current architecture, layering, module split, and implementation shape
- `docs/decisions/`
  - decision records that explain why a design boundary exists
- `docs/verification/`
  - spike 验证清单与手测清单；validation support, not product/runtime semantics
- `docs/research/`
  - 上游与对标项目的调查报告；作为 decisions 的证据材料，本身不是合同
- `docs/_work/`
  - local working notes; not repository facts

Status guidance:

- treat `contracts/`, `architecture/`, and `decisions/` as active repository
  facts
- treat `verification/` as validation support
- treat `research/` as evidence; 若其中事实与上游最新代码冲突，以上游代码为准
- treat local notes under `docs/_work/` as working material

## Read By Type

### User-Facing Entry

- [README.md](../README.md)

### Contracts

- [`mvp-scope.md`](./contracts/mvp-scope.zh-CN.md)(active)
  - MVP 功能范围、命令面、审批行为、fail-closed 清单、non-goals、功能承载力门槛
- [`streaming-cards.md`](./contracts/streaming-cards.zh-CN.md)（已准入，Phase 2)
  - 执行卡 volatile 流式：合并/节流/重试纪律、缺口→重建、fail-closed 清单
- [`images.md`](./contracts/images.zh-CN.md)（已准入，Phase 2)
  - 图片入站（暂存、TTL）与出站（上传+扇出）管线；附件暂存状态轴
- [`group-chat.md`](./contracts/group-chat.zh-CN.md)（已准入，Phase 2)
  - mention_only 群：激活、入站矩阵、点击校验审批、允许名单落地；群配置状态轴
- [`scheduled-prompts.md`](./contracts/scheduled-prompts.zh-CN.md)（已准入，Phase 3)
  - 定时 prompts:systemd --user 定时器经 `kitectl prompt send` 回到控制面;display 模式；终止策略

### Architecture

- [`kite-design.md`](./architecture/kite-design.zh-CN.md)(active)
  - 总体架构、进程形态、分层、适配层边界、状态轴、事件消费策略、卡片模型、持久化、服务管理

### Decisions

- [`backend-selection.md`](./decisions/backend-selection.zh-CN.md)
  - 为什么选 kap-server 作为共享后端；两条集成路径的取舍；上游漂移对策
- [`process-shape-and-language.md`](./decisions/process-shape-and-language.zh-CN.md)
  - 为什么 Python + managed 子进程；为什么暂缓本地 TUI wrapper；裸 kimi 的立场
- [`concurrency-model.md`](./decisions/concurrency-model.zh-CN.md)
  - 为什么采用服务端队列语义 + prompt 级归属；为什么不实现 interaction owner；跨进程撕裂风险与单实例前提
- [`control-plane.md`](./decisions/control-plane.zh-CN.md)
  - 为什么 kitectl 经 loopback 控制面变更 daemon 状态；它修复的双写者 bug;outcome-unknown 错误分类

### Verification

- [`spike-checklist.md`](./verification/spike-checklist.zh-CN.md)
  - 第 0 里程碑：业务代码开工前必须通过的上游实测清单
- [`spike-results.md`](./verification/spike-results.zh-CN.md)
  - 第 0 里程碑结果（2026-07-21 通过，kimi 0.28.1)：各项实测观察、上游漂移修正、已折入合同/架构的设计调整

### Research

- [`kap-server-usability.md`](./research/kap-server-usability.zh-CN.md)
  - kap-server 可用性调查（生命周期、认证、API 覆盖、事件模型、并发语义、成熟度、缺口）
- [`okbot-vs-focus.md`](./research/okbot-vs-focus.zh-CN.md)
  - 对标项目 OKbot 与 FOCUS 的对比；OKbot 路线的教训
- [`focus-assets-map.md`](./research/focus-assets-map.zh-CN.md)
  - 从 FOCUS 借鉴什么、为什么（生命周期、鲁棒性、单一事实源资产），映射到已准入的 Phase 2 功能

## Read By Question

| Question | Read |
| --- | --- |
| KITE 的整体架构与进程形态是什么？ | [`kite-design.md`](./architecture/kite-design.zh-CN.md) |
| 为什么桥 kap-server 而不是 ACP / node-sdk / wire？ | [`backend-selection.md`](./decisions/backend-selection.zh-CN.md), [`kap-server-usability.md`](./research/kap-server-usability.zh-CN.md) |
| 为什么不用 TypeScript embed kap-server？为什么复用 FOCUS 的 Python 资产？ | [`process-shape-and-language.md`](./decisions/process-shape-and-language.zh-CN.md) |
| 为什么没有 `kite` 本地 TUI wrapper（对应 FOCUS 的 fcodex）? | [`process-shape-and-language.md`](./decisions/process-shape-and-language.zh-CN.md) |
| 多个飞书会话同时操作同一 session 时行为是什么？谁在独占写入？ | [`concurrency-model.md`](./decisions/concurrency-model.zh-CN.md) |
| MVP 做什么、不做什么？新功能怎么获准进入？ | [`mvp-scope.md`](./contracts/mvp-scope.zh-CN.md) |
| 开工前要实测验证上游的哪些行为？ | [`spike-checklist.md`](./verification/spike-checklist.zh-CN.md) |
| spike 实测观察到什么、修正了哪些文档事实？ | [`spike-results.md`](./verification/spike-results.zh-CN.md) |
| 哪些 Phase 2 功能已准入、各在什么合同下？ | [`streaming-cards.md`](./contracts/streaming-cards.zh-CN.md), [`images.md`](./contracts/images.zh-CN.md), [`group-chat.md`](./contracts/group-chat.zh-CN.md) |
| 为什么 kitectl 要经 loopback 控制面做变更？ | [`control-plane.md`](./decisions/control-plane.zh-CN.md), [`focus-assets-map.md`](./research/focus-assets-map.zh-CN.md) |
| 为什么不做 memory / ASR / 设备控制？ | [`okbot-vs-focus.md`](./research/okbot-vs-focus.zh-CN.md), [`mvp-scope.md`](./contracts/mvp-scope.zh-CN.md) |

## Language

中英双语强制（2026-07-21 已决）：英文为规范版（`<name>.md`)，中文为
`<name>.zh-CN.md`，两版成对、内容同步；由 `scripts/check-docs.sh` 在 CI
强制（沿用 FOCUS 惯例）。README 与 AGENTS.md 参照 FOCUS 做法：README
保持中文单语，AGENTS.md 配 zh-CN 译本。
