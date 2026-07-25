# KITE vs FOCUS 一致性审查报告(2026-07-25)

> 类型:工作笔记(docs/_work/,非仓库事实文档)。审查结论待逐项修复;修复完成后本文件可归档或删除。
> 审查基线:KITE @ 7e1ffff;FOCUS @ 工作区当前;上游 kimi-code @ c497af60e(main,0.29.1-6-gc497af60e),KITE 验证版本 0.29.0,本机安装 0.29.1。
> 方法:8 个并行审查集群(飞书传输+卡片、事件管线+流式+patch 调度、命令面+权限、群聊+附件+定时、stores+基础设施、daemon+CLI+服务+控制面+安装、kap 适配器 vs 上游真实代码、文档契约端到端),外加主审查人对全部 HIGH 发现的独立代码复核。
> 基线状态:`python3 -m pytest -q` 全绿(1016 passed)。
> 总体判断:移植保真度整体很高(多个模块与 FOCUS 逐行/逐字节一致),但有 5 个 HIGH、约 15 个 MED 级问题,主要集中在:FOCUS 移植时点之后的修复未跟上、对上游 kap 的两个错误假设、群/审批路径的边界守卫缺失。

---

## 一、HIGH(5 项,全部经主审查人独立复核确认)

### H1. `/last` 历史回退在真实飞书 API 下永远失效

- 位置:`kite/feishu_transport.py:1144`(`list_messages`)、`kite/app_handler.py:1854`(`_last_text_from_history`)
- 事实:FOCUS 决策文档 `~/llm/focus/docs/decisions/feishu-raw-card-retrieval.md` §5.1 证实:不带 `card_msg_content_type=user_card_content` 时 message/list 与 message/get **不返回**发送时的原始卡片 JSON(只给扁平 re-render 形态,element id 丢失)。FOCUS 传输层在 `_list_history_messages_page`/`get_message_items` 支持该参数(`feishu_bot.py:1292,1308-1310`);KITE 2026-07-22 移植时丢失。
- 后果:`/last` 回退路径只导出 checksum 可验证的投影(fail-closed 设计)→ 真实历史中卡片无 element id → 永远跳过 → 永远回"该会话暂无终态答复记录"。主路径(terminal store)不受影响,但回退是死代码。KITE 测试(`tests/test_app_handler.py:612`)喂的是 as-sent 形态卡片,恰好掩盖;`test_last_history_marker_only_card_is_not_exported` 实际已证明扁平形态会被跳过。
- 修复:`list_messages` 增加 `card_msg_content_type: str = ""` 参数(非空时 `request.queries.append(...)`,FOCUS 同法),`_last_text_from_history` 传 `"user_card_content"`;补一个扁平形态输入的回退测试。

### H2. 流式 offset 单位错配:非 BMP 字符(emoji)导致每个 delta 触发一次全量快照重建

- 位置:`kite/streaming_transcript.py:58-72`(`expected = len(self._step_text)`),触发点 `kite/event_pipeline.py:1008-1018`
- 事实:上游 kap-server 用 JS `turn.assistantText.length`(**UTF-16 code units**)给 volatile 帧盖 offset(`~/llm/kimi-code/packages/kap-server/src/transport/ws/v1/inFlightTurnTracker.ts:82`);KITE 用 Python `len()`(code points)。每个非 BMP 字符使上游 offset 比 KITE 预期多 1。
- 后果:流中出现一个 emoji 后,后续每个 delta 都 offset > expected → 误判 gap → `_rebuild_session`;reseed 后基线仍是 Python 长度,下一个 delta 再次 gap(审查代理实证:`"a😀bc"` 逐字符流入触发 2 次快照重建)。含 emoji 的正常回复流在 RuntimeLoop 上产生每 delta 2 次阻塞式 REST 的重建风暴;内容正确(never-shrink 兜底)但循环被打爆。KITE 测试全用 BMP 中文所以未暴露。
- 修复:transcript(或 adapter 归一化层)统一用 UTF-16 code units 追踪 expected offset(`len(text.encode("utf-16-le")) // 2`),`rebuild_from_snapshot` 同样换算;补非 BMP 字符的 gap 不误判测试。

### H3. `kitectl schedule` systemd 单元把用户文本原样写进 ExecStart,`%` 说明符展开静默篡改文本或使单元拒载

