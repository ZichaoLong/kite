# 决策：进程形态、语言与本地 TUI wrapper

> 状态：已决（待对齐项见末节）。

## 问题一：Python 复用 FOCUS 资产，还是 TypeScript embed kap-server?

kap-server 以库形式导出 `startServer()`，理论上 KITE 可以是一个 TS 进程，
把引擎、API、桥三者放进同一进程。

**决定：Python,kap-server 作为 managed 子进程。**

理由：

1. **资产复用是数量级差异。** FOCUS `bot/` 约 4 万行中，上游无关部分占
   大头——飞书传输层、卡片、RuntimeLoop、binding/stores、群聊 domain、
   service_manager、install 体系。真正绑死 codex 的只有适配层约 1400 行
   (`adapters/codex_app_server.py` + `codex_protocol/client.py`)。选 Python
   = 换掉这 1400 行 + 按 kap 语义修订合同；选 TS = 其余 3.8 万行全部重写。
2. **fork 内嵌路线已被证伪。** OKbot 证明内嵌的代价是跟上游 merge 到死;
   且 kimi-code 是 TS,fork 内嵌在物理上就不存在接口。
3. **进程隔离本身是优点。** kap-server 崩溃不带走桥的状态机；桥崩溃不带走
   session runtime。FOCUS 的 managed 模式已验证这个形态。
4. kap-server 无 daemon 模式的缺陷，恰好被 managed 子进程形态补掉。

代价：多一层进程管理（端口、token、生命周期）;FOCUS 的对应代码可直接
移植，代价已预付。

## 问题二：为什么暂缓本地 TUI wrapper（对应 FOCUS 的 `focus`/`fcodex`)?

FOCUS 的核心卖点之一是"本地终端继续飞书正在操作的同一 live thread"。
**该能力在 kimi-code 上当前不可实现，MVP 不做，命令名 `kite`/`kcode`
预留。**

依据（详见 `docs/research/kap-server-usability.md` §6):

- 交互式 `kimi` TUI 不能连 kap-server，无 `codex --remote` 类模式；TUI 与
  server 的引擎还不同代（v1 vs v2)。
- `kimi -S` resume 的是磁盘会话，且跨进程无 session 锁：kap-server live
  持有时用 TUI 续写 = 两进程无锁双写同一 session 目录。KITE 若提供 wrapper
  就是在产品上鼓励这条数据损坏路径。

### 替代立场：裸 kimi 不在共享合同内

与 FOCUS 对裸 codex 的立场一致，写进 README 与用户文档：

> 当 session 正由 KITE 的 kap-server 持有（busy 或近期活跃）时，不要用
> `kimi -S` / `kimi -c` 在本地续写同一 session；要本地操作，先在飞书侧
> `/detach` 并确认 session 空闲，再自行承担冷接续。

未来上游若出现远程 attach 能力（wire 回归、或 TUI 支持连 kap-server),
按 FOCUS 的 wrapper 设计补回：薄壳 + 本地代理 + exec 上游 TUI。

## 问题三：TUI wrapper 暂缓后，本地接续由谁承担？（2026-07-21 核查后补记）

**由上游 web UI 承担。** 核查确认（证据：
`docs/research/kap-server-usability.md` 补充核查）：

- kimi-web 是 kap-server 的纯 /api/v1 客户端，无特权通道、无独占假设，
  与 KITE 桥接地位完全平等；
- 并发由服务端 FIFO + 广播收敛 + 审批幂等兜住，飞书端与 web 端同时
  操作同一 session 是上游设计内的一等场景；
- web UI 有真实投入的移动端适配，`kimi web --host` + 带 token 的
  Network URL 原生支持手机访问。

因此 KITE 的"本地继续同一 session"故事 = `kited` 看管的同一个
`kimi web --no-open`，无需任何自研 wrapper。这也强化了拉起方式选择
`kimi web --no-open` 而非纯 API shim（见 `kite-design.md` 已对齐 1)。

产品定位上的推论：手机在同一局域网时，web UI 与飞书机器人功能重叠；
KITE 飞书面的独特价值在 web UI 覆盖不到处——跨网络可达（飞书推送
无需 VPN/隧道）、群聊共享、IM 原生异步审批。

## 已对齐（2026-07-21)

1. 拉起命令：`kimi web --no-open`（见 `kite-design.md` 已对齐 1)。
2. 版本策略：不硬性钉死（见 `kite-design.md` §10)；安装/启动时做版本
   检测，与已验证版本不符时警告但不阻止运行。
