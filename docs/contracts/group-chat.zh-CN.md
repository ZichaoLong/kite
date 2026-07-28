# 合同：群聊（Phase 2)

> 状态：已准入（2026-07-23，通过下方承载力门槛）；随实现转 active。
> 证据：`docs/research/focus-assets-map.md`(FOCUS 群域盘点）、
> `docs/decisions/concurrency-model.md`（队列语义 + prompt 级归属）、
> `docs/contracts/mvp-scope.md` §3（多 chat 广播规则）。

## 1. 功能承载力门槛

1. **归哪一层？** 应用层（入站分类、命令守卫、卡片点击时的操作者校验）
   + 本地状态层（一个新 store 存群配置）。传输层已归一会话类型与
   @提及。
2. **动哪条状态轴？** 新增两条，开工前已在 kite-design §4 登记：
   **群配置**——持久化、以 chat 为键 `{activated, activated_by,
   activated_at, mode}`；以及 **assistant 日志**——每群 JSONL 消息日志
   （单调 `seq`）加触发**边界三元组** `{seq, created_at, message_ids}`
   （时间戳 alone 不是 cursor：同毫秒可有多条消息）。其余全部搭乘既
   有轴：群只是一个普通 `chat_id` binding（轴 1)，以及 prompt 归属
   （轴 4）扩展 `sender_open_id`。
3. **崩溃/重启怎么恢复？** 群配置与日志/边界都在 store 中（与 binding
   同样加载）;@检测无状态；prompt 归属按既有 §4.6 路径重建。重启后
   日志覆盖近期历史，边界三元组与飞书 REST 历史回填精确去重（FOCUS
   久经崩溃考验的设计）。
4. **用什么测试锁？** 见 §5。

## 2. 范围

包含：**`mention_only`、`assistant`、`all` 三种群模式**。管理员在群内
  `/group activate` 激活一次；模式经
  `/group-mode 〈mention_only|assistant|all〉` 切换（默认 `mention_only`)。

- `mention_only`：仅成员的 @bot+文字 触发；其余全部忽略（不记日志、
  不留上下文）。
- `assistant`：成员的每条消息写入每群日志；@bot+文字 触发时，将上次
  触发边界以来的日志作为上下文注入。历史拉取失败则阻断该 prompt 并
  显式提示（fail-closed——绝不静默地不带上下文作答）。
- `all`：成员的每条消息直接触发 prompt（不注入上下文）。**排他规则**:
  all 模式群的 session 不得绑定到任何其他 chat（防跨会话噪声）；当
  session 已被共享时，切到 `all` 以整改文案拒绝；`all` 模式下
  `/switch`/`/new` 换入共享 session 同样拒绝（FOCUS 的会话访问规则）。

群内斜杠命令仍仅管理员可用。未激活群或陌生人内容一律静默忽略（仅在
@/斜杠时给一次拒绝提示，不刷屏）。

不含（明确非目标）：群内 merge_forward、超出操作者规则的按成员 ACL、
经 kitectl 建群/管群。

## 3. 行为合同

1. **激活**:`/group activate|deactivate`（仅管理员，群内）写配置;
   `/status` 展示。激活要求群已绑定（未绑定群的首次激活同时创建并
   绑定 session，与单聊首次使用同规则）。
2. **入站矩阵**：已激活群内，仅成员的 @bot+文字 进入 prompt 路径;
   `mention_only` 模式下非 @消息完全忽略（不记日志、不留上下文）;
   `assistant` 模式下非 @的成员消息写入每群日志（机器人自己的消息与
   非成员消息绝不入日志）。未激活群内，除管理员斜杠命令外全部忽略。
   单聊行为不变（暂仍仅管理员）。
3. **assistant 上下文组合**:assistant 模式触发时，prompt 组合为
   `<group_chat_scope>/<group_chat_context>/<group_chat_current_turn>`——
   边界以来的日志（与飞书 REST 历史回填合并、按边界三元组去重、过滤
   机器人自身消息）加当前消息；封套文案告诉模型回答当前消息而不是
   复述历史。上限：50 条 / 24h 回看（两者可配：
   `group_history_fetch_limit` / `group_history_fetch_lookback_seconds`)/
   边界 5s 宽限（固定常量，未接配置）；历史拉取
   失败阻断该 prompt 并显式提示（fail-closed)。
4. **群内审批/question**：卡片发到群聊（按 mvp-scope §3 广播）；点击
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
5. assistant 模式历史拉取失败 → 阻断该 prompt 并显式提示；绝不静默
   地不带上下文作答。
6. all 模式排他违规（群的 session 已共享或将被共享到其他 chat)→ 模式
   切换/改绑以整改文案拒绝；绝不静默放行。