- 位置:`kite/schedule_units.py:476-510`(`_quote_unit_arg` / `render_service_unit`)
- 事实:`_quote_unit_arg` 只转义 `\` 和 `"`,不做 `%` → `%%`。systemd 对 unit 文件做 specifier 展开:文本含 `%h` → 提交到 kap 的 prompt 被替换成 home 路径(本机 systemd 259 实测静默篡改);含 `%z` 等非法说明符 → 单元 `bad-setting`,timer 每次触发失败,schedule 静默失效。FOCUS 无此暴露:其 unit 只放 `--prompt-file <path>`(`~/.agents/skills/feishu-scheduled-prompts/scripts/manage_scheduled_prompt.py`),用户文本不落 unit 文件。
- 修复:systemd 渲染时对所有用户来源标量(`text`、`chat_id`、`ctl_path` 等)做 `%` → `%%` 转义;或回到 FOCUS 的 prompt-file 方案。`render_timer_unit` 的 OnCalendar 是解析产物,不受影响。

### H4. `kitectl service log` 读错日志源,在 Linux/Windows 上永远报错

- 位置:`kite/kitectl.py:438`(`path = _service_definition().stdout_log_path` → `<data>/service.stdout.log`)
- 事实:`service.stdout.log` 只有 launchd 后端会写(`kite/service_manager.py:329` StandardOutPath);systemd unit 无 StandardOutput 重定向,Windows `.cmd` 启动器也不重定向;kited 把 kap stdout 写到的是另一个文件 `kap-server.stdout.log`(`kited.py:317`)。daemon 真正的日志 `kite.log`(`logging_setup.py:27-33` RotatingFileHandler,全平台存在)反而无人读。FOCUS `focusctl service log` tail 的是 daemon 轮转日志(`manage_cli.py:1414-1420`),且有 follow 模式(KITE 只打印不跟随,语义缩减)。
- 修复:改为 tail `default_log_file(data_dir)`(kite.log);按文档决定是否补 follow。

### H5. WS ping 探活对真实 kap-server 无效:空闲连接每个 stale 窗口(默认 45s)都全量重连

- 位置:`kite/adapters/kap_server.py:1366-1371`(recv 超时→probe)与 `1382-1400`(`_probe_with_ping`)
- 事实:KITE 注释假设 "kap has no heartbeat, but answers app-level ping";上游 `wsConnectionV1.ts onMessage`(192-218)的 switch 无 `ping` 分支,客户端 app-level ping 落入 default 被静默忽略;上游注释明写 no ping/pong heartbeat;KITE 自己的研究文档 `docs/research/kap-server-usability.md:89` 也写着"无 ping/pong"。审查代理对真实 kap-server 0.29.1 现场实证:(a) 裸连发 `{"type":"ping"}` 帧 6 秒无任何回帧;(b) KITE 自己的 KapWsClient(stale=3s/ping_timeout=2s)空闲 14 秒发生 3 次 connect+disconnect——probe 从未成功。
- 放大器:`tests/fake_kap.py:550-558` 让假服务器应答 pong(真实服务器不会),`tests/test_kap_ws_client.py:157`(`test_ping_probe_keeps_idle_connection_alive`)因此绿得毫无意义。
- 后果:生产默认 stale_seconds=45,任何空闲会话每 ~55s 经历一次完整重连:N 个 warmup REST、client_hello、replay、`on_connection_change` 状态抖动、WARNING 日志刷屏;故障检测也比设计慢 ~10s;并放大 M14 的重放窗口。
- 修复:删掉 `_probe_with_ping`(recv 超时直接判 stale,与 `kite-design.md` §5 一致);同步修正 fake_kap 不应答 ping、重写该测试与 `kap_server.py:1367` 注释。

---

## 二、MED(KITE 自身 bug,FOCUS 无此问题)

### M1. `_question_timed_out` 两条路径丢失 pending 条目(与 approval 路径不对称)

- 位置:`kite/event_pipeline.py:1770-1795`
- 事实:(a) 入口即 `pop`;通用 `KapError`(非 40902/40405)只 log 后 return——条目已丢、卡仍可点、点击永远"已失效或已处理"、上游 question 永远 pending(agent 卡死)。对照 `_approval_timed_out`(`:1313-1323`)两条失败路径都重新登记,本函数自己的 `KapTransportError` 分支也 re-add——通用 KapError 分支为漏写。(b) pop 先于 `resolved` 检查:超时回调与点击完成竞态下,resolved 条目被 pop 后 return,随后 `question.answered` 事件到达时 `_question_resolved` 找不到条目早退,其余 item 卡永远不被冻结(已实证)。
- 修复:改 `.get` 检查后再 pop(对齐 approval);transient KapError re-add;resolved 条目留给 answered 事件关闭。

### M2. 无法路由/无法归因的 approval(及 question)只发过期卡,不上游应答也不起超时定时器 → prompt 永久悬挂

