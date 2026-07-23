# 合同：群聊（Phase 2)

> 状态：已准入（2026-07-23，通过下方承载力门槛）；随实现转 active。
> 证据：`docs/research/focus-assets-map.md`(FOCUS 群域盘点）、
> `docs/decisions/concurrency-model.md`（队列语义 + prompt 级归属）、
> `docs/contracts/mvp-scope.md` §3（多 chat 广播规则）。

## 1. 功能承载力门槛

1. **归哪一层？** 应用层（入站分类、命令守卫、卡片点击时的操作者校验）
   + 本地状态层（一个新 store 存群配置）。传输层已归一会话类型与
   @提及。
2. **动哪条状态轴？** 新增一条，开工前已在 kite-design §4 登记：
   **群配置**——持久化、以 chat 为键 `{activated, activated_by,
   activated_at, mode}`。其余全部搭乘既有轴：群只是一个普通 `chat_id`
   binding（轴 1)——FOCUS 需要的共享 binding hack 在 KITE 的 chat 键
   store 下天然免费——以及 prompt 归属（轴 4）扩展 `sender_open_id`。
3. **崩溃/重启怎么恢复？** 群配置在 store 中（与 binding 同样加载）;
   @检测无状态；prompt 归属按既有 §4.6 路径重建（建不回的审批显式
   过期，不变）。
4. **用什么测试锁？** 见 §5。

## 2. 范围（第一刀）

包含：**仅 `mention_only` 群**。管理员在群内 `/group activate` 激活
  一次，此后**任何成员** @bot + 文字即可发起 prompt。群内斜杠命令仍
  仅管理员可用。未激活群或陌生人内容一律静默忽略（仅在 @/斜杠时给
  一次拒绝提示，不刷屏）。

不含（本刀明确非目标）:`assistant` 模式（每群日志 + 历史上下文——需
  日志/边界轴，推迟）、`all` 模式（每条消息都触发——会灌爆 FIFO 且需
  排他规则）、merge_forward、超出操作者规则的按成员 ACL、经 kitectl
  建群/管群。

## 3. 行为合同

1. **激活**:`/group activate|deactivate`（仅管理员，群内）写配置;
   `/status` 展示。激活要求群已绑定（未绑定群的首次激活同时创建并
   绑定 session，与单聊首次使用同规则）。
2. **入站矩阵**：已激活群内，仅成员的 @bot+文字 进入 prompt 路径；非
   @消息完全忽略（不记日志、不留上下文）。未激活群内，除管理员斜杠
   命令外全部忽略。单聊行为不变（暂仍仅管理员）。
3. **群内审批/question**：卡片发到群聊（按 mvp-scope §3 广播）；点击
   处理者校验 `点击者 open_id == 发起者 open_id || 管理员`——旁观者
   点击得到拒绝 toast，状态不变。发起者 `sender_open_id` 随 prompt
   归属记录（轴 4，不新增轴）。
4. **群内 /abort**：发起者或管理员，与单聊同规则——同一操作者校验。
5. **广播**：普通输出（执行卡/终态卡）像任何 attached chat 一样发到
   群聊；群与单聊共享同一 session 时，既有多 chat 规则不变。
6. **多用户身份（允许名单）**：随激活自然落地——群成员名单（由飞书
   维护）即用户名单；本刀不引入独立单聊允许名单（FOCUS 的证据：群
   已覆盖需求；单聊暂仍仅管理员）。

## 4. fail-closed 清单

1. 未激活群的成员消息 → 忽略（仅 @/斜杠时一次拒绝提示）；绝不发起
   prompt。
2. 旁观者点击审批/question → 拒绝 toast，状态不变，该点击不产生上游
   响应（卡片为操作者保留）。
3. 群配置 store 损坏 → 该群按未激活处理（fail 向静默，永不 fail 向
   开放）。
4. 事件缺少发送者身份 → 按非成员处理。

## 5. 锁定行为的测试

- 入站矩阵：单聊/群 × 管理员/激活成员/陌生人 × @/非@ × 斜杠/文字
  （每格有显式结果）。
- 激活：仅管理员；重启后保留；deactivate 立即停止全部成员 prompt。
- 未绑定群首次激活即创建+绑定（cwd = `default_working_dir`)。
- 审批卡：发起者点击决议；管理员点击决议；旁观者点击 → toast，卡片
  不动，无 REST 调用。
- `/abort`：群内仅发起者/管理员。
- 广播：群 + 单聊绑定同一 session 均收到卡片；审批只去发起者所在
  chat（既有 §3 规则）。
- 配置损坏 → 按未激活处理。

## 6. 推迟项与指引

- `assistant`/`all` 模式：需日志/边界轴；`all` 还需排他规则。FOCUS 的
  `group_chat_store` 日志半与 `group_history_recovery.py` 是现成设计
  （资产地图 §1)。
- 合并转发聚合：`forward_aggregator.py`(2s 窗口）已留档。
- 群内富 question 表单：同操作者规则，无需新合同。
