# 决策：多实例（多租户）形态

> 状态：已决（2026-07-26，取代 kite-design.md §9 的"Phase 3 候选"登记，
> 与 `docs/decisions/concurrency-model.md` 互为补充）。
> 蓝本：FOCUS 的 `bot/instance_layout.py` 与 `bot/instance_resolution.py`。

## 问题

KITE 需要在同一台主机上为不同企业（飞书租户）各跑一个机器人：多个相互
独立的实例，各有自己的飞书应用、配置、绑定与 kap-server。MVP 的前提是单
实例；本决策定义多实例形态，以及它真正需要的并发机制。

## 决策

### 1. 实例布局（FOCUS 形态，零迁移）

```
<config root>/instances/<name>/   (配置：system.yaml、env、init.token、control.token)
<data root>/instances/<name>/     (数据：stores、日志、kap home、运行状态)
```

**默认实例保持今天的路径逐字节不变**(`~/.config/kite` +
`~/.local/share/kite`)——现有部署直接成为默认实例，零迁移。命名实例位于
`instances/<name>/`。实例名限于 `[a-z0-9][a-z0-9._-]*`，最长 64 字符
（与 FOCUS 一致；其他一律拒绝；禁用 `default`、`instances`、`..`)。

创建：`kitectl instance create <name>`（对标 FOCUS 的 `focusctl
instance create`）搭建实例——目录、由随包模板写入的 `system.yaml`
(0600，此后绝不覆盖）、每次运行都刷新的 `system.yaml.example` 参考副
本、0600 env 模板；`default` 搭建根实例。模板以 package data 随包分发
(`kite/install_template_data/`，经 `kite/install_templates.py` 加载，
源码检出内优先取仓库 `config/` 副本），安装后的部署无需回找源码树。
`install.sh` 委托同一 scaffold——默认流程搭建根实例，`--instance
<名称>` 搭建命名实例。写 service 定义仍是显式后续步骤
(`kitectl [--instance <名称>] service install`，设计 §9)。

### 2. 每实例隔离 kap home（撕裂杀手）

每个实例的 kited 以**隔离的 `KIMI_CODE_HOME`**（位于 `<data>/kap-home/`)
拉起自己的 kap 子进程（不再共享 `~/.kimi-code`):

- session 在物理上按租户隔离——多企业机器人的正确语义。
- 任何两个 kap-server 进程都不可能写同一个 session 目录：
  `docs/research/kap-server-usability.md` §4/§7 的"无跨进程会话锁"隐患
  在构造上消失。
- provider 配置来自各实例自己的 env 文件（`KIMI_MODEL_*` 覆盖层，spike
  已验证），无需逐 home 的 kimi 配置；某租户若想让 kap 与本地 kimi CLI
  共享状态，也可显式把 `kap.home` 指向真实的每租户 kimi home（风险自负，
  同裸 kimi 立场）。

### 3. 实例解析（kitectl)

`kitectl [--instance <name>] <command>`；解析阶梯（FOCUS 的
`instance_resolution.py`):

1. 显式 `--instance`（或 `KITE_INSTANCE` 环境变量）;
2. 恰好一个实例在运行时取该实例（经每实例 `control_plane.json` 元数据
   发现，过滤死 pid);
3. 默认实例。

歧义（多实例在跑且未显式指定）fail-closed 并列出候选。`kitectl service`
类命令只接受显式或默认实例（破坏性操作不走"单实例便利")。

显式指定的命名实例必须**已存在**（生效目录任一轴在盘上即算存在；对标
FOCUS 的 `require_existing_instance`):kitectl 与 kited 对未创建的实
例名一律 fail-closed，报错指向 `kitectl instance create`——打错名字
绝不静默搭建出一个空实例。检查发生在显式目录轴发布之后，因此
`--instance <名称> --data-dir <自定义>` 按生效目录判定。

阶梯第 2 级在"绝不可能是这个意思"的场景一律跳过（审查 N1-LOW)：除
`service` 外，实例无关命令（`completion`、`instance create`）不查
它；任何携带显式目录轴的调用——`--config-dir`/`--data-dir`，或预设
的 `KITE_CONFIG_DIR`/`KITE_DATA_ROOT`——同样跳过，因为调用者已经点
名了目录，不能再混入某个在跑实例的名字。kited 自身也从不走第 2
级：daemon 本身就是实例，经 `--instance`/`KITE_INSTANCE` 或默认实例
点名。

### 4. daemon 实例租约（真正需要的跨实例守卫）

两个 kited 绝不能驱动同一个实例（飞书会把机器人事件在两者之间负载均
衡，行为不一致）。kited 启动时对 `<实例数据>/kited.lock` 加**排他劝告式
文件锁**；第二个使用同一数据目录的 kited 立即退出并报告持锁者 pid。租约
放在数据目录而非配置目录：所有可变共享面都在数据目录
(`control_plane.json`、stores、`runtime_status.json`、`<数据>/kap-home`),
且按轴显式覆盖 `--config-dir`/`--data-dir` 可能让两个不同配置目录指向同
一数据目录（FOCUS 同样锁数据目录）。这是多实例真正需要的唯一跨进程协
调，不引入任何其他机制。

### 5. interaction owner：仍不实现（有意）

用户的方向包含"interaction owner 及其他配套"。分析记录于此，供日后重
议：

- FOCUS 的 owner 租约源于 codex app-server 的交互请求只有单一出口、必
  须有确定性路由。kap 把审批/question 变成了可查询的 REST 资源且响应幂
  等，实例内多 chat 已由 prompt 归属路由安全覆盖（见
  `docs/decisions/concurrency-model.md`——不变）。
- owner 要守卫的唯一跨进程会话共享向量已被 §2 消除（隔离 kap home——
  没有两个进程共享 session)。
- 其余共享面（每租户的飞书机器人身份、kap 的 per-prompt FIFO、审批幂
  等）分别由 §4 与上游语义覆盖。

因此 owner 租约继续作为**预留概念**(kite-design.md §4)，重议信号不变
（真实用户撞到它能解决的冲突；上游长出锁原语）。任一信号触发，实现起
点就在本节（FOCUS 的 `stores/interaction_lease_store.py` 与
`thread_runtime_coordination.py`)。

## 后果

- kite-design.md §9 的"多实例是 Phase 3 候选、需先补跨实例并发合同"由
  本文档解决。
- 新增配置/环境变量：`KITE_INSTANCE`;kitectl 新增全局旗标 `--instance`。
- `kap.home` 默认值仅对**命名实例**改为实例自己的 `<data>/kap-home`;
  默认实例保持 `~/.kimi-code`（其活状态已在那里）。
- 全程 fail-closed：非法实例名、租约冲突、解析歧义、实例级 provider 配
  置缺失，一律显式报错，绝不静默兜底。