- 位置:`kite/event_pipeline.py:1156-1173`(unattributable)、`:1188-1212`(ownership 非 certain)、`:1636-1652`(question 同构)
- 事实:FOCUS 投递失败/无绑定时 `auto_reject_request` 一定应答上游(`interaction_request_controller.py:231-233, 291-294`)。KITE expired 分支直接 return:无 `_approvals` 条目、无 timer、无 REST resolve。上游 v2 approvals 不过期(`kap-server/src/routes/approvals.ts:84`),turn 无限期阻塞,只能靠 `kitectl interaction sweep` 人工解。副作用:每次 resync/重启 rebuild 都对同一 approval 重发一张过期卡(spam)。design §4.6 说 "explicitly expired and closed out",代码只做了前一半。
- 修复:expired 路径上游 resolve rejected/dismissed(fail-closed 到底),或登记条目并照常起超时定时器。

### M3. "拒绝并反馈"的 `_pending_feedback` 在审批关闭后不清除,吞掉用户下一条普通消息

- 位置:种植 `kite/event_pipeline.py:1398`;清理仅 `:1866-1868`(sweep)和 `:1947/1962/1977`(用户回复);缺失于 `_close_approval`(`:1511`)、`_approval_resolved`(`:1274`)、`_approval_timed_out`(`:1295`)、`_rebuild_approvals`(`:2129`)
- 事实:点"拒绝并反馈"后卡仍活着,另一管理员再点"批准"→ 审批关闭,feedback 条目残留;该用户之后发的任意文本被 `try_handle_interaction_reply` 拦截 → 回"该审批已处理"且**消息不进入 prompt 路径**。
- 修复:审批关闭的所有路径顺带清除该 approval 的 `_pending_feedback` 条目。

### M4. ack 游标无条件回写,可回退持久化游标(缺单调守卫)

- 位置:`kite/adapters/kap_server.py:1514`(`_handle_ack_payload`)
- 事实:KITE 自己另两处游标写入都有"同 epoch 不得回退"守卫(`_advance_cursor` `:1543`、`_adopt_snapshot_cursor` event_pipeline.py:2055-2060),唯独 ack 路径直接 `_cursor_set` 覆盖。竞态:subscribe 在 RuntimeLoop 线程等 ack,recv 线程继续分发事件;`[replay 101-110][ack@110][live 111]` 时 recv 线程先推 store 到 111,ack 处理随后写回 110 → 重启后重放 111 → 重复事件进 pipeline。影响有界(取决于 pipeline 幂等性),但直接违反己方游标纪律。
- 修复:ack 采用与 `_adopt_snapshot_cursor` 相同的守卫(同 epoch 且 `current.seq >= incoming.seq` 则跳过);跨 epoch 仍采纳。

### M5. `pending_interaction: 'none'` 未归一化,`/sessions` 对每个空闲会话显示"待处理:none"

- 位置:`kite/adapters/kap_server.py:517`(work_changed)、`894`(snapshot)、`1681`(list/get)
- 事实:上游 `SessionPendingInteraction = 'none' | 'approval' | 'question'`,wire 上每个无挂起交互的会话都是字符串 `'none'`(0.29.0 即如此,非漂移)。KITE `_optional_str` 透传 → `app_handler.py:2235` `if session.pending_interaction:` 恒真 → 每个空闲会话显示 `空闲;待处理:none`(**用户可见、必现**);`kitectl.py:352` 还把所有会话当有 flag → 每次 service stop 多打两轮 REST。fake_kap 用 `None`,测试从未覆盖。
- 修复:适配层边界把 `'none'` 归一化为 `None`(3 处解析点)。

### M6. durable `error` 事件帧永不推进游标,重连后重复投递且可误判当前 prompt

- 位置:`kite/adapters/kap_server.py:1426-1441`(`_dispatch_frame` 在 `_parse_event_frame`/`_advance_cursor` 之前拦截 `type=="error"`)
- 事实:上游 `error` 是普通 durable 事件(不在 VOLATILE_SIGNAL_TYPES;现场实证带 seq/epoch/session_id,如 `provider.auth_error`)。KITE 拦截后游标不动 → 重连按旧游标 replay → error 帧再次触发 `_error_frame_impl` → 可能把**当时健康的 prompt** 误判 failed。缓解:turn 失败后通常紧跟 work_changed 把游标带过;只有"error 是 journal 尾部"时暴露(空闲期 MCP/compaction 错误)。`tests/fake_kap.py:184-203` 的 `send_error_frame` 不带 seq/epoch,注释"not a durable event"与真实相反。
- 修复:error 帧带 seq/session_id 时先 `_advance_cursor` 再触发 `on_error_frame`;修正 fake 的 error 帧形状。

### M7. ack 列入 resync 且订阅未建立时,重建后不补订阅 → 会话静默到下次重连

