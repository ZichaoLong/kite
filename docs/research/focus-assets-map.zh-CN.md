# FOCUS 资产地图（Phase 2 借鉴盘点）

> 类型：research（证据材料，非合同）。盘点日期：2026-07-23。
> 对象：对 `/home/zlong/llm/focus` 的三路盘点（生命周期/单一事实源、
> 事件管线鲁棒性、群聊/图片/身份），对照 KITE 已准入的四个 Phase 2 功能
> （volatile 流式卡、图片进出、群聊、多用户允许名单）与现有 MVP 代码。
> 目的：记录什么值得借鉴、为什么，供后续重构判断保留/拆分/简化/删除。

## 0. 对现有 MVP 的横切发现

盘点在*当前*代码里发现了与 Phase 2 无关的真实缺口：

1. **kitectl 绕过 daemon**(`kitectl prompt send` 直连 kap REST):daemon 的
   prompt 归属表永远学不到归属，CLI 发起的 prompt 触发审批时只能走
   fail-closed"归属未知 → 过期卡"路径。FOCUS 的做法：loopback 控制面把
   CLI 变更路由进活服务。→ `docs/decisions/control-plane.md`。
2. **终态文本无 reconcile**:`turn.ended` 时只取一次；"turn 结束先于最终
   消息落盘"的常见竞态会产出空终态卡。FOCUS 重试 snapshot 读取并以之为
   权威。→ `event_pipeline` 加固（见 §3)。
3. **无 outcome-unknown 错误分类**:kitectl 把"连不上"与"已发出但超时"
   混为一谈；对非幂等提交盲目重试会重复入队。→ 并入控制面决策。
4. **无预检/原因码层**:`/detach`、`/new` 不检查在途工作；
   `kitectl service restart` 直接杀掉 kap-server（丢弃全部在途 prompt)
   且无警告。→ 原因码预检 + 重启预览，移植 `runtime_admin_controller.py`
   的纪律。
5. **状态变更 ad hoc**:`EventPipeline` 内部状态在 ~15 处就地修改。FOCUS
   的 reducer 消息 + UNSET 哨兵 + 冻结只读视图，正是其 persist-before-commit
   与恢复顺序得以证明的基础。Phase 2 引入流式/群聊状态时采用该纪律，
   不提前。
6. **store schema 无版本/迁移立场**(binding_store 无版本字段；cursor
   store 把损坏当空）。首次 schema 变更前应显式决定 fail-closed 还是迁移。

## 1. 资产地图（按处置分组）

### PORT（改名为主，基本原样）

| FOCUS 模块 | KITE 得到什么 | 落点 |
| --- | --- | --- |
| `service_control_plane.py` (215) | loopback JSON-lines 控制面 + outcome-unknown 错误分类 | kited ↔ kitectl（见 `docs/decisions/control-plane.md`) |
| `runtime_card_publisher.py`（调度器部分） | latest-wins 合并 patch 队列：每卡 ≤1 在途 patch、恰好一次尾随冲刷、遵守 retry-after；把飞书 RTT 移出 RuntimeLoop | 流式卡（见 `docs/contracts/streaming-cards.md`) |
| `execution_transcript.py` | 每 prompt 转写：delta 累积 + 权威全文 reconcile + 只增不减守卫 + 预算化投影 | 流式卡 |
| `thread_image_delivery.py` (108) | 上传一次/扇出到各 attached chat，单 chat 失败隔离 | 图片出站（见 `docs/contracts/images.md`) |
| `pending_attachment_store.py` (173) | 带 TTL 的待消费附件 store,`take` 原子取走 | 图片入站 |
| `thread_subscription_registry.py` (55) | 首个/末个订阅者边沿检测，驱动上游 WS 订阅/退订 | 适配层（群聊广播使多 chat 共享 session 成为常态时） |

### REWORK（移植纪律，按 kap/KITE 语义重写）

