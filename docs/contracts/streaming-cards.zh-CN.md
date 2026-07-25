# 合同：volatile 流式卡（Phase 2)

> 状态：已准入（2026-07-23，通过下方承载力门槛）；随实现转 active。
> 激活后代码行为与本文不一致即 contract gap。
> 证据：`docs/research/focus-assets-map.md`(FOCUS 流式机制）、
> `docs/architecture/kite-design.md` §5（已预留 volatile 策略）。

## 1. 功能承载力门槛

1. **归哪一层？** 适配层（归一 `assistant.delta` 与 offset 水位）→
   应用层（每 prompt 转写 + patch 调度）→ 传输层边沿的合并 patch 调度器
   （飞书 RTT 绝不阻塞 RuntimeLoop)。
2. **动哪条状态轴？** 不新增。流式状态是每 prompt 的内存转写，与 prompt
   归属一样在重启/resync 后由 REST snapshot 重建（§4.6)；执行卡锚点已有
   `{chat_id, session_id, prompt_id, card_message_id}`。
3. **崩溃/重启怎么恢复？** durable 事件仍是唯一权威驱动：offset 缺口、
   resync、重启一律落入既有 snapshot 重建路径；终态卡永远由 durable
   信号产生。volatile 文本是增强，永远不是证据。
4. **用什么测试锁？** 见 §5。

## 2. 范围

包含：`assistant.delta` 逐字流入当前执行卡；每个 attached chat 自己的
卡片各自更新（每锚点一扇出）。

不含（明确非目标）:`thinking.delta`、`tool.call.delta`、`shell.*`、
`agent.status.updated`；向审批/question 卡流式写入；任何以 volatile 为
依据进入 durable 决策的状态。

## 3. 行为合同

1. **全快照 patch 不变量**：每次 patch 都从累积转写整体重渲卡片。
   patch 永远不是 diff——丢失或被合并的 patch 不丢内容。
2. **合并**：每卡 latest-wins——任一时刻每卡最多一个在途 patch，恰好
   一次尾随冲刷；delta 洪流收敛为每卡每阵 ~2 次 patch。
3. **节流**：每卡最小 patch 间隔（默认 700 ms)+ 单个尾随定时器；最终
   状态绝不丢弃。
4. **patch 失败**：可重试（飞书 230020 限流、传输超时）→ `retry_after`
   （默认 2s）后重排，新渲染优先；不可重试 → 丢弃（由不变量 1 保证
   安全）。终态卡 patch 失败 → 一次性纯文本内容兜底。
5. **delta 愈合**:delta 携带累计 offset;offset 缺口直接落入 snapshot
   重建（不猜缺失文本）。回合完成的权威文本永远 reconcile 覆盖 delta
   累积文本，单调——只增不减。
6. **Markdown**：只在渲染时、对完整累积文本做清洗（delta 可能劈开
   token)。执行卡用 runtime 变体（容忍未闭合围栏）；终态卡用 json2
   变体（围栏规整化）。
7. **尺寸上限**：回复投影按字符预算并带截断提示；终态卡按 utf-8 字节
   预算，超预算 → 纯文本兜底（移植 FOCUS 终态预算纪律）。
8. **定时器卫生**：关停与终态转换取消尾随/重试定时器；过期定时器在
   其 prompt 结束后触发是 no-op（代数守卫）。
9. **目标匹配**:prompt 级 delta 只能改锚点 prompt_id 匹配的卡片（与
   durable 事件同规则）。

## 4. fail-closed 清单

1. offset 缺口 / volatile 溢出 → 按 `resync_required` 同路径 snapshot
   重建卡片内容。
2. 重建失败 → 卡片定格"状态未知"并附 `kitectl session status` 排查提示
   （沿用 MVP §4.2)。
3. 流式被禁用或 kap 停发 delta → durable 路径仍产出正确卡片（流畅度
   降级，正确性永不降级）。

## 5. 锁定行为的测试

- 合并：N 次快速提交 → 每卡 ≤1 在途、恰好一次尾随、次序保持；在途期间
  新提交替换待渲染。
- 节流：空闲立即 patch，间隔内仅一个尾随定时器；关停/终态取消定时器。
- 重试：230020/超时 → 重排并最终应用；新渲染取代；不可重试丢弃不崩。
- 缺口：offset 跳变 → 恰好一次重建；重建失败 → 定格"状态未知"。
- reconcile：权威文本覆盖 delta；更短的陈旧文本永不缩短现有内容。
- 渲染：流式中未闭合围栏容忍渲染；终态卡围栏规整；超预算终态 → 纯文本。
- 扇出：两个 attached chat 各自收到自己的流式卡。

## 6. 与既有合同的关系

- mvp-scope §3 的并发展示不变（队列长度仍来自 durable 状态）。
- kite-design §5 的 durable 事件列表仍是状态转换的唯一驱动；本合同为
  展示层增加 volatile 旁路——正是该节已登记的 "volatile later" 条款。
- 卡死兜底：FOCUS 的镜像看门狗 / 卡死"执行中" reconcile 是有意延后的
  hardening 项（已在 focus-assets-map 登记）。落地前，卡在"执行中"的
  卡片通过 `kitectl session status` 排查路径兜底恢复。
