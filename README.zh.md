# SlackAgentTeam (multi_app)

**语言：** [English](README.md) · [日本語](README.ja.md) · [中文](README.zh.md)

支持本地 Claude Code、Codex CLI 与 OpenAI Responses API 的多 Agent Slack 机器人。每个 agent 是独立的 Slack App（各自 bot 身份与 token）。支持 @ 提及驱动的交接、回合预算防循环、上下文续接，以及通过 GitHub Issues + push 的多机协作。

## 架构

多个 agent 在同一 Python 进程内以 Socket Mode 并行运行。频道中仅被 `@` 的 agent 会发言；回复中的相互 `@` 可完成交接。纯逻辑（发送者分类、启动判定、回合预算、去重、提示词）在 `multi_core.py`；Slack / Claude / Codex 接线在 `multi_app.py`。

## 文件一览

| 文件 | 作用 |
|------|------|
| `multi_app.py` | 多 agent 入口：配置加载、Roster、SlackAgent、Socket Mode |
| `multi_core.py` | 纯逻辑层（不依赖 slack/claude 包） |
| `state_store.py` | SQLite 线程状态持久化（会话/交接摘要跨重启保留） |
| `issue_claim.py` | 宿主侧 GitHub issue 认领工具（v2 租约：`claim` / `renew` / `release` / `verify`） |
| `agents.yaml` | agent 定义（`card` / persona、所有者、节点、项目） |
| `agents.distributed.example.yaml` | 两人 × 每人两个本地 agent 的分布式示例 |
| `slack-app-manifest-agent.yaml` | 每个 agent 一份的 Slack App manifest 模板 |
| `.env.example` | 环境变量模板（多 agent token） |
| `webui.py` | 本地控制台（agent CRUD、Slack App 向导、实时监控、热切换） |

## 角色

每个 agent 在 `agents.yaml` 里有两层人设：

- **`card`（L1，可选）** — 给队友看的接口卡（何时找我 / handoff 带什么 / 我交付什么 / 什么别找我）。出现在 `!roles`、各 agent system prompt 的队友段、webui。建议 ≤6 行 / 400 字（仅警告）。
- **`persona`（L2）** — 仅自己可见的行为约束，可写很长；只注入本人 system prompt。
- **回退：** `card` 缺省/空 → 用 `persona` 首行（与旧行为一致）。`defaults.card` 会被忽略。可热更新，无需重启。

| name | 职责 | optional | Token 环境变量 |
|------|------|----------|----------------|
| `dev` | 实现 / 修复 / 冒烟验证；完成后按需 `@reviewer` / `@qa` | 否 | `DEV_SLACK_BOT_TOKEN` / `DEV_SLACK_APP_TOKEN` |
| `reviewer` | 代码审查；LGTM 或具体意见；需修改时再 `@dev` | 否 | `REVIEWER_SLACK_BOT_TOKEN` / `REVIEWER_SLACK_APP_TOKEN` |
| `pm` | 需求整理与任务拆分；一次只 `@` 一个；向人类汇报（不写代码） | 是 | `PM_SLACK_BOT_TOKEN` / `PM_SLACK_APP_TOKEN` |
| `qa` | 测试视角；跑 pytest 等；失败时带复现步骤 `@dev` | 是 | `QA_SLACK_BOT_TOKEN` / `QA_SLACK_APP_TOKEN` |
| `designer` | UI/UX 方案与评审；规格明确后交给 `@dev` 实现 | 是 | `DESIGNER_SLACK_BOT_TOKEN` / `DESIGNER_SLACK_APP_TOKEN` |
| `dx` | 线程交通管制与 agent 健康（`!status` / `!reset`）；只读工具 | 是 | `DX_SLACK_BOT_TOKEN` / `DX_SLACK_APP_TOKEN` |

`dx` 的 `allowed_tools` 仅限 `[Read, Glob, Grep]`。

## Runtime：claude / codex / openai

每个 agent 可选择执行引擎（`runtime` 字段：agent → defaults → `claude`）：

```yaml
agents:
  - name: dev
    runtime: codex          # 该 agent 使用 Codex CLI
    codex_sandbox: workspace-write   # read-only | workspace-write | danger-full-access
    # codex_model: gpt-5.2-codex     # 省略 = CLI 默认模型
  - name: reviewer          # 省略 → claude
  - name: planner
    runtime: openai
    openai_model: gpt-5.6-sol
    openai_api_key_env: ALICE_OPENAI_API_KEY  # 这里只写环境变量名
  - name: grok-reviewer
    runtime: openai
    openai_model: grok-4.5
    # 指向本机 OpenAI 兼容代理（CLIProxyAPI / Antigravity 桥）
    openai_base_url: http://127.0.0.1:8317/v1
```

| 项目 | claude | codex | openai |
|------|--------|-------|--------|
| 执行 | claude-agent-sdk（进程内） | 本机 `codex exec --json` 子进程（需先 `codex login`） | OpenAI Responses API（官方或 CLI Proxy） |
| 线程连续性 | session resume | thread resume | `previous_response_id` |
| System prompt | SDK 选项 | 新会话时前置 | 每次作为 `instructions` 发送 |
| 本机工具 | Claude Code 工具 | Codex 本机沙箱 | **无**；仅文本分析、评审和 handoff |
| 超时 / 预算 | `claude_timeout` 与回合预算适用全部 runtime | 同左 | 同左 |
| 模型 | `claude_model` | `codex_model` | `openai_model`（默认 `gpt-5.6-sol`；可填任意 id，如 `grok-4.5`） |
| 凭据 | 本机 Claude 登录/API key | 本机 Codex 登录 | 本机环境变量，默认 `OPENAI_API_KEY` |
| 端点 | — | — | 官方 OpenAI，或 `openai_base_url` / `OPENAI_BASE_URL` |