| FOCUS 模块 | 要移植的纪律 | 落点 |
| --- | --- | --- |
| `execution_recovery_controller.py` | 终态 reconcile：空读重试、snapshot 权威、投递去重；watchdog 代数计数器；降级分级 | `event_pipeline` 终态路径（加固） |
| `runtime_admin_controller.py` | `ReasonedCheck` 原因码预检；破坏性操作预览（不可验证 ⇒ 仅 force，永不 available) | `/detach` `/new` 预检；`kitectl service restart` 预览（加固） |
| `binding_runtime_manager.py` | 多 binding 变更的 persist-before-commit + 回滚 | binding 写路径（群聊使批量变更成真之前） |
| `runtime_state.py` / `runtime_view.py` | reducer 消息 + UNSET 哨兵 + 冻结只读视图 | Phase 2 状态增长时采用（流式/群聊） |
| `prompt_turn_entry_controller.py` | 先卡后提交的 fail-closed 次序；启动失败染红卡片；用户戳"卡死"执行时触发 watchdog reconcile | `app_handler` / 管线（加固） |
| `interaction_request_controller.py` | pending→processing 点击守卫（杜绝重复提交）;fail-close 清扫入口（解绑/关停时清扫待决审批） | `event_pipeline` 审批切片（加固） |
| `adapter_notification_controller.py` | 每事件目标匹配；delta→权威 reconcile；心跳驱动 watchdog；终态次序陷阱 | 流式消费（见 `docs/contracts/streaming-cards.md`) |
| `inbound_surface_controller.py` | 路由表 + 群守卫分类（`group_admin` / `request_actor_or_admin`)，点击时校验操作者 | 群聊（见 `docs/contracts/group-chat.md`) |
| `codex_group_domain.py` | 激活命令 + 点击时管理员校验 | 群聊 |
| `stores/group_chat_store.py`（配置半） | 每群配置 `{mode, activated, activated_by, activated_at}` | 群聊（日志/边界半随 assistant 模式推迟） |
| `file_message_domain.py` | 暂存进 session cwd 的管线：类型校验、文件名纪律、TTL + 惰性清扫、cwd 不匹配阻断、消费一次可回滚 | 图片入站（见 `docs/contracts/images.md`) |
| `group_history_recovery.py` | 边界三元组去重（seq + created_at + message_ids)、过滤自身消息、失败即阻断的历史拉取 | 随 assistant 模式推迟 |
| `generated_image_delivery.py`(+store) | 事件驱动出站投递的 claim→deliver→commit 幂等 | 备用；扫描半为 codex 特有（文生图是 KITE 永久非目标） |

### SKIP（附理由）

| FOCUS 模块 | 理由 |
| --- | --- |
| `thread_access_policy.py`、`thread_runtime_coordination.py` | interaction-owner 租约——与 KITE 已决并发模型矛盾（`docs/decisions/concurrency-model.md`)；若将来准入可作为模板 |
| `instance_resolution.py`、`instance_layout.py`、`legacy_migration.py` | 多实例 = Phase 3；无旧安装可迁移 |
| `thread_resolution.py` | KITE 的 `/sessions` + `/switch` 已覆盖 |
| `permissions_profile.py`、`approval_policy.py` | codex 枚举；KITE 的 `/mode` `/plan` 已交付 |
| `forward_aggregator.py` | 合并转发未准入；2s 聚合窗口技巧留档备群聊时用 |
| `card_text_projection.py` | 终态卡标记 + 反投影与 `/last` 一起到来时再移植 |

## 2. 图片合同用到的上游已验证事实

kap 的 prompt `content` 是带 `image` / `video` / `file` 的可辨别联合
（`packages/protocol/src/message.ts:70-78`)，入站图片可原生提交；暂存
路径引用作为组合上下文与超大/不支持文件的兜底。

## 3. 各项发现的落点

- `docs/decisions/control-plane.md` —— 发现 1 + 3（双写者、outcome-unknown)。
- 现有管线加固批 —— 发现 2、4 与 §1 的点击守卫/清扫部分（终态
  reconcile、原因码预检、重启预览）。
- `docs/contracts/streaming-cards.md` —— 流式机制（§1 PORT 行 + 清单）。
- `docs/contracts/images.md` —— 进出管线 + 新附件暂存轴。
- `docs/contracts/group-chat.md` —— 群配置轴、点击校验、允许名单自然落地。
- `docs/architecture/kite-design.md` §4 —— 按承载力门槛先行登记两条新
  状态轴（group config、attachment staging)。
