# 合同：定时 prompts(Phase 3)

> 状态：已准入（2026-07-25，通过下方承载力门槛）；随实现转 active。
> 蓝本：FOCUS `docs/contracts/scheduled-prompts.md`（久经验证的形态：
> 不建内嵌调度子系统；systemd 定时器经本地 CLI 回到 daemon)。

## 1. 功能承载力门槛

1. **归哪一层？** 管理面（`kitectl schedule`)+ OS 定时器；执行经
   loopback 控制面进入 daemon(`prompt/submit`)，即应用层。daemon 不新增
   子系统。
2. **动哪条状态轴？** 不新增。定时 prompt 就是普通 prompt：归属经控制
   面记到绑定 chat（轴 4)，审批/question 行为与飞书发起的 prompt 完全
   一致。调度元数据存在 systemd unit 文件里，不进 KITE stores。
3. **崩溃/重启怎么恢复？** 定时器是 systemd 的事（跨 kited 重启与重启
   机保留，取决于 `Persistent=`)。已触发的 prompt 按 mvp-scope §4.6 走
   普通恢复路径。`kitectl` 除读取 unit 文件外不持有调度状态。
4. **用什么测试锁？** 见 §4。

## 2. 产品形态

明确**不**建内嵌调度子系统，**不**建持久任务队列。支持的形态是：
**在未来某个时刻，为既有绑定安全地合成一条新 prompt**，由同一个
KITE 实例经普通 prompt 路径执行。

- 触发路径：systemd --user 定时器 → service unit 执行
  `kitectl [--instance <名称>] prompt send --chat <chat_id> --text <text>`
  → loopback 控制面 → daemon 提交（归属记到该 chat，模式随绑定）。为
  命名实例创建的定时任务触发时携带 `--instance <名称>`，因此总是路由回
  创建它的实例（见 §3.1)。
- 冲突交给 kap 服务端 FIFO(session 忙则排队；不需要也不存在本地内存
  队列）。
- 平台边界：受管定时助手与 `service_manager` 同构分派——Linux
  `systemd --user` 定时器、macOS `launchd`(`StartCalendarInterval`)、
  Windows Task Scheduler 日历触发（2026-07-25 准入）。

## 3. `kitectl schedule` 命令面

- `kitectl schedule create --chat <id> --text <text> (--at <ISO 时间戳> | --cron <表达式>) [--display silent|announce]`
  - 在 `~/.config/systemd/user/` 写入 `kite-schedule-<hash>.timer` +
    `.service`（默认实例）或 `kite-schedule-<实例>-<hash>.timer` +
    `.service`（命名实例）并启用定时器；不为未来时刻手动触发（触发归
    systemd)。
  - `--at` 生成一次性定时器（`OnCalendar=<ts>`);`--cron` 生成周期定
    时器。过去的时间戳与无法解析的 cron 在写入前拒绝（fail-closed)。
  - 生成 unit 中的 kitectl 路径解析顺序：显式 `--ctl-path` >
    `KITE_BIN_DIR/kitectl` 或 `~/.local/bin/kitectl` > `<数据根>/.venv/bin/kitectl`。
    解析结果写入 unit。
- `kitectl schedule list` —— 列出当前实例的 `kite-schedule-*.timer`
  及其计划与下次触发时间（优先解析 `systemctl --user list-timers`，失
  败回退 unit 文件）。
- `kitectl schedule show <name>` —— 打印 timer + service 定义。
- `kitectl schedule remove <name>` —— 停用并删除 unit 对（仅 `--yes`
  免确认）。
- `kitectl schedule run-now <name>` —— 立即触发一次（经
  `systemctl --user start`)。

### 3.1 实例作用域（多实例）

OS 定时器存储在同一主机用户下是单一共享命名空间，因此定时任务按实例
划分命名空间（docs/decisions/multi-instance.md):

- 命名实例的 unit 名携带实例段：`kite-schedule-<实例>-<hash>`（默认实
  例保持 `kite-schedule-<hash>`)。hash 本身仍由 chat + 计划 + 文本决
  定，两个实例绝不冲突、也不会互相覆盖对方的定时器。
- 命名实例生成的 ExecStart 触发 `kitectl --instance <名称> prompt
  send ...`,prompt 提交回创建该定时任务的实例；默认实例省略该旗标
  （此后走 kitectl 常规解析阶梯）。
- `list`、`show`、`remove`、`run-now` 只看见、只接受当前实例的定时任
  务：裸 hash 在当前命名空间内解析；属于其他命名空间的完整名称一律
  fail-closed 拒绝。

## 4. 行为与安全合同

1. 定时任务只是"在未来时刻发起一条新 prompt"；不得绕过
   binding/attach/操作者准入（控制面对 CLI prompt 执行同一套规则）。
2. `display_mode`:`silent`（默认）直接提交；`announce` 由 daemon 在
   提交前向目标 chat 发一条简短的"定时触发"提示。无更复杂的编排。
3. 触发时目标绑定必须存在；不存在时由控制面既有的 `no_binding` 错误
   收口（不隐式创建绑定）。
4. 周期定时器必须带明确终止策略（同 FOCUS)：一次性时间戳、prompt 内
   自删除条件 + 删除命令、或已知截止时间的确定性一次性清理 prompt。
   `--cron` 未带终止策略时 helper 给出警告。
5. 触发时 daemon 不在，service unit 日志中留下控制面"未运行"错误（经
   `systemctl --user status kite-schedule-<name>` 可见）；不盲目重试
   (outcome-unknown 可见，不隐藏）。

## 5. 锁定行为的测试

- unit 渲染：一次性 vs 周期、`OnCalendar` 取值、service 内的 kitectl
  路径、`Persistent=` 开启。
- ctl-path 解析顺序（显式 > env/bin 目录 > venv)。
- create 校验：过去 `--at`、非法 `--cron`、无绑定的 chat → 拒绝且不写
  任何文件。
- list/show/remove/run-now 全流程（mock systemctl，不触真实 systemd);
  remove 需 `--yes`。
- 实例作用域（§3.1)：命名实例的 unit 名携带实例段、ExecStart 携带
  `--instance <名称>`；两个实例不可能冲突；list 按当前实例过滤；
  show/remove 对其他实例的定时任务 fail-closed 拒绝。
- `announce` 在提交前发出触发提示（daemon 侧，经控制面 prompt/submit
  的 display 标志）。
- 触发时 daemon 不在：service 日志记录拒绝且退出非零。