## 5. 锁定行为的测试

- 入站矩阵：单聊/群 × 管理员/激活成员/陌生人 × @/非@ × 斜杠/文字 ×
  模式（mention_only/assistant)（每格有显式结果）。
- 激活：仅管理员；重启后保留；deactivate 立即停止全部成员 prompt。
- 模式切换：`/group-mode` 仅管理员；assistant → 每条成员消息入日志
  （机器人自身除外）;mention_only → 不写日志；all → 每条成员消息直接
  触发 prompt。
- all 排他：session 被其他 chat 共享时切 `all` 拒绝（整改文案）;`all`
  模式下 `/switch`/`/new` 换入共享 session 拒绝；独占时放行。
- assistant 上下文：日志/边界与 REST 回填合并、边界三元组去重（同毫
  秒消息）、自身消息过滤、封套结构、上限（50/24h/5s)、拉取失败阻断
  并提示。
- 日志轴：JSONL 单调 seq 追加、边界读写、损坏按未激活/空日志处理
  (fail-closed)、重启重载。
- 未绑定群首次激活即创建+绑定（cwd = `default_working_dir`)。
- 审批卡：发起者点击决议；管理员点击决议；旁观者点击 → toast，卡片
  不动，无 REST 调用。
- `/abort`：群内仅发起者/管理员。
- 广播：群 + 单聊绑定同一 session 均收到卡片；审批只去发起者所在
  chat（既有 §3 规则）。
- 配置损坏 → 按未激活处理。

## 6. 推迟项与指引

- 当前无推迟项。此前的推迟项均已准入：群内合并转发（§3.7)、all 模式
  反向排他（§3.8)、富 question 表单（§3.9)。

## 7. 后续准入（2026-07-25)

### 3.7 群内合并转发

按模式的触发语义（与 FOCUS 一致）:`mention_only` → 静默丢弃（转发不
携带 @);`assistant` → 写入群日志作上下文素材，不触发；`all` → 聚合
后按成员文字消息处理。2s 聚合窗口与递归展开与单聊路径共享。

认领-合并语义（同样与单聊共享，审查 M12)：暂存的转写会等满约 2s 窗
口，期间发送者的下一条纯文本将**认领**该暂存——两者合并为一条
prompt（先是 `<forwarded_messages>` 转写块，再是留言），指令绝不抢在
它所指的内容之前执行。认领按（发送者， chat）键控，不会跨成员、跨
chat 串扰。只有无人认领的窗口才把转写单独冲刷成一条 prompt。斜杠命
令永不认领；交互回复（审批/question 作答）按设计优先认领。群内的认
领先复查当前模式（与窗口冲刷的 fail-closed 复查镜像）：仅仍处于已
激活 all 模式的群才认领；窗口内切走后暂存留给冲刷，由冲刷显式丢弃。

### 3.8 all 模式反向排他

排他规则双向生效：任何其他 chat（单聊或群）改绑（`/switch`、首次绑
定）进 all 群已占用的 session，以同样的整改文案拒绝。

### 3.9 富 question 表单

`question.requested` 按 question 项渲染选项按钮卡（编号回复兜底，操作
者规则与审批一致）；作答或超时后 dismiss(patch）卡片。落实 mvp-scope
的 question 原始行。

## 补充对齐（2026-07-24)

1. **发送者显示名**：面向群的通知（审批/question 路由提示）经通讯录 API
   (`contact:user.base:readonly`）解析发起者显示名，走带 TTL 的
   read-through 缓存。回退链与 FOCUS 同构：`name` 或 `nickname` → 回退
   `open_id[:8]`（机器人发送者为 `机器人:{id[:8]}`);fail-soft，不占状态轴。
   测试：缓存命中/TTL/负缓存/回退链，以及有/无可解析名两种通知文案。

## 补充对齐（2026-07-25，审查 C2)

1. **机器人被移出群 / 群解散**：收到 chat 不可用生命周期事件时，该群的
   激活配置被停用（fail 向静默，与 §4.3 同一立场）；模式偏好、binding
   与群日志文件保留。之后重新拉机器人进群不会静默复活旧激活状态——
   需要管理员显式再次 `/group activate`。
2. **飞书话题（thread）回复并入主流上下文**（范围裁剪，审查 L16 后登记）:
   FOCUS 按话题 scope 建模、将话题回复排除在主流上下文之外；本刀每群
   只有一条边界三元组、无话题 scope，因此成员在话题中回复的消息与普通
   群消息一样进入 assistant 模式日志与 REST 历史回填。消息 wire 上带有
   `thread_id`，未来可以在客户端过滤；历史列表 API 本身不提供服务端
   话题过滤。
