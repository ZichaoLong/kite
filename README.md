# KITE

> 说明：本项目是 **FOCUS**（飞书 ↔ Codex）的 kimi-code 对应实现——同一套桥接架构与工程纪律，上游从 `codex app-server` 换成 kimi-code 的 kap-server。

**KITE — Kimi-code In Threads, Everywhere** 把飞书机器人、本地 Web UI 和同一个
kimi-code 共享后端（kap-server）接到一起。

命名意象：kite 是一种鸟（鸢），与 Lark（云雀）同属鸟类主题；风筝靠一根线把空中
的它与地面连起来——这正是本项目做的事：把云端飞书会话系在本地 kimi-code 上。

本项目提供：

- 飞书里的 kimi-code 会话使用入口（单聊 + 群聊）
- 本地继续同一批 session 的 Web UI（kited 托管的 kap-server 直接提供）
- 本地查看 / 管理面 `kitectl`

你可以把它理解成一层桥接：

- 飞书会话先绑定到某个 kimi-code session
- 这个 session 跑在 KITE 实例自己托管的 kap-server 上
- 飞书与本地 Web UI 连的是同一个 kap-server：在飞书里发起的会话，可以在浏览器
  里打开同一个 session 继续操作，两边看到同一批上下文与终态
- 裸 `kimi` CLI 仍然可单独使用，但它使用自己的会话空间，不在飞书桥接合同内

## 使用入口

| 入口 | 作用 | 什么时候用 |
| --- | --- | --- |
| 飞书聊天命令 | 当前 chat 绑定的使用入口 | 在飞书里提问、切会话、改当前会话设置 |
| 本地 Web UI | 浏览器打开 kap-server 端口（默认 `http://127.0.0.1:58627`） | 想在本地继续飞书正在操作的同一 session |
| `kitectl` | 本地管理面 | 配置、启停、binding、session、prompt、image、定时任务、清理 |
| `kited` | daemon 入口 | 由 service manager 调用，通常不手敲 |

## 快速开始

### 前置条件

- Python 3.11+
- 本机已安装 kimi CLI，且 `kimi --version` 可正常执行
- 已在飞书开放平台创建自建应用，拿到 `app_id` 与 `app_secret`

### 1. 安装

```bash
cd /path/to/kite
bash install.sh
```

安装器会创建独立 venv、在 `~/.local/bin` 写入 `kited` / `kitectl` 包装命令，并
写入 OS service 定义（Linux 为 systemd --user `kite.service`；**只写入，不启动**）。
然后执行：

```bash
kitectl service start
```

如需登录后自动启动：

```bash
kitectl service autostart enable
```

不要使用 `pip install .` 或 `pip install -e .`——唯一支持的安装路径是仓库提供
的 install 脚本。

### 2. 配置飞书应用

推荐先一次性配好权限、事件与回调，再发布应用。

#### 权限

权限用途概览（标注"预留"的为与 FOCUS 对齐、供后续功能使用的权限，建议一并开通，
避免日后反复补充）：

- 机器人自识别（预留）：`application:application:self_manage`
- 发送者显示名解析（群通知、审批路由提示、`/whoami`）：`contact:user.base:readonly`
- 通讯录基础与工号展示（预留，FOCUS 同构回退链）：
  `contact:contact.base:readonly`、`contact:user.employee_id:readonly`
- 群名读取（预留，binding 列表群名缓存）：`im:chat:readonly`
- 接收消息（单聊 / 群内 @ / all 模式群全部消息）：
  `im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly`、`im:message.group_msg`
- 读取消息（assistant 模式历史回填、合并转发展开）：`im:message:readonly`
- 发送回复、更新执行卡/终态卡：
  `im:message`、`im:message:send_as_bot`、`im:message:update`
- 图片收发：`im:resource`

<details>
<summary>一键导入权限 JSON（点击展开）</summary>

在飞书开放平台「权限管理」页面点击「批量开通」，粘贴以下 JSON 即可导入当前建议
权限集：

```json
{
  "scopes": {
    "tenant": [
      "application:application:self_manage",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "im:chat:readonly",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.group_msg",
      "im:message.p2p_msg:readonly",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:message:update",
      "im:resource"
    ]
  }
}
```

</details>

#### 事件与回调

在「事件与回调」中启用：

- WebSocket 长连接模式
- 事件：`im.message.receive_v1`
- 事件：`im.message.recalled_v1`（用于撤回仍在队列中的消息）
- 事件：`im.chat.disbanded_v1`（群解散时停用该群激活状态）
- 事件：`im.chat.member.bot.deleted_v1`（机器人被移出群时停用该群激活状态）
- 回调：`card.action.trigger`（审批/问题按钮、执行卡取消按钮）