模型与 runtime 可在 webui「监控」页面对**运行中进程热切换**（确认对话框；下一回合生效；会写回 `agents.yaml`，重启后保持）。切到 `openai` 前，对应 Key（或本机 CLI Proxy 的 `openai_base_url`）需在 client 构建时可用；若启动后才补 Key / base URL，需要重启一次。

### 本机 CLI Proxy（Grok / Antigravity 等）

`openai` runtime 可对接任意 **OpenAI 兼容** Responses 端点，包括本机 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)：

```yaml
defaults:
  runtime: openai
  openai_base_url: http://127.0.0.1:8317/v1   # 或在 .env 设 OPENAI_BASE_URL
  openai_model: grok-4.5
  openai_api_key_env: OPENAI_API_KEY          # 若 proxy 要求客户端 key
```

- `openai_base_url`（agent → defaults → `OPENAI_BASE_URL`）将请求从 `api.openai.com` 改到本地代理。
- `openai_model` 为自由文本：`grok-4.5`、proxy 暴露的 Antigravity/Gemini 模型 id 等。
- 仅配置 base URL、未设真实 key 时，会使用本地占位 key 以便 SDK 启动；若 proxy 的 `api-keys` 校验客户端，请把对应值写入 `OPENAI_API_KEY`。
- WebUI 模型下拉会预置常见名称，并在 proxy 可用时合并 `GET {base_url}/models`；监控页也可手动输入任意模型 id。

OpenAI runtime 使用 Responses API。共享 YAML 只记录
`openai_api_key_env` 的变量名（以及可选的 `openai_base_url`），Key 本身只从本机 `.env` 读取，启动后从父进程
环境清除，也不会写入 Slack、SQLite 或远端 roster。Responses API 会接收
Slack prompt/context，并使用存储的 response id 续接线程；如需要读取或修改
本机仓库，请 handoff 给 Claude/Codex runtime。

## 状态持久化

每线程状态（会话 resume id、交接摘要、token 统计、last-seen）会写穿到 SQLite——`STATE_DB`，默认 `state.db`；仅在未配置有限 owner quota 时才可置空禁用——启动时自动恢复，重启不再"失忆"。配置有限 quota 后，`STATE_DB` 为空或无法打开会使启动失败。状态按已认证的 Slack team id 与 project id 隔离，不会静默恢复另一个/未知 Slack workspace 的会话，也不会在频道改绑项目后恢复旧项目会话；旧数据库会安全迁移，旧行保留在空的 legacy scope。未配置 projects 时继续使用稳定的 team-only scope 以保持兼容。resume id 与引擎/工作目录绑定：runtime 或 workspace 变更后 id 会被丢弃，改由摘要交接。`!reset` 会连同持久化行一起删除；闲置超过 48h 的行会被清理。往复预算刻意不持久化：频道中获准人类提及本项目可用 agent，或向其发送合法 structured handoff 时会重置；DM 无需 `@`。访客、普通闲聊、unknown/project 外 target 均不会重置。Docker 下 DB 存放在 `agent-state` volume。

`StateStore` 会在整个生命周期独占 `${STATE_DB}.lock`。第二个节点若误指向
同一数据库会直接启动失败，不会静默共享会话。DB、lock、WAL、SHM 均为私有
权限；每个 owner/node 必须使用独立的 state 路径或 volume。

## 多 agent 共用的本地优先转录

同一 `multi_app` 进程里的全部本地 agent 共用一个有界转录库，键为 Slack
team、channel、thread。root/reply/edit/delete 事件会在去重、项目路由、权限检查和
activation 提前返回之前写入，因此每人本地的 2–3 个 agent 不会重复拉同一线程。
消息按 Slack 时间戳排序，只保存上下文分类所需字段；当前 roster、项目 ACL、
人类 allowlist 与 guest/feed 标签在读取时应用，不会在写入时固化权限。

正常上下文热路径只读本地：看到 live root 后即可 context-warm，对
`conversations.replies` 发起 **0 次**调用，但 live observation 本身不等于
canonical authority。canonical handoff 超过最近一次完整 snapshot 的 watermark
时，会触发一次进程共享的同线程回填。cold/incomplete thread 同样使用
singleflight 回填，
使用 cursor 分页且 `limit=15`；重启恢复的线程每个进程先从 root 做一次完整
远端校验（不使用 `oldest` 捷径）才取得 authority。同一 boot 内因内存 LRU
重新载入会沿用该 boot 的校验；新 boot 则把持久记录视为未知，直到完成一次
完整校验。只有 terminal cursor 且完整
snapshot 中确实存在 root 才能标记 complete。成功 snapshot 会更新离线期间的
edit，并把远端已不存在的旧持久消息写为 tombstone；逐消息 compare-and-swap
保证回填期间到达的 live new/edit/delete 胜出。失败、缺 root、coverage 有歧义
或 HTTP 429 时继续返回本地 partial suffix，不做缺失消息删除，遵守
`Retry-After`（否则用配置的 cooldown），不会循环重试。重启后的完整校验最坏
需要一次性分页调用 `ceil(thread 消息数 / 15)` 次；在每分钟 1 次的降级 tier 下，
长 cold thread 可能需要数分钟，但此后 warm activation 仍为 0 次 history API。
每条记录默认最多 64 KiB；过大的结构化字段会被丢弃，正文按 UTF-8 安全截断。
任何可能影响 canonical 顺序的截断记录，即使远端回填成功，也会让 snapshot
失去 authority。canonical 分布式 handoff/预算对 partial history 一律不
fail-open；若旧消息因条数/时间容量被截断，只有
当前策略认定的 driving-human reset 晚于截断边界时，后缀才具备 authority，
否则 fail-closed。