- 位置:`kite/adapters/kap_server.py:1515-1528` + `kite/event_pipeline.py:2016-2046`
- 事实:`client_hello`/`subscribe` 对冷会话(warmup 竞态失败)或已删除会话,上游不建立服务端订阅,ack 只列入 `resync_required`。KITE 触发 `_rebuild_session` 做快照重建并采用游标,但全链路没有任何地方重新 subscribe——该会话在剩余连接生命周期内收不到 live 事件。目前被 H5 的"意外重连"掩盖(~55s 自愈);H5 修好后健康连接下可静默数小时。buffer_overflow/epoch_changed 类 resync 不受影响(服务端订阅仍在)。
- 修复:快照重建成功后对该 session 重新 `subscribe()`(防 tight-loop 计数)。

### M8. `/init` 在群聊中可执行,常驻管理员 token 泄露给全群(安全)

- 位置:`kite/app_handler.py:555-557`(群 ingress 对 admin 的任何 slash 命令直接 `_dispatch_command`)+ `:1429`(`_cmd_init`)
- 事实:FOCUS 给 `/init` 挂 `scope="p2p"`(群里拒绝并提示私聊,`codex_handler.py:1779-1785`,scope 检查先于 admin 检查);KITE 移植时整套 per-command scope 机制丢失,`/group`、`/group-mode` 补了 ad-hoc 群限定,`/init` 未补 p2p 限定。init token 是常驻秘密(`ensure_init_token` 只生成一次不轮换,`config.py:96`)→ 管理员在群里敲 `/init <token>` 后任何群成员可抄走自注册管理员。
- 修复:`_cmd_init` 或群 ingress 处拒绝非 p2p 的 `/init`,回复"请私聊机器人执行 `/init`"。

### M9. `/switch`(含 /sessions 按钮)在活动 prompt 期间不拒绝,审批路由静默失效

- 位置:`kite/app_handler.py:1971-2048`(`_switch_to_session` 无 active-prompt 检查)
- 事实:FOCUS 换线程时有在途执行则拒绝("当前线程仍在执行,暂不切换。",`codex_handler.py:3012-3017`)。mvp-scope 对齐项 7 只对 `/new` 立规,其理由("在途工作的执行卡、终态结果与审批路由会失去可见性")对 `/switch` 逐字成立。后果:换绑后旧 session 新到的 approval 走 fail-closed 分支,expired 卡 targets 为空不发往任何 chat,审批在上游挂到 5 分钟超时。
- 修复:`_switch_to_session` 换绑前对当前绑定 session 跑与 `check_new` 相同的 active-prompt 预检;或明确文档化此有意差异。

### M10. `/attach` 与 `/group activate` 无 all 模式独占复查,可静默打破独占不变量

- 位置:`kite/app_handler.py:1594-1606`(`_cmd_attach`)、`:1675-1696`(`_cmd_group activate`)
- 事实:可达路径 A:B 绑定 S 后 /detach → all 群 G /switch S(detached 不占坑,放行是设计)→ B /attach(无探针)→ S 上两个 attached chat(G 为 all 模式)。可达路径 B:all 群 deactivate → 另一 chat /switch 进该 session → 群里 activate(保留旧 mode,无探针)→ 共享会话。两条都违反 group-chat §2 与 §4.6 "never silently allowed"。FOCUS 有写时强制(`prompt_write_denial_check`,每条 prompt 前复查)使共享态惰性化;KITE 的 `_handle_prompt` 完全没有写时独占检查。另小文案 bug:activate 回复"群成员 @我 并发送文字即可提交 prompt"在 all 模式下措辞错误(all 不需要 @)。
- 修复:`_cmd_attach` 翻转前、`_cmd_group activate` 结果 mode 为 all 时,跑 `all_mode_session_exclusive`/`all_mode_session_occupied` 探针;激活回复按 mode 区分措辞。

### M11. mention_only 群路径丢失 app sender / 自消息守卫(跨 bot 注入)

- 位置:`kite/app_handler.py:581-594`(mention_only 分支)
- 事实:FOCUS `feishu_bot.py:1965-1967` 在模式分发前 `if chat_type == "group" and sender_type == "app": return`。KITE assistant(`:618-620`)和 all(`:700-703`)分支都有 `sender_type == "app" or sender == bot_open_id` 检查,唯独 mention_only 没有——`bot_mentioned && text && sender_open_id` 即直达 `_handle_prompt`(已用测试脚手架实证:app sender + mentioned → 成功提交 prompt)。后果:同群另一个机器人 @KITE bot 即可触发 prompt。tests/test_group_chat.py 中 app sender 用例只覆盖 assistant 与 all。
- 修复:mention_only 分支(或模式分发前统一)补同款守卫 + 测试。

### M12. 转发+留言合并语义丢失,且顺序倒挂:留言 prompt 先于转写 prompt 提交

