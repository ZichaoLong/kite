# OKbot 与 FOCUS 对比：两条路线的教训

> 类型：research（证据材料，非合同）。调查时间：2026-07-21。
> 调查对象：`~/llm/kimi/OKbot`(kimi-cli fork,HEAD 38e305e）与
> `~/llm/focus`(FOCUS，飞书 ↔ Codex 桥接）。
> 用途：回答"KITE 应该走哪条路、避开哪些坑"。

## 核心差异：fork 内嵌 vs 协议桥接

| 维度 | FOCUS | OKbot |
| --- | --- | --- |
| 与上游关系 | 独立 daemon，协议桥接未修改的 `codex app-server`(WS JSON-RPC)；升级上游 = 换 CLI 版本 | `MoonshotAI/kimi-cli` v1.9.0 的 **fork**，飞书焊进源码树（`src/kimi_cli/feishu/`，单文件 `sdk_server.py` 4593 行）；升级上游 = 持续 merge |
| 会话模型 | 五状态轴显式契约（binding/attached/loaded/running/interaction owner),JSON store | `chat_id:user_id` → 内存 dict 里的 KimiSoul；持久化复用 kimi-cli session 文件 |
| 多端/多实例 | 多订阅广播 + interaction owner 独占写入；跨实例 loaded gate + 机器级 lease,**fail-closed** | 多飞书账号（每账号一 ws 线程），但无多进程/多机协调；并发写只靠文件锁兜底 |
| 审批模型 | approval_policy + permissions_profile 持久化、每 turn 显式应用，**fail-closed** | YOLO 自动批准（默认开）+ 三键审批卡，**出错时 fail-open 自动批准** |
| 消息渲染 | 单锚点执行卡流式 patch（每会话任一时刻最多一张）+ 独立终态卡 | 分段多消息：thinking 折叠卡、正文每 ~100 字符一张卡、工具调用各发独立卡（旧 StreamingCard 路径是死代码） |
| 功能范围 | 克制：thread 生命周期、权限、群聊合同、图片、定时 prompt | 扩张：定时任务、长期记忆（SQLite+embedding)、后台任务唤醒、语音 ASR、文生图、浏览器/Android 设备操控、MCP/Skills 热更新、Plan Mode |
| 测试 | 1137 测试，与源码约 1:1；核心链路覆盖重 | 639 测试，但飞书核心 `sdk_server.py`(4593 行）几乎无直接测试（`tests/okbot/` 仅 509 行） |
| 文档 | contracts/architecture/decisions 合同体系；代码文档不一致视为 contract gap | 自有文档与代码脱节（webhook 模式、`card.py` 已不存在仍写进 README)、版本号不一致、`print` 调试 |

## 设计哲学的分野

- **FOCUS 把"共享同一个 live thread 的安全性"当核心问题**：复杂度花在状态轴、
  租约、跨实例准入上，宁可拒绝也不冒险。功能面刻意收窄。
- **OKbot 把"飞书里能用上 Kimi 的全部能力"当核心问题**：审批默认放行、出错
  自动批准，省掉协议层开销换功能快速堆叠。并发安全、多实例、状态契约基本
  不存在。

## 对 KITE 的教训

1. **OKbot 路线在 kimi-code 上物理不可行**:kimi-code 是 TypeScript 重写，
   "Python 进程内 import agent 内部 API"的接口不存在。fork 内嵌随 kimi-cli
   一起终结。
2. **功能堆叠的代价是可维护性**:OKbot 的飞书核心 4593 行几乎无测试、文档
   与代码脱节、审批 fail-open——功能增速超过架构承载力后，用户体验到的不是
   功能而是 bug（与 OpenClaw 同型的问题）。
3. **OKbot 的正面价值是功能需求清单**：定时任务、后台任务唤醒、记忆、语音、
   富媒体都验证过有真实需求。KITE 应按自己的承载力门槛挑选吸收（定时 prompt
   已在 FOCUS 有现成合同）,**吸收需求，不吸收架构**。
4. **FOCUS 路线在 kimi-code 上第一次成为可能**:kap-server 提供了对标
   `codex app-server` 的长驻后端（详见 `kap-server-usability.md`),FOCUS 的
   整套设计可以平移。

## OKbot 增量功能的处置建议

| OKbot 功能 | KITE 处置 | 理由 |
| --- | --- | --- |
| 定时任务（自然语言 cron) | Phase 3 吸收 | FOCUS scheduled-prompts 合同可平移 |
| 后台任务唤醒会话 | 暂缓，观察上游 `task.*` 事件 | kap-server 已有 task 事件流，先让 MVP 跑通 |
| 长期记忆 | 不做 | 超出桥接器职责；属 agent 侧能力 |
| 语音 ASR | 不做（Non-goal) | 引入外部 ASR 依赖，与桥接无关 |
| 文生图/设备操控 | 不做（Non-goal) | 属 agent 工具能力，不属于桥 |
| MCP/Skills 热更新 | 不做 | 属 agent 配置面，走 `kitectl` 或上游 CLI 即可 |
| Plan Mode | MVP 吸收（透传映射） | kap-server per-prompt `plan_mode` 原生支持，成本极低 |