Slack 降级限流的精确条件是：从 **2025-05-29** 起新建或新安装、商业分发且
未上 Marketplace 的 app，`conversations.history` 与
`conversations.replies` 为 **每分钟 1 次、每次最多 15 条**。Marketplace
app 与客户内部自建 app 仍为 Tier 3；既有的站外分发安装不受该已公布降级限制。
参见 Slack 官方
[`conversations.replies` 文档](https://docs.slack.dev/reference/methods/conversations.replies/)
和[限流公告](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/)。
因此架构不依赖每次 activation 或每个本地 peer 都调用历史 API。

| 配置 | 默认值 | 上限含义 |
|------|--------|----------|
| `TRANSCRIPT_MAX_THREADS` | `512` | 每进程内存 LRU 线程数 |
| `TRANSCRIPT_MAX_MESSAGES_PER_THREAD` | `50` | 每线程在内存和 SQLite 保留的最新记录数 |
| `TRANSCRIPT_DB_MAX_THREADS` | `2048` | SQLite 转录线程数；不会删除 agent/session 行 |
| `TRANSCRIPT_MAX_RECORD_BYTES` | `65536` | 每条规范化记录的逻辑字节数（最小 256 B，硬上限 1 MiB） |
| `TRANSCRIPT_MEMORY_MAX_BYTES` | `33554432`（32 MiB） | 进程内转录总字节数（硬上限 512 MiB） |
| `TRANSCRIPT_DB_MAX_BYTES` | `268435456`（256 MiB） | SQLite 转录逻辑字节数（硬上限 4 GiB） |
| `TRANSCRIPT_TTL_SECONDS` | `604800`（7 天） | 转录保留时间 |
| `TRANSCRIPT_RETRY_COOLDOWN_SECONDS` | `60` | 回填失败后的最短重试间隔 |

内存与 SQLite 字节上限都必须不小于单记录上限；无效或倒置的配置会在启动时
报错，错误信息不包含消息内容。

`/state` 与 WebUI 只显示内存/持久消息及字节容量、完整/partial、淘汰、
回填调用与失败等聚合计数，绝不输出消息正文、用户内容或凭证。

## 回复语言

`reply_language`（agent → defaults → `日本語`）会注入 system prompt。  
当前 `agents.yaml` 默认常为 `English`；可按需设为 `中文` / `日本語` / `English`。  
webui 监控页也可按 agent **热切换**（下一回合；写回 `agents.yaml`）。

## 控制台（Web UI）：配置 + 监控 + 热切换

```bash
make webui        # = .venv/bin/python webui.py → http://127.0.0.1:8765
```

四个标签页：

**指引**（首次访问默认页）

- 五个有顺序的阶段：分配角色 → 创建 Slack Apps → 连接本机私有账号 → 编写职责 Prompt → 跑通第一次任务/交接
- 上手勾选进度只保存在当前浏览器，不上传服务器
- 可直接复制频道规则、人类任务、结构化 `HANDOFF` 与 dev/reviewer/planner persona 模板
- Agent 职责表明确区分本地工具 Agent 与没有本地文件工具的 OpenAI Agent
- 只有进入受保护标签页时才询问控制 Bearer；静态指引本身无需凭据

**监控**（数据来自运行中的 `multi_app` 管理 API）

- **跟踪的 issues**：各 agent 有效仓库中的 open GitHub issue（编号 / 标题 / status 标签 / 经办人）；30 秒缓存；需本机 `gh` 已登录
- 顶部状态：在线 / 离线、agent 数、忙碌线程、会话、连接
- 每个 agent「节点」：状态条、runtime·模型、会话 / 巡检、近期线程（忙碌 / tokens / 剩余预算 / 空闲）
- **热切换模型**（带确认）：对运行中进程下一回合生效，无需重启
- **热切换 runtime**（带确认）：claude / codex / openai；会丢弃不兼容会话
- **回复语言**下拉（中文 / 日本語 / English）：下一回合生效
- **Reasoning effort** 按 runtime（openai: none…max；claude: low…max；codex: minimal…xhigh）；空 = 引擎默认
- **会话重启**：清空该 agent 全部会话（摘要保留供交接）
- 模型列表来自真实引擎配置（`~/.claude/settings.json`、有 key 时 Anthropic API、`~/.codex/config.toml`）
- **UI 语言** 中 / 日 / EN（浏览器本地；与 agent 的 `reply_language` 无关）
- 每 5 秒自动刷新（确认对话框待处理时暂停）

**配置**（写入 `agents.yaml` / `.env`；保存时自动热加载到运行中的 `multi_app`——新增/删除 agent 仍需重启）

- Agent 列表、token 状态（bot / app）、runtime、职责摘要
- **设置向导**：为每个 agent 生成 manifest → 复制 → 在 api.slack.com 创建 → 粘贴 token → 校验（`auth.test` + `apps.connections.open`）→ 写入 `.env`
- **编辑 persona** / **添加 agent**

**认证**用于验证 Agent 实际运行环境中的 Claude、Codex 与 GitHub 登录
（有 Docker 时优先容器，否则本机）。每个 owner 只连接自己的账号。

Webui **仅绑定 127.0.0.1**（Host 头校验防 DNS rebinding）。实时数据来自管理 API（`ADMIN_BASE`，默认 `http://127.0.0.1:8766`）。owner/分布式配置自动要求环境变量 `SLACK_AGENT_CONTROL_TOKEN_<SLACK_USER_ID>` 中的独立 Bearer（至少 32 个随机可打印字符）。浏览器只存入 `sessionStorage` 并发送 `Authorization: Bearer ...`，任何 user-id header 都不被信任。owner 只能查看/修改自己的 agent，top-level admin 可管理全部，全局 reload 仅 admin 可用；只有 `/healthz` 保持匿名。保存会生成 `.bak` 备份（已 gitignore）；`agents.yaml` 中的 YAML 注释会在保存时丢失。

## Slack App 创建（手动）

（使用 Web UI 向导时可跳过。）每个 agent 需要**独立的 Slack App**。

1. [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. 粘贴 `slack-app-manifest-agent.yaml`；每个 App 改三处：
   - `display_information.name`（如 `Agent (dev)`）
   - `features.agent_view.agent_description`
   - `bot_user.display_name`（如 `dev` / `pm`）
3. **Install to Workspace**
4. Token：
   - **Bot User OAuth Token**（`xoxb-...`）
   - **App-Level Token**（`xapp-...`，scope `connections:write`）
5. 写入 `.env`：`{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN`
6. 对每个 agent 重复

Manifest 含 **`agent_view`**（Agent 消息体验）。需要 `slack-bolt>=1.29.0`。

### optional 语义

- `optional: true`（pm / qa / designer / dx）：缺 token → **警告并跳过**，进程继续
- 配好 token 后重启即可加载
- 非 optional（dev / reviewer）缺 token → `RuntimeError`
- 全部 agent 被跳过 → `RuntimeError`（无有效 agent）

### Scope

| 分组 | Scope | 说明 |
|------|-------|------|
| 核心 | `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history` | 收发 / 回复 / 线程上下文 |
| Agent UX | `assistant:write` | AI 侧栏 / assistant 线程 |
| 信息 | `channels:read`, `groups:read`, `im:read`, `im:write`, `mpim:read`, `mpim:write`, `users:read` | 频道成员（`conversations.members`）/用户元数据 |
| 表情 | `reactions:read`, `reactions:write` | 触发消息状态（⏳ 处理中 → ✅ / ❌） |
| 文件 | `files:read`, `files:write` | 附件入库需 `files:read`；write 可选 |
| 定制 | `chat:write.customize` | 第二阶段动态人设；可选 |

**变更 scope/manifest 后必须 Reinstall to Workspace**，否则新 scope/事件不会生效。

### Slack 原生增强

- `agents.yaml` 中 `trusted_feed_bots: [B0XXXXXXX]`：白名单 Slack App（如 GitHub）消息作为只读 `[feed]` 上下文，**不会**激活 agent。
- 触发消息上的人类附件会下载到 `<workspace>/.slack-files/`（最多 3 个、单文件 10MB；48h 后清理），供 agent 本地工具读取。
- 状态用触发消息上的 reaction 表示（📥 线程忙或节点满而排队 → ⏳ 处理中 → ✅ 完成 / ❌ 失败 / 🤐 新鲜度复查后撤回）；不再发「处理中…」占位消息。provider 冷却等待超过 30 秒时，会在线程里发一次预计恢复时间。
- 失败会按类别给出下一步（上下文过长 → `!reset`；认证、计费 → 节点所有者）。巡检同类失败连续 3 次即暂停并在频道通知一次，之后每 6 轮试一次，成功即自动恢复（`/state` 的 `patrol_fence` / `last_failure`）。
- 重启不再让 ⏳ 永久挂着：已受理的激活会记录到 state DB（`activation_ledger`），启动时对上一进程没跑完的激活贴 ⚠️，并在线程里提示请求者检查后重新 @。不会自动重跑，因为被中断的回合可能已经 push 或发过评论。
- 同一线程中排队的多个触发（例如处理中又收到「@dev 加 X」「@dev 还要 Y」）会按时间顺序合并成一轮回复，不会再跑第二轮去重复回答第一轮已在上下文中看到的内容。
- 回复里用纯文本 `@name` 写的已注册 agent 不会收到通知，帖子会附一行提醒，避免交接静默中断。

## .env

见 `.env.example`。最低要求：必需 agent；可选按需添加：

```bash
# 必需
DEV_SLACK_BOT_TOKEN=xoxb-...
DEV_SLACK_APP_TOKEN=xapp-...
REVIEWER_SLACK_BOT_TOKEN=xoxb-...
REVIEWER_SLACK_APP_TOKEN=xapp-...

# 可选（未设置则启动时 skip）
# PM_SLACK_BOT_TOKEN=...
# QA_SLACK_BOT_TOKEN=...
# DESIGNER_SLACK_BOT_TOKEN=...
# DX_SLACK_BOT_TOKEN=...

# 强烈建议：允许的人类 Slack 用户 ID 白名单
ALLOWED_SLACK_USERS=U01ABCDEF

# 可选：agents.yaml 未写 workspace 时的 Claude 工作区
CLAUDE_WORKSPACE=/path/to/project
```

可在 `agents.yaml` 用 `bot_token_env` / `app_token_env` 覆盖环境变量名。

## 启动

### Docker（推荐）

```bash
cp .env.alice.example .env.alice   # 只在 Alice 的机器填写 Alice 的值
NODE=alice make up                 # 只启动 Alice 节点
NODE=alice make logs
NODE=alice make restart
NODE=alice make down
NODE=alice make test-docker
make help
```

Compose 说明：

- **每人一个 profile**：Bob 使用 `.env.bob` 与 `NODE=bob`；不要在本机启动
  其他人的 profile。
- **凭证**：每个 profile 都有独立的 Claude/Codex/gh volume 与 env 文件，
  不打进镜像，也不挂载另一位 owner 的认证目录。
- **状态/工作区**：Alice 与 Bob 使用不同的 state/workspace volume；唯一共享挂载
  是只读、无凭据的 `roster.yaml`。
- **隔离**：容器最终使用非 root `agent` 用户；管理端口仅绑定 localhost
  （Alice 8766、Bob 8767）。

### 本地 venv

```bash
.venv/bin/python -m pip install -r requirements.txt
make run     # = .venv/bin/python multi_app.py
make test
make webui
```

启动日志会打印 roster（name / user_id / workspace）。无 token 的 optional agent 会打 skip 警告。

## 用法

1. 将 bot 邀请进共享频道（至少 `dev`、`reviewer`）
2. 例：`@dev 在 foo.py 实现 bar()，然后让 reviewer 检查`
3. `dev` 完成后 `@reviewer`；`reviewer` 被提及后启动
4. 有 `pm` 时先 `@pm` 拆任务
5. 获准人类 `@` 本项目可用 agent 或发送合法 structured handoff 时会**重置**交接预算（DM 无需 `@`）

DM 中人类可无 `@` 对话（peer 不会在 DM 中交接）。

## 上下文自动管理

当 agent 最近一轮输入上下文（input + cache tokens）超过阈值时：

1. **摘要**（无工具一轮，≤800 字交接摘要）
2. **切换**会话（丢弃 Claude session_id）
3. **交接**在下次启动时注入摘要

| 项目 | 说明 |
|------|------|
| 阈值 | `context_rollover_tokens`，默认 **60000** |
| 解析顺序 | agent 字段 → `defaults` → `60000` |
| 无 usage | 按 0 token；**不** rollover |

## 可靠性

| 机制 | 说明 |
|------|------|
| 硬超时 | `claude_timeout`（默认 **900** 秒）；释放锁并通知线程 |
| Slack 429 | `AsyncRateLimitErrorRetryHandler`（Retry-After，最多 2 次重试） |
| 内存回收 | 线程状态 idle **48h** 后回收（约每 10 分钟扫描） |
| 长线程 | 共用有界本地转录；仅 cold/incomplete 线程用 cursor、每页最多 15 条回填。上下文需要截断时保留线程根消息（通常是任务定义），从中间省略 |
| 回复新鲜度门控 | 发帖前检查生成期间线程内新到的同事/授权人类消息，触发恰好一次重新判断（`POST_ORIGINAL` 原样发 / 修订全文 / `NO_REPLY` 撤回）；与最新非本人消息逐字重复的回复不会发出。只读本地转录（零额外 Slack API 调用），门控自身出错时 fail-open 照常发帖。`FRESHNESS_RECHECK=0` 可关闭 |
| Provider 限流冷却 | AI 轮次被限流时（Claude / Codex / OpenAI 的 429、用量上限、overloaded 信号；尊重 `Retry-After`）武装按 provider 账号共享的冷却（本机所有 Claude agent 共用一个 Claude 登录、所有 Codex agent 共用一个 Codex 登录；OpenAI 按端点 + key 变量区分）——基础 **60 秒**，连续触发翻倍至上限 **480 秒**。冷却结束后只在确认安全时重试一次：未执行有副作用的工具（shell、编辑、MCP）、provider 给出的恢复时间在 15 分钟内、线程未被重置；否则不静默重放，而是在线程里说明原因。冷却等待期间会让出节点并发槽，其他 agent 不受影响。冷却期内开始的轮次先等待；巡回轮直接跳过（下个周期自然重试）。成功轮次重置连击。状态见 `/state` 的 `provider_cooldown`；用 `PROVIDER_COOLDOWN_BASE_SECONDS` / `PROVIDER_COOLDOWN_MAX_SECONDS` 调整 |
| 自适应轮次节拍器 | 全部本地 agent 共享一条节点级的 provider 轮次启动时间线，按自适应间隔错开——基础 **0.5 秒**，每次限流翻倍至上限 **8 秒**，连续 **5** 个清洁轮次后减半回落——让共用同一 provider 账号的 2-3 个 agent 永不同拍开火。覆盖 Slack、新鲜度复查与巡回轮次；等待发生在单轮超时窗口开启之前，不降低总并发。状态见 `/state` 的 `turn_pacer`；用 `PROVIDER_PACER_BASE_SECONDS`（0 为关闭）/ `PROVIDER_PACER_MAX_SECONDS` / `PROVIDER_PACER_CLEAN_TURNS` 调整 |
| Socket 投递缺口 | Socket Mode 断线（或主机休眠）超过 **120 秒**可能已错过 Slack 的重投窗口，共享转录会在下次读取时从线程根消息重新校验，不再把缺口当作完整上下文。用 `SOCKET_GAP_REVALIDATE_SECONDS` 调整 |

## 安全

| 机制 | 说明 |
|------|------|
| 凭证隔离 | 加载后从进程环境 scrub 所有 `*_SLACK_BOT/APP_TOKEN`（Claude 子进程继承 `os.environ`）；`.env` 权限 0600 |
| 上下文授权过滤 | self / 当前频道 peer / 获准人类保持普通上下文；无 authority 的人类仅作为 continuation-safe 的 `[guest]` 信息，unknown bot/system 仍排除 |
| 状态库权限 | `STATE_DB`（默认 `state.db`）保存 session 状态与有界本地转录。目录 `0700`，DB/WAL/SHM `0600`。视同敏感数据，勿在不可信用户间共享文件或 volume |

残余风险：agent 的 Bash 与 OS 用户相同，仍可能读盘上的 `.env` 与状态库。请轮换 token；生产建议用独立 OS 用户/容器。

### 运维命令

对目标 agent `@` 后跟命令（**不**消耗回合预算；**不**触发正常启动）：

| 命令 | 效果 |
|------|------|
| `!status <@agent>` | 会话 / 近期 tokens / 回合 / 交接摘要 / 剩余预算 |
| `!reset <@agent>` | 完全重置该线程的会话、统计与摘要 |
| `!roles <@agent>` | 当前频道 agent roster + 职责摘要（`card`，空则 persona 首行） |

示例：`!status <@U_DEV>`。主要给人与 `dx` 用。

## 频道指引（topic / purpose）

启动时 agent 会读取频道 **topic** 与 **purpose**（5 分钟缓存），作为运维规则放在 prompt 顶部。请把规则写在频道 topic/描述里。

## 协作摩擦控制

| 控制 | 行为 |
|------|------|
| 一消息一请求 | 每条消息只交接一次；禁止多 agent 同任务刷屏 |
| 不复述 | 不重复线程里已知事实 |
| 无注水回复 | 禁止纯确认闲聊；回复要有信息量 |
| 回合预算 | `budget.max_agent_rounds`（默认 **12**） |
| dx 交通管制 | 出现循环 / 空转闲聊时简短纠正 |

## 防循环

| 机制 | 行为 |
|------|------|
| 被 `@` 才发言 | 公开/私有频道要求 `<@user_id>` 提及 |
| 回合预算 | 每线程 agent→agent 交接上限 |
| 人类重置 | 获准人类 `@` 本项目可用 agent，或指定合法 structured target；获准人类 DM 无需 `@` |

预算用尽时，每个线程只发一次暂停通知，直到获准人类发送驱动消息。访客及普通频道闲聊不会恢复预算。访客消息仍以只读 `[guest]` 上下文可见，但不能启动/reset、执行命令、授权 handoff，也不能向 agent 下指令。

## 常见陷阱

1. **改 scope 后必须 reinstall**，否则 history/context API 会静默失败（代码降级为空上下文）。
2. **bot 之间的 `@` 不保证触发 `app_mention`**——启动判定在 `message` 事件上。
3. **Token 环境变量名** 默认为 `{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN`。
4. **ALLOWED_SLACK_USERS** 为空 = 任何人可触发；生产请设白名单。
5. **工作区权限**——Edit/Write/Bash 会改本机；收紧 workspace 与白名单。
6. **`slack-bolt` ≥ 1.29.0** 才能用 `agent_view`。
7. **远端 agent 被识别为 unknown bot**——每台主机都必须配置其 `slack_user_id` 与 `slack_bot_id`。

## 多机部署

采用 [`roster.yaml`](roster.yaml)、[`agents.alice.yaml`](agents.alice.yaml)
与 [`agents.bob.yaml`](agents.bob.yaml) 的“Slack 协调、本地执行”方式。
所有主机只共享同一份无凭据 roster；每个节点只使用本人的 local runtime 文件与
env 文件。`roster.yaml` 是身份/路由事实源，仅包含 name、Slack user/bot id、
owner、node、card 与项目 ACL；token/env、workspace、runtime、GitHub 与 tool
字段一律拒绝。本地文件只列本人 2–3 个 agent 的 runtime 与凭据变量名。

每个人只保存本人 agent 的 Slack token。Claude/Codex 登录、OpenAI key、GitHub
认证、workspace 与 SQLite 全留在本人节点。跨节点协作与上下文传递**只通过
Slack 消息/handoff**；AI 账户、登录目录、本地 session、文件系统和数据库均不共享。
每个逻辑 agent 必须声明规范的 `owner: U...`；每台主机必须设置本机 owner
的控制 Bearer。top-level admin Bearer 按节点可选，只在明确安装它的节点授权，
无需在所有人的主机间复制一个团队共享 token。owner token 缺失、已安装 token
过短或重复都会使启动失败。

共享 roster 还可按 owner 配置 UTC 日 token 配额：

```yaml
quotas:
  daily_total_tokens:
    U01ALICE: 250000
    U02BOB: 250000
  reservation_tokens:
    U01ALICE: 20000
    U02BOB: 20000
```

同一人的 2–3 个本地 agent 共用一份账本，并且必须属于同一个 `node_id`；shared
roster 若把同一 owner 拆到多个节点会被拒绝。每轮在占用 node/queue 前，先用
SQLite `BEGIN IMMEDIATE` 原子预留；只有
`已使用 + 活跃预留 + 新预留 <= 每日上限` 才允许启动。queue full、task 创建失败、
provider 启动前取消都会释放预留。provider 已启动的轮次按其精确 usage 结算，并把
output token 纳入总量。精确 usage 可能高于预留，因此最多允许刚完成的这一轮造成
超额，后续轮次会被拒绝。已启动但报错或 usage 不完整时，至少按预留量保守结算，
并标记 `estimated`/`error`。重启恢复时，未启动的 stale 预留释放，已启动的 stale
预留保守结算。

配额统计中的 `input_tokens` 包含 cache 输入，`output_tokens` 始终计入每日总量，
`cache_tokens` 另存为 input 的诊断子集。Claude 分别报告 direct、cache read 与
cache creation；Codex 分别报告 direct 与 cached input；OpenAI Responses 的
`input_tokens` 已包含 cached token，因此不会重复相加。缺少 output usage 时按估算
处理，不会当作 0。`/state` 与 WebUI 显示 limit、used、remaining、active reserved、
denied、errors 及 agent/runtime 下钻，且不包含 prompt 或消息正文。owner 只能看到
自己的账本；安装了 bearer 的 top-level admin 可看到该节点全部 owner。

启动时注册所有逻辑 agent（含远端 Slack user/bot id、owner、node 和 card），
但只有本节点且 token 完整的 agent 才启动 Socket Mode 与 AI runtime。因此
远端 bot 的 @交接会被识别为 peer，同时不共享 AI 账户。

远端 agent 的新增/删除/card/owner 变更会热替换内存路由，不重连本地 Slack
socket。本地 Slack identity、owner 或 node 的变化只标记需重启，并在重启前保留
已认证身份；无效或冲突的 roster 快照不会产生任何修改。

每台主机需要：

1. Python 环境（`.venv` + `requirements.txt`）
2. 本人的 `claude` / `codex` 登录或 `OPENAI_API_KEY`；如使用 GitHub，再用本人的 `gh auth login`
3. 每个可写 agent **独立 clone**，并 `git config user.name "<agent>-agent"`
4. `AGENT_NODE_ID=<本机节点>`，以及仅本地 agent 的 `{AGENT}_SLACK_*`

AI 与 GitHub 凭据按节点彻底隔离：每个人只在自己电脑登录
Claude/Codex/OpenAI 与 `gh`，不共享账号或 token。仓库按 agent 独立解析，优先级
是 `agents[].github_repo` → `defaults.github_repo` → 兼容旧配置的
`github.repo`。显式写 `github_repo: ""` 会只关闭该 agent 的 GitHub 功能。
仓库值必须是规范的 `OWNER/REPO`。

`projects` 把 Slack channel id 绑定到成员、管理员和允许的逻辑 agent。配置
projects 后，未映射频道、非项目成员和项目外 agent 都不能行使 authority。
handoff roster 取“项目配置 agent”与完整 `conversations.members` 结果的交集
（未配置 projects 时以全逻辑 roster 为配置集合），按 Slack team/channel 缓存
5 分钟；system prompt、`!roles`、入站路由、结果 handoff 与 patrol 使用同一视图，
不在场 target 只拒绝一次。查询失败、分页不完整或 cursor 循环会记录 warning，
且只回退到原配置边界，绝不越过 project ACL。1:1 DM 只把 self 暴露为 agent
target。`!reset` 仅 owner/项目管理员可执行。项目管理员必须显式列在
`projects[].admins`，必须是 `ALLOWED_SLACK_USERS` 允许的人类，并拥有该项目的
破坏性 agent 管理权限；普通项目成员不会自动继承管理员权限。

`node.max_concurrency`（默认 2，也可用 `NODE_MAX_CONCURRENCY`）限制本机 AI
并发；`node.max_queue`（默认 10，也可用 `NODE_MAX_QUEUE`）表示真实等待任务
上限，`0` 即不允许等待。只有无法同时立即预留 node slot 与 realpath workspace
的任务才计入 queued；等待 workspace 不占 node execution slot，因此不同
workspace 仍可运行。超过容量时不会创建 task，只返回一次 busy 提示；
`/state` 会显示 queued/running 和节点容量。

配置 reload 会先无副作用地解析、校验整份 YAML，再逐个处理运行中的 agent。
空闲 agent 的安全变更立即应用；busy agent 涉及 runtime/workspace/repository 的
完整快照进入 deferred，后一次 reload 会整体覆盖旧 pending（last-write-wins）。
所有已运行或排队的 Slack/patrol 工作无论 success/error/cancel 都结束后会自动
消费 pending。repository/workspace 生效前必须通过精确
`(realpath workspace, repository)` 预检；失败则完整保留旧配置与 session。
`/reload` 按 agent 返回 `applied`、`deferred`、`skipped`、
`restart_required`、`failed`，`/state` 可观察 pending。新增/删除 agent、
token 环境变量、identity/node/project、patrol 调度变更仍需重启进程。

handoff 在代码中强制单目标：一条消息提及多个已注册 agent 时只保留第一个，
并在 Slack 明示警告，不会静默 fan-out。结构化 handoff 以
`target_agent_id` 为准，即使没有 `@` 也会路由；附带 mention 会被忽略，
unknown/项目外目标不会启动任何 agent：

```text
HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-12","goal":"Review PR 12","done_criteria":["tests pass"],"artifact":"https://.../pull/12"}
<@U02BOBREVIEW>
```

**预算收敛：**peer 启动前，每台主机读取完整 Slack thread 历史，把最新允许的
human 消息之后的 handoff 按 Slack 源时间戳排序，仅前 `max_agent_rounds`
个可以执行。历史缺失或读取失败时 fail-closed。Slack bus 本身就是共享顺序
authority，不需要额外协调器，也不需要共享 AI 账户。

| 主机 | Agent | `.env` 中的 token |
|------|--------|-------------------|
| alice-mac | alice/dev, alice/reviewer | `ALICE_DEV_*`, `ALICE_REVIEWER_*` |
| bob-mac | bob/dev, bob/reviewer | `BOB_DEV_*`, `BOB_REVIEWER_*` |

agent id 中的 `/`、`-` 在默认 token 环境变量名里转为 `_`。`optional` 继续兼容
旧版本地可选 runtime；分布式归属应使用 `node_id`。设置
`AGENT_NODE_ID`/`node.id` 后，每个 agent 必须声明 `node_id` 或显式
`local: true/false`，否则拒绝启动。远端逻辑 agent 必须填写两个 Slack id。

## 每个线程独立 Git worktree

多个本地代码 agent 共用一个仓库时，`thread_worktree` 会为每个 Slack 根线程
确定性创建 branch 与 worktree。同一节点上，同一 team/channel/根线程的所有
agent 复用同一路径，并由同一 lease 串行执行；不同线程可以在 node 上限内并行。
映射持久化到 `STATE_DB`，应用自身的 `git worktree` 控制操作在进程间串行。

```yaml
worktrees:
  root: /app/worktrees
  base_ref: main
  max_per_repo: 16

defaults:
  workspace_mode: thread_worktree
```

启用后这三个值必须全部明确配置，也可分别由 `WORKTREE_ROOT`、
`WORKTREE_BASE_REF`、`WORKTREE_MAX_PER_REPO` 提供。`WORKTREE_ROOT`
必须是 base 仓库之外、不含 symlink component 的绝对路径。Compose 使用每位
owner 独立的 worktree volume（`alice-worktrees` / `bob-worktrees`），它与
state volume 分离且不跨用户共享。Claude/Codex 的 `cwd` 是线程 worktree；
OpenAI API agent 只持有相同调度 lease，没有本地文件工具。Patrol 和 serial
模式仍使用配置的 base workspace。

`WORKTREE_ROOT` 只能在进程重启时切换。reload 会把 `worktree_root` 报告在
`restart_required` 中，并保持当前 root、RepoSpec、mapping 与 session 不变；
`base_ref` 和 `max_per_repo` 继续沿用原有的空闲热更新/busy deferred 语义。
重启后，如果 `ready` 或 `creating` mapping 仍指向旧 root，系统会 fail-closed：
应先用旧配置重启并 clean remove。只有 mapping 状态为 `removed`、旧路径确实
不存在、保留的 managed branch 完整且未被 checkout、两个 root 都没有冲突的
Git metadata 时，第一次 ensure 才会把 mapping 原子迁移到新确定性路径。
dirty、active、branch 缺失或被篡改的 mapping 永不迁移。

仓库控制锁与每线程 lease 位于 canonical Git common-dir 下固定的 owner-only
目录，而不是 `WORKTREE_ROOT`。因此，同一仓库即使由不同进程、不同 root 或不同
state DB 使用，创建/删除以及同线程 runtime 仍会互斥。容量从整个仓库的
`git worktree list --porcelain` 计算，只统计严格匹配
`refs/heads/slack-agent-wt/<64 位小写十六进制>` 的 mapping；managed prefix
格式异常时 fail-closed。

监控页只显示已认证 owner 的映射，admin 可查看全部。手动移除仅 owner/admin
可用，并且 fail-closed：active、dirty、untracked、unpushed、路径/`.git`/
branch 缺失或被篡改都会拒绝。clean 移除只删除 worktree，branch 会保留；
下一轮会恢复相同 branch 与路径。系统不会做破坏性的自动清理。

Provider runtime 仍是受信任的本地进程。应用不会安装 `git`/`gh` wrapper；
system prompt 禁止 agent 执行 `git worktree`、`git gc`、`git prune`、离开
managed branch 或破坏共享 ref。无限制 Bash 技术上仍能绕过提示，因此只应对
你信任的 agent 与仓库启用。AI 订阅、CLI 登录目录、Slack/GitHub token、
state DB、base workspace 与 worktree root 始终归每个人本机所有；跨主机协作
不需要共享任何这些内容。

## GitHub 工作流试点

每个 agent 使用自己的有效 `github_repo`；顶层 `github.repo` 仅作为向后兼容
fallback。频道流程：issue → PR → review。

示例请求：

> `@dev 处理 issue #1，然后请 reviewer 审查`

Git 纪律（在 system prompt 中）：

- **读本地代码前先刷新 remote 信息**（`git fetch`；serial workspace 可用
  `git pull`）
- **让其他 agent 看之前先 commit & push + PR URL**——未 push 的工作在其他主机上不存在
- `thread_worktree` 中必须保持 managed branch，禁止
  `gh issue develop --checkout` 与 `gh pr checkout`

预期路径：

1. **dev** 原子创建 issue 专属 Git-ref 租约完成认领，再写入匹配的 agent/node/时间标记
2. **dev** 用 `gh issue view` 阅读 issue；`thread_worktree` 中直接在既有
   managed branch 工作，不 checkout
3. **dev** 修复 → commit/push → `gh pr create`
4. **dev** 贴 PR URL 并 `@reviewer`
5. **reviewer** 用 `gh pr view` / `gh pr diff`（同机时也可检查共享线程
   worktree）审查，不切换 managed branch
6. **reviewer** LGTM → merge + `gh issue close`

### 巡检（Patrol）

Agent 可周期性扫描 `status:todo` issue，原子创建 issue 专属 Git-ref 租约后认领一件并推进。租约协议由 `issue_claim.py` 执行（`claim` / `renew` / `release` / `verify`，输出一个 JSON 结论，只有 `claimed` / `renewed` 代表拥有），不再交给模型手动执行：巡检由宿主先列出 todo issue 并认领最早可认领的一件，空闲轮不消耗任何 provider 回合；回合工作期间由宿主续租，续租失败或结果未知会取消该回合。交互回合调用同一个工具；ref 和 marker 格式不变，仍可与旧的 prompt 驱动协议节点混跑。租约为 30 分钟，至少每 15 分钟续约；仅当 GitHub 服务端 `Date` 已超过到期时间加 5 分钟宽限期时，才允许 stale takeover。创建、续约、接管和释放都使用 `--force-with-lease`，条件必须是 ref 不存在或等于已观测 SHA。开工前必须重读并确认 ref、metadata commit、marker comment 的 issue/agent/node/nonce/时间/SHA 完全一致。读取失败、marker 缺失或不匹配、命令失败、超时及结果不确定一律 fail-closed。运维恢复 stale ref 时也必须基于已观测 SHA 条件写，禁止无条件删除。共享 GitHub assignee 不代表所有权。

巡检使用 epoch 对齐的绝对 deadline，以及所有节点一致、稳定排序的 logical-agent roster；不同节点会得到同一 wall-clock schedule，长任务结束后跳过错过周期，不做追赶式突发。启用 GitHub 时，workspace 的每一条 `origin` fetch/push URL 都必须匹配配置的 canonical `OWNER/REPO`，否则该 workspace 的 GitHub workflow 与巡检会被禁用；租约 CAS push 不使用可变 remote 名，而固定推送到显式 canonical `https://github.com/OWNER/REPO.git`，因此须先配置非交互 HTTPS 认证（例如 `gh auth setup-git`）。巡检与 Slack 回合共用 node concurrency limiter 和 realpath workspace lock。空闲轮次输出 `PATROL_IDLE`（不发帖）。

```yaml
github:
  repo: OWNER/DEFAULT-REPO     # 旧配置 fallback
  patrol_channel: C0XXXXXXXX   # 巡检发帖频道

defaults:
  github_repo: OWNER/TEAM-REPO

agents:
  - name: dev
    github_repo: OWNER/DEV-REPO # agent 级覆盖
    patrol_interval: 300       # 秒；0 = 关闭

  - name: reviewer
    github_repo: ""             # 只禁用此 agent 的 GitHub
```

repository/workspace 变更会在 target 预检通过后热应用（busy 时 deferred）。
修改 patrol interval/channel 后需重启。日志和 `/state` 会显示 active repo、
pending config 与 patrol 状态。