- 位置:FOCUS `feishu_bot.py:1930-1943` vs KITE `kite/app_handler.py:834-865, 882-916`
- 事实:FOCUS 把 merge_forward 暂存 2s,窗口内同一 (sender, chat) 的下一条非附件消息认领暂存转写,`<forwarded_messages>` 与留言合成**一条** prompt。KITE 重新设计为"窗口内 N 条 merge_forward 聚合",无暂存-认领机制——留言立即独立提交,2s 后转写 flush 再提交第二条;FIFO 下留言(指令)先执行且看不到转发内容,转写脱离指令。KITE 文档(group-chat §3.7、focus-assets-map)均未登记此为有意裁剪。
- 修复:恢复"窗口内下一条文本认领暂存转写"的合并;或把裁剪写进契约并说明理由。

### M13. launchd 后端给一次性 `--at` 渲染了非法 `Year` 键 → 一次性退化为每年重复

- 位置:`kite/schedule_units.py:563-575`(`launchd_start_calendar_interval`)、`:633-637`
- 事实:launchd.plist(5) 的 StartCalendarInterval 只定义 Minute/Hour/Day/Weekday/Month;`Year` 非合法键被忽略 → macOS 上 `--at 2026-08-01 10:30` 变成每年 8 月 1 日重复,与 systemd/Windows 的一次性语义不一致;违反模块自己"each backend renders only the forms it can express faithfully and rejects the rest (fail-closed)"原则;`tests/test_schedule.py:651-655` 把 Year 锁成了预期。
- 修复:launchd 后端对 `--at` 直接 `ScheduleError`(诚实 fail-closed),或包自卸载实现一次性语义。

### M14. env 文件名文档/代码不一致,按文档放凭证会静默失效

- 位置:`kite/platform_paths.py:18`(`ENV_FILE_NAME = "kite.env"` → 默认 `~/.config/kite/kite.env`)
- 事实:`docs/architecture/kite-design.md:217`、`AGENTS.md`、`install.py:169` next-steps 全写 `~/.config/kite/env`。用户按文档写 `env`,kited 读 `kite.env` → 凭证静默缺失。另外 `ensure_env_template`(`env_file.py:57`)全仓库无人调用(仅测试),FOCUS 在安装脚手架生成 0600 模板。
- 修复:二选一统一(建议改 `ENV_FILE_NAME = "env"` 对齐设计文档)或改三处文档+install 文案;install.py 里生成模板。

### M15. `config/system.yaml.example` 管理员键名与代码不符

- 位置:`config/system.yaml.example:11`(`# admins: []`)vs `kite/config.py:163`(读 `admin_open_ids`)、`app_handler.py:329`(/init 持久化写 `admin_open_ids`)
- 后果:按示例手配 `admins:` → 静默零管理员,所有 admin 命令被拒(fail-closed 但极难排查)。
- 修复:示例改为 `admin_open_ids`。

---

## 三、MED-LOW / LOW(按主题归并)

### FOCUS 移植后漂移(FOCUS 5787d4c,2026-07-24,晚于 KITE 07-22 移植时点)

- L1. 卡片 markdown 缺原始 HTML/XML 开标签中和:`kite/feishu_card_markdown.py:51,95` 缺 `_neutralize_raw_html_xml_openers_outside_fenced_code`;工具输出/模型文本含原始 HTML/XML 时飞书**整张卡片拒绝** patch(流式 patch 持续被拒,卡面冻结在执行中)。修复:原样移植 FOCUS 5787d4c 的 `feishu_card_markdown.py` 变更(含测试)。
- L2. patch 缺 230099 → `content_rejected` 分类与执行卡极简终态重试:`kite/feishu_transport.py:964-983` 一律 `failure()`;FOCUS `feishu_bot.py:2397-2408` + `runtime_card_publisher.py:264-294` 对非 running 执行卡重试一次极简终态卡。KITE 执行卡 freeze patch 被拒后永远停"执行中"且取消按钮仍可点。修复:随 L1 一起移植。

### 事件管线

- L3. 终态文本归因启发式可跨 prompt 张冠李戴:`event_pipeline.py:210-241` 取"最新一页第一条带 text 的 assistant 消息",纯工具收尾的 turn 会钉上一个 prompt 的答复并入 terminal store(已实证);attempt-1 在 `_refresh_queue_depth` 之前执行有次级窗口。修复:注释记录边界;中期利用上游 message 的 prompt 归因字段(当前未填充);`moved_on` 检查前移。
- L4. 孤儿终态(standalone)路径不落 terminal store:`event_pipeline.py:848-859` 无 upsert(对照 `:879-890`),`/last` 丢结果。修复:`terminal_message_id and text` 时同样 upsert。
- L5. 无 watchdog/卡死 reconcile(FOCUS 8s 镜像看门狗未移植):focus-assets-map 已登记为 hardening 延后项,LOW。修复:按规划补 generation-guarded watchdog,或文档明确兜底(kitectl)。
- L6. 工具行上限丢弃的是**最新**活动且无截断提示(`event_pipeline.py:611-613`,FOCUS 保留尾部+提示)。修复:尾部淘汰 + 截断提示行。
- L7. 重复/重放的 turn.started 会给同一 prompt 冻结现有卡再发新卡(`event_pipeline.py:568-603`,FOCUS `reuse_existing_card=True`)。可达性低,外观问题。
- L8. 会话标题一次拉取失败即永久缓存空串(`event_pipeline.py:2234-2244`)。修复:失败不缓存或短 TTL。
- L9. `_terminal_delivered` 集合无界增长(`event_pipeline.py:422,833`)。卫生项。
- L10. fire-and-forget submit 任务异常被静默吞掉(`runtime_loop.py:91-103`;`_rebuild_session`/`_shutdown_impl` 走 submit,意外异常无声消失)。修复:`_run` 中对无 done event 的 error 补 logger.exception。
- L11. `_advance_cursor` 的 epoch 检查排在 seq 检查之后(`kap_server.py:1541-1561`):epoch 轮换后新 seq 回退,epoch 不等判断到不了,直到新 epoch seq 超过旧高水位才触发 resync(缓解:断线重连有服务端 resync_required 兜底)。修复:先判 epoch 再比 seq。

