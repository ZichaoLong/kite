# KITE

**KITE — Kimi-code In Threads, Everywhere**，飞书(Feishu/Lark)↔ kimi-code 的桥接项目。

命名意象：kite 是一种鸟（鸢），与 Lark（云雀）同属鸟类主题；风筝靠一根线把空中的它与地面连起来——这正是本项目做的事：把云端飞书会话系在本地 kimi-code 上。

## 定位

KITE 把飞书机器人接到 kimi-code 的共享后端（kap-server）上，让飞书会话可以驱动、观察、接续同一个 kimi-code session。

架构蓝本是姊妹项目 **FOCUS**（飞书 ↔ Codex app-server 桥接，位于 `~/llm/focus`）：复用其分层架构、状态契约与工程纪律，把上游适配层从 `codex app-server` 换成 kimi-code 的 **kap-server**(REST + WebSocket)。

产品取向**克制**：功能增速不得超过架构承载力。每个功能进入开发前必须先立合同（见 `docs/contracts/mvp-scope.md` 的承载力门槛）。反面教材是 OKbot 式的 fork 功能堆叠（见 `docs/research/okbot-vs-focus.md`）。

## 当前状态

**M1 进行中**：第 0 里程碑（spike）已于 2026-07-21 通过（kimi 0.28.1，结果见
`docs/verification/spike-results.md`),`contracts/`、`architecture/`、`decisions/`
已转 active。已完成：仓库骨架与安装器（`install.sh`)、FOCUS 原语/stores/RuntimeLoop
移植、service_manager、kap 适配层与 `kited`/`kitectl session` 最小竖切（171 tests)。
进行中：飞书传输层与应用层（需飞书自建应用凭证联调）。

## 文档入口

- 文档索引与阅读规则：`docs/doc-index.md`
- 总体架构设计：`docs/architecture/kite-design.md`
- MVP 功能合同：`docs/contracts/mvp-scope.md`
- 上游后端选型：`docs/decisions/backend-selection.md`

## 关键外部参考

| 参考 | 位置 | 角色 |
| --- | --- | --- |
| FOCUS | `~/llm/focus` | 架构蓝本与资产复用来源 |
| kimi-code | `~/llm/kimi/kimi-code` | 上游；kap-server 行为以其实际代码为 source of truth |
| kimi-cli | `~/llm/kimi/kimi-cli` | 上游前身（Python）；仅作历史参考，不作为桥接目标 |
| OKbot | `~/llm/kimi/OKbot` | 对标项目；功能需求清单参考，非架构参考 |