本项目默认走长连接，不需要公网 webhook URL。

配置完成后记得发布应用版本，权限与事件才会生效。

### 3. 本地启动、配置、初始化

编辑实例配置（首次安装后已生成模板）：

- `~/.config/kite/system.yaml`：填入 `app_id`、`app_secret`（其余字段均有默认值，
  全部可配项见仓库 `config/system.yaml.example`）
- `~/.config/kite/env`：按需填入 provider 环境变量（如 `KIMI_API_KEY`，0600 模板
  已生成）

改完配置后重启服务：

```bash
kitectl service restart
```

查看初始化口令：

```bash
kitectl config init-token
```

然后在飞书里私聊机器人：

```text
/init <token>
```

这一步会把当前发送者登记为管理员。非管理员默认不能直接使用机器人；`/whoami`、
`/init <token>` 这类身份与初始化命令仍可在私聊使用。

### 4. 开始使用

在飞书里：

- 发送 `/help` 看可用命令导航
- 直接发送普通文本开始对话（首次使用会自动创建并绑定新会话）
- 执行过程以执行卡呈现，终态以终态卡呈现；执行卡上有「取消执行」按钮，也可
  手动发送 `/abort`
- 用 `/new`、`/sessions`、`/switch` 管理会话；用 `/mode`、`/plan`、`/effort`
  调整当前绑定的权限模式、计划模式与思考强度；`/goal` 管理会话目标
- 图片、文件、合并转发消息可以直接发；转发后紧接着发一条文字说明，两者会合并
  为一条 prompt（说明在前还是内容在前由认领语义保证，指令不会跑在内容之前）
- 群聊里管理员先用 `/group activate` 激活，再用 `/group-mode` 选择
  `mention_only` / `assistant` / `all` 三种模式
- 如果想让同一个机器人同时服务多个项目，建议为每个项目单独建一个群聊；每个
  群聊固定绑定自己的会话，避免在单聊里反复 `/switch`

在本地继续同一批 session：

- 浏览器打开 `http://127.0.0.1:58627`（`kap.port` 可在 system.yaml 调整），就是
  kited 托管的同一个 kap-server Web UI——飞书里的会话在这里可见、可继续
- 远程使用时可 `ssh -L 58627:127.0.0.1:58627 <host>` 做本地端口转发

本地查看 / 管理：

```bash
kitectl service status
kitectl service log
kitectl binding list
kitectl session list
kitectl prompt send --chat <chat_id> --text "..."
kitectl image send --chat <chat_id> --path ./diagram.png
kitectl schedule create --chat <chat_id> --cron "9 * * * 1-5" --text "..."
kitectl interaction sweep
```

#### 可选进阶

- `kitectl prompt send` 可从脚本/cron 向某个既有飞书会话合成发起一轮 prompt；
  `kitectl schedule create` 则是它的定时包装（systemd --user timer，macOS/Windows
  分别走 launchd / Task Scheduler），适合做例行任务。
- `kitectl interaction sweep` 清理上游残留的过期审批/问题，适用于重启后或长时间
  运行时的卫生检查。
- shell 补全：`eval "$(kitectl completion bash)"`（zsh/PowerShell 同样支持，
  见 `kitectl completion --help`）。

### 5. 多机器人多实例

如果你希望配置多个飞书应用及机器人（例如不同企业/团队各一个），每个机器人对应
一个 KITE 实例：

```bash
bash install.sh --instance corp-a        # 只创建该实例的目录与 env 模板
# 编辑 ~/.config/kite/instances/corp-a/system.yaml 与 env
kitectl --instance corp-a service install
kitectl --instance corp-a service start
```

每个实例有自己的：

- 配置目录（`~/.config/kite/instances/<name>/`）
- 数据目录（`~/.local/share/kite/instances/<name>/`）
- service 与独立托管的 kap-server（端口在各自 system.yaml 的 `kap.port` 配置）

所有实例共享本机的 `kited` / `kitectl` 命令；`kitectl --instance <name> ...` 指定
目标实例，不显式指定时按「KITE_INSTANCE 环境变量 → 唯一在跑实例 → 默认实例」
解析。

## 更多帮助

- 飞书里发送 `/help`
- 本地查看 `kitectl --help`（以及各子命令的 `--help`）
- 深入文档看 `docs/doc-index.zh-CN.md`（架构、功能合同、决策记录）

## 当前状态

主链路已实网联调通过：单聊/群聊对话、执行卡/终态卡（含流式更新）、审批与问题
卡片、`/abort` 与取消按钮、重启恢复、图片/附件、定时任务、多实例。测试与文档
一致性检查随 CI 运行。