### 命令面 / 群聊

- L12. `/sessions` 卡片不含 cwd,与 mvp-scope §2 "title/cwd/busy" 不符(`app_handler.py:2232-2238`;`SessionSummary.cwd` 已解析未用)。kitectl 侧有 CWD 列。
- L13. 裸 `/`(及 `/ xxx`)被当 prompt 提交而非未知命令(`command_surface.py:124-140` → `app_handler.py:525-532`;FOCUS 一律按未知命令答复)。
- L14. `/group-mode` 不做写法归一(`mention-only`/`mention` 被拒;FOCUS `codex_group_domain.py:87-91` 有 `-`→`_`、`mention`→`mention_only`)。
- L15. p2p 转发"抓取成功但无可渲染内容"静默吞掉(`app_handler.py:852-858` 只对原始 items 判空;FOCUS 对渲染后文本判空并回复提示)。
- L16. assistant 历史回填丢了 FOCUS 的话题(thread)过滤器(`group_history.py:87-130` vs `group_history_recovery.py:303-304`);话题回复会混入主流上下文。建议确认并补文档。
- L17. 审批"拒绝并反馈"toast 说"群聊中需 @机器人 回复",all 模式下不经 @ 门,提示语义错误(`event_pipeline.py:1403-1406`)。
- L18. 机器人被移出群/群解散时 KITE 保留群配置与日志,FOCUS 清除(`app_handler.py:490-493` vs `feishu_bot.py:654-669`);重新拉进群时旧 activated/mode 无需管理员操作即复活。group-chat 合同对 bot-removed 语义沉默,需先立合同再定行为(建议至少 deactivate)。
- L19. 群内附件 staging 实为仅管理员(`app_handler.py:723-731` 自承 "in this cut"),images.md/group-chat.md 未登记该限制,成员发图无任何提示。需产品裁决:补文档 or 放开实现。

### 定时 / CLI / 服务

- L20. `kitectl schedule create` 默认 ctl-path 解析在 Windows 必然失败(`schedule_units.py:417-419` 候选无 `Scripts/kitectl.exe`;install.py Windows 不写 wrapper)。
- L21. init token 并非"安装时生成"(mvp-scope §5 与代码不符;只有 kited 首启 `ensure_init_token()`;且无 `kitectl config init-token` 对应物)。
- L22. `kitectl service status` 丢失 FOCUS 退出码语义(FOCUS 不在运行返回 3,KITE 恒 0)。
- L23. kited 对 `kap:` 配置节校验错误不给干净退出(`kited.py:496` `kap_settings(config)` 在 try 之外)。
- L24. readiness 等待期间 SIGTERM 最长被拖 60s(`kited.py:320` → `_wait_ready` 无 stop_event 钩子;若升级 SIGKILL,fail-close 清扫被跳过)。
- L25. 启动期 `ws.subscribe()` 异常可击穿 run()(`kited.py:383-384`;小窗口,systemd 救活)。
- L26. 自定义 `--config-dir/--data-dir` 下 `service install` 产出自相矛盾的 unit(`kitectl.py:266-271`;unit 不带目录参数,FOCUS 会写 `--instance`)。
- L27. `kitectl session status` 遇一个坏 session 全军覆没(`kitectl.py:193`;可选 per-session 错误行)。
- L28. `ServiceStopPreview.kited_running` 死字段,preflights.py:226-228 docstring 失实。
- L29. `kap.host` 接受任意值但子进程从不带 `--host`(`kap_server.py:1008-1015`;非 loopback 必然 readiness 超时。建议配置校验显式拒绝非 loopback)。
- L30. kitectl 直读 BindingStore,bindings.json 损坏时抛裸 traceback(`kitectl.py:169, 448, 607`;违反自身 `_die` 纪律)。
- L31. WS 重连无退避,token 失效后 2s 死循环(`kap_server.py:1289-1311`;warmup 401 还计入上游限速器)。修复:指数退避封顶,连续 N 次 auth 失败转告警态。
- L32. `_pending_acks` 对超时后迟到的 ack 永久泄漏(`kap_server.py:1404-1409`)。
- L33. subscribe ack 的 `not_found` 被忽略,已删除会话永久滞留重订循环(`kap_server.py:1508-1529`)。
- L34. `_tool_display_detail` 缺 `todo_list` 分支;docstring 与实现不符(`kap_server.py:1699-1736` vs 上游 `display.ts:49-51`)。

### 文档 gap(代码行为正确或待裁决,改文档为主)

- D1. mvp-scope §6 结构化日志缺字段:started/ended 缺 chat_id;approval resolved 缺 chat_id/prompt_id;resync/重建日志缺 chat_id/prompt_id(`event_pipeline.py:548,861,1288,2040`)。
- D2. mvp-scope §4.5 措辞是 FOCUS"提交即建卡"模型遗留;KITE 提交期业务错误回复文本、执行期 error frame 转 failed 卡,意图达成,建议改文档。
- D3. design §7 "atomic write + file lock" 实际只有 cursor store 有跨进程 flock(其余 store 单写者 tmp+rename,行为安全,文档夸大)。
- D4. group-chat §3.3 "5s boundary slack (config-overridable)" 实际不可配置(`group_history.py:66` 支持参数但 kited/config 未接线)。
- D5. images.md 未登记"群内附件仅管理员"(同 L19)。
- D6. init token 合同(安装时生成)与代码(首启生成)自相矛盾(同 L21)。

### 测试掩蔽面(fake_kap 与真实上游的漂移,修复 H5/M5/M6 时一并处理)

- T1. `fake_kap.py:550-558` 应答 pong(真实服务器不应答)→ H5 的测试绿得没意义。
- T2. `fake_kap.py` 用 `None` 表示无 pending_interaction(真实 wire 是 `'none'`)→ M5 未覆盖。
- T3. `fake_kap.py:184-203` error 帧不带 seq/epoch/session_id(真实是 durable)→ M6 未覆盖。
- T4. `fake_kap.py:175-182` question wire 用 `items` 键、option 无合成 id(真实是 `questions[]` + `q_<i>`/`opt_<i>_<o>` + allow_other)→ 未来 question 重建测试会静默解析 0 项。

### 上游漂移注记(0.29.0 → HEAD c497af60e,经 0.29.1)

- U1. `d751b6796`(0.29.1):`event.session.work_changed` 改为全局事件,无订阅也扇出到每条连接。KITE 会给未订阅会话推进游标/建 work 状态,目前无害(replay 以订阅游标为准),建议适配层按 wanted/bound 过滤。
- U2. `a2401cc1e`(0.29.1 之后):新增 provider 写端点,KITE 消费面不受影响。
- U3. KITE 消费切片在 0.29.0→HEAD 无破坏性协议变化(WS_PROTOCOL_VERSION 仍为 2,事件目录/ack/resync/snapshot/journal 格式均未变)。

---

## 四、修复优先级建议

1. **立即修(生产可见/凭证失效)**:M5(`'none'` 归一化)、H5(ping 探测)、M14(env 文件名)、M15(admin_open_ids 示例键名)、H4(service log)、H3(`%` 转义)。
2. **正确性高危**:H1(/last 回退)、H2(UTF-16 offset)。
3. **安全**:M8(/init 群守卫)、M11(mention_only app-sender 守卫)。
4. **状态机正确性**:M1、M2、M3、M4、M6、M7。
5. **FOCUS 漂移同步**:L1+L2(移植 5787d4c)、M12(转发语义,需先裁决是否合同化)。
6. **契约对齐(代码或文档二选一)**:M9、M10、L12-L19、D1-D6。
7. **卫生项**:L7-L11、L20-L34、T1-T4、U1。

每项修复按仓库惯例附回归测试(当前多个发现正是被测试掩蔽:H1/H2/H5/M5/M6/M11)。

---

## 五、已核对一致(抽样/逐项验证无分歧,压缩记录)

- 传输层:消息去重(500/300s)、patch 重试(230020/timeout/2s)、send/reply/delete 语义、附件下载/上传、reply-in-thread 缓存(1000/600s)、WS 长连 auto_reconnect、mention 归一化与 fail-closed、卡片动作解析与 `make_card_response` 注入约定。
- 卡片:`card_limits.py` 逐行一致;终态卡 schema 2.0 + 标题/模板与投影识别闭环;checksum 绑定渲染后可见文本(KITE 比 FOCUS 更自洽);建造侧守卫 KITE 实际调用(FOCUS 反而未调用);26000B/12000 字符预算一致;marker 注入/嵌图/超额三向 fail-closed 有测试。
- 运行时:`runtime_loop.py` 逐行一致;`CardPatchDispatcher` 槽位状态机(≤1 in-flight + 恰好一次 trailing flush、latest-wins、retry-after 钳制)一致且 KITE 有增强(cancel/timer 注入/worker 异常防死);700ms 节流一致;点击防重两阶段守卫+失败回滚+40902 冻结一致;终态去重单点;fail-close 清扫(/new /switch 按 chat、shutdown 全量、kap 不可达跳过上游应答但本地 patch)符合 mvp-scope 对齐项 8;快照重建 cursor 只进不退;gap 闩锁(BMP 流恰好一次重建);never-shrink reconcile;actor 检查与编号回复解析;kited 关闭次序(先清扫后停 kap)。
- 命令面:/init 流程主体、非 admin 放行集合 {/help,/init,/whoami}、/new 先建后绑+忙碌拒绝、/detach 从不拒绝(文档化有意)、/mode /plan 三态与 yolo 管理员限定+群内公告、/status 覆盖、/last 主路径(store 优先+15000 截断+历史失败显式报错)、/abort 权限与 40402、审批路由(可操作卡只发 certain 归属 chat,其余只读通知)、/sessions 排序/过滤/管理员校验、群 ingress 矩阵(未激活/mention_only/assistant/all)、/group /group-mode 管理员双检、PromptOwnership certain/best_effort 与重建、IdentityNames 缓存链、preflights ReasonedCheck 体系与 restart preview stale-flag 修复无残留、帮助文案全角占位符。
- 群聊/附件/定时:boundary triple 逐行等价(同毫秒 id 集合去重、5s slack、24h/50 上限)、历史回填 fail-closed 且边界只在提交成功后推进(KITE 有据改进)、自消息过滤、GroupLogStore/GroupConfigStore 损坏 fail-closed 读法、激活门禁、actor-at-click、all 模式正反向探针四处接线(/group-mode all、/switch、/new、首绑)、合并转发 2s 窗口/per-(sender,chat)/上限/逐项隔离/UTC+8/close fail-closed、附件域 TTL/消费一次性/失败恢复/cwd 校验/文件名消毒/字节上限、PendingAttachmentStore take 同时清扫、schedule create 前置校验/稳定 hash 名/Persistent=true/remove --yes/run-now/announce 先通知后提交/Windows XML fail-closed。
- stores/基础设施:file_lock/process_utils/cli_table 与 FOCUS 逐字节相同;platform_paths/env_file/logging_setup/file_permissions 纯改名;config.py helper 1:1 保留且新校验有测试(approval_timeout=300、附件 TTL 600/20MB、群历史 50/24h);binding_store 损坏 raise fail-closed + schema_version 已补;terminal_result_store 忠实移植;event_cursor_store flock+原子写+0600;FOCUS 独有 store 缺席均有文档依据。
- daemon/CLI/服务/控制面/安装:service_manager 三平台逐行一致;control_plane 与 decisions 文档完全对齐(1MB cap、hmac、空 token fail-closed、三段式错误分类);kitectl 读写纪律(写走控制面、读直连、interaction sweep 有合同背书);kited 监督循环(bounded backoff、pid 匹配注册表、crash 窗口 rest 置空、干净停机保留 rest 供清扫);KapServerProcess SIGTERM→SIGKILL 升级/端口冲突/环境白名单;install.sh 与 FOCUS 逐字节一致;install.py 受管 venv 纪律。
- kap 适配器(对上游 0.29.1 真实代码+现场实验):REST 信封/request_id、`GET /meta`、`POST /shutdown`、sessions 分页、`last_seq` 硬编码 0 未被使用、`GET .../prompts` resume-backed 预热、prompt 提交 text/image part 与 permission_mode/plan_mode/model 字段、abort 40402、snapshot 全字段与 pending 投影(q_<i>/opt_<i>_<o> 合成 id)、approvals/questions REST 与 40902/40404/40405/40909 怪癖处理、messages 分页 role 过滤、WS 路径/Bearer/server_hello/client_hello/ack 载荷/replay 语义/resync 独立帧先于 ack、journal 跨重启保 epoch、volatile offset=pre-append 按 step 归零、durable 事件目录名与载荷全对齐、实例注册表 pid/port 解析、server.token 0600、版本 warn-don't-block 策略。
- 契约端到端:mvp-scope §4 fail-closed 清单 1-10 条全部有落点(§4.5 措辞见 D2);§5 权限模型 OK;§3 并发行为 OK;design §4 七条状态轴各有 owner,未发现第八条轴;streaming-cards/images/group-chat/scheduled-prompts 各合同条款 OK(例外已列 L 系列)。
