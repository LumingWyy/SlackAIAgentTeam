# SlackAgentTeam (multi_app)

**Languages:** [English](README.md) · [日本語](README.ja.md) · [中文](README.zh.md)

ローカル Claude Code、Codex CLI、OpenAI Responses API を利用するマルチエージェント Slack ボット。各 agent は独立した Slack App（bot 身分・token）。メンション駆動の引き継ぎ、ターン予算によるループ防止、コンテキスト継続、GitHub Issues + push による多機協調。

## アーキテクチャ

複数 agent が同一 Python プロセス内で Socket Mode 並列実行。チャンネルでは `@` された agent だけが発言。返信内の相互 `@` で引き継ぎ可能。純粋ロジック（送信者分類・起動判定・ターン予算・重複排除・プロンプト）は `multi_core.py`、Slack / Claude / Codex 配線は `multi_app.py`。

## ファイル一覧

| ファイル | 役割 |
|----------|------|
| `multi_app.py` | マルチ agent 入口：設定読込、Roster、SlackAgent、Socket Mode |
| `multi_core.py` | 純粋ロジック層（slack/claude パッケージ非依存） |
| `state_store.py` | SQLite スレッド状態永続化（セッション/要約が再起動をまたいで残る） |
| `issue_claim.py` | ホスト側の GitHub issue claim ツール（v2 lease: `claim` / `renew` / `release` / `verify`） |
| `agent_guard.py`, `agent_guard_bin/` | agent の PATH 上の `gh` / `git` ガード：agent は PR を作り、マージは人間 |
| `agents.yaml` | agent 定義（`card` / persona、所有者、node、project） |
| `agents.distributed.example.yaml` | 2 人 × 各 2 ローカル agent の分散構成例 |
| `slack-app-manifest-agent.yaml` | agent ごとの Slack App 用 manifest テンプレート |
| `.env.example` | 環境変数サンプル（マルチ token） |
| `webui.py` | ローカルコンソール（agent CRUD、ローカル workspace、セットアップ、監視、ホットスワップ） |
| `.claude/skills/slack-app-setup/` | Claude Code skill：agent がブラウザで Slack App 設定を進め、人は確認だけ行う |

## ロール

各 agent は `agents.yaml` で 2 層のペルソナを持ちます:

- **`card`（L1・任意）** — 仲間向け窓口（いつ呼ぶか / handoff に何を含めるか / 何を返すか / 何は扱わないか）。`!roles`・各 system prompt の仲間段・webui に表示。目安 ≤6 行 / 400 文字（警告のみ）。
- **`persona`（L2）** — 自分向けの行動制約。長くてよい。本人の system prompt にのみ注入。
- **フォールバック:** `card` 未設定/空 → `persona` 1 行目（従来どおり）。`defaults.card` は無視。再起動なしでホット反映可。

| name | 職責 | optional | Token 環境変数 |
|------|------|----------|----------------|
| `dev` | 実装・修正・動作確認；完了後 `@reviewer` / `@qa` | 否 | `DEV_SLACK_BOT_TOKEN` / `DEV_SLACK_APP_TOKEN` |
| `reviewer` | コードレビュー；LGTM または指摘；要修正時 `@dev` | 否 | `REVIEWER_SLACK_BOT_TOKEN` / `REVIEWER_SLACK_APP_TOKEN` |
| `pm` | 要件整理・分解；1 件ずつ `@` 依頼；人間へ報告（コードを書かない） | 是 | `PM_SLACK_BOT_TOKEN` / `PM_SLACK_APP_TOKEN` |
| `qa` | テスト観点；pytest 等；失敗時は再現手順付きで `@dev` | 是 | `QA_SLACK_BOT_TOKEN` / `QA_SLACK_APP_TOKEN` |
| `designer` | UI/UX 提案・レビュー；実装は仕様明確化後 `@dev` | 是 | `DESIGNER_SLACK_BOT_TOKEN` / `DESIGNER_SLACK_APP_TOKEN` |
| `dx` | スレッド交通整理・agent 健康（`!status` / `!reset`）；読取専用 | 是 | `DX_SLACK_BOT_TOKEN` / `DX_SLACK_APP_TOKEN` |

`dx` の `allowed_tools` は `[Read, Glob, Grep]` のみ。

## Runtime：claude / codex / openai

agent ごとに実行エンジンを選択（`runtime`：agent → defaults → `claude`）：

```yaml
agents:
  - name: dev
    runtime: codex
    codex_sandbox: workspace-write   # read-only | workspace-write | danger-full-access
    # codex_model: gpt-5.2-codex
  - name: reviewer          # 省略 → claude
  - name: planner
    runtime: openai
    openai_model: gpt-5.6-sol
    openai_api_key_env: ALICE_OPENAI_API_KEY  # 環境変数名だけを書く
  - name: grok-reviewer
    runtime: openai
    openai_model: grok-4.5
    # ローカル OpenAI 互換プロキシ（CLIProxyAPI / Antigravity ブリッジ）
    openai_base_url: http://127.0.0.1:8317/v1
```

| 項目 | claude | codex | openai |
|------|--------|-------|--------|
| 実行 | claude-agent-sdk | 本機 `codex exec --json` | OpenAI Responses API（公式または CLI Proxy） |
| スレッド継続 | session resume | thread resume | `previous_response_id` |
| system prompt | SDK 引数 | 新セッション時に前置 | 毎回 `instructions` として送信 |
| ローカルツール | Claude Code ツール | Codex サンドボックス | **なし**；分析・レビュー・handoff のみ |
| タイムアウト / 予算 | 全 runtime に `claude_timeout` とターン予算 | 同左 | 同左 |
| モデル | `claude_model` | `codex_model` | `openai_model`（既定 `gpt-5.6-sol`；任意 id 可、例 `grok-4.5`） |
| 認証 | ローカル Claude 認証/key | ローカル Codex login | ローカル環境変数（既定 `OPENAI_API_KEY`） |
| エンドポイント | — | — | 公式 OpenAI、または `openai_base_url` / `OPENAI_BASE_URL` |

モデルと runtime は webui「監視」タブで**稼働中ホットスワップ**可能（確認ダイアログ；次ターンから；`agents.yaml` へ書き戻され再起動後も維持）。`openai` へ切り替えるには、Key（またはローカル CLI Proxy の `openai_base_url`）が client 構築時に利用可能である必要があります。起動後に Key / base URL を追加した場合は一度再起動してください。

### ローカル CLI Proxy（Grok / Antigravity など）

`openai` runtime は任意の **OpenAI 互換** Responses エンドポイント（本機の [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) を含む）に向けられます：

```yaml
defaults:
  runtime: openai
  openai_base_url: http://127.0.0.1:8317/v1   # または .env の OPENAI_BASE_URL
  openai_model: grok-4.5
  openai_api_key_env: OPENAI_API_KEY          # proxy が client key を要求する場合
```

- `openai_base_url`（agent → defaults → `OPENAI_BASE_URL`）で `api.openai.com` 以外へ向ける。
- `openai_model` は自由記述：`grok-4.5`、proxy が公開する Antigravity/Gemini の id など。
- base URL のみで key 未設定のときは SDK 用プレースホルダ key を使う。proxy の `api-keys` と一致させる場合は `OPENAI_API_KEY` にその値を置く。
- WebUI のモデル一覧はよく使う名前を内蔵し、proxy 稼働時は `GET {base_url}/models` をマージ。監視タブで任意のモデル id も手入力できる。

共有 YAML には `openai_api_key_env` の変数名（と任意の `openai_base_url`）だけを保存します。Key はローカル
`.env` から読み、起動後に親プロセス環境から削除し、Slack・SQLite・remote
roster には保存しません。Slack prompt/context は Responses API に送られ、
保存された response id でスレッドを継続します。ローカル repository の読み書き
が必要な作業は Claude/Codex runtime に handoff してください。

## 状態の永続化

スレッドごとの状態（セッション resume id・引き継ぎ要約・token 統計・last-seen）は SQLite に書き込まれ——`STATE_DB`、既定 `state.db`、finite owner quota 未設定時だけ空で無効化可能——起動時に復元されるため、再起動しても agent の記憶は消えません。finite quota 設定時は `STATE_DB` が空または open 不可なら起動を拒否します。状態は認証済み Slack team id と project id で分離され、別/不明な Slack workspace や channel の旧 project のセッションを暗黙に復元しません。既存 DB は安全に移行し、旧行は空の legacy scope に残ります。projects 未設定時は互換性のため安定した team-only scope を使います。resume id はエンジン/作業ディレクトリに紐づくため、runtime や workspace の変更後は id を破棄し要約で引き継ぎます。`!reset` は永続化行も削除。48h 放置した行は掃除されます。往復予算は意図的に永続化しません。チャンネルでは許可済み人間が対象 project の agent を `@mention`、または有効な structured handoff を送った場合にリセットし、DM は `@` 不要です。guest・雑談・unknown/project 外 target ではリセットしません。Docker では DB は `agent-state` volume に置かれます。

`StateStore` は生存中ずっと `${STATE_DB}.lock` を OS lock で独占します。
同じ DB を指す 2 番目の node は session を共有せず起動失敗します。DB、lock、
WAL、SHM は private permission で、owner/node ごとに別の path/volume が必要です。

## 複数 agent 共有の local-first transcript

同じ `multi_app` process の全 local agent は、Slack team・channel・thread を
key とする bounded transcript を 1 つ共有します。root/reply/edit/delete
event は dedup、project routing、認可、activation の早期 return より前に
取り込むため、各人のローカル 2–3 agent が同じ thread を重複取得しません。
Slack timestamp 順で、context 分類に必要な field だけを保存します。現在の
roster、project ACL、human allowlist、guest/feed label は read 時に適用し、
write 時点の権限を固定しません。

通常の context hot path は local-only です。live root を観測すれば
`conversations.replies` **0 回**で context-warm になりますが、live
observation 自体は canonical authority ではありません。canonical handoff が
最後の full snapshot watermark を越える場合、process 共有の same-thread
backfill を 1 回実行します。cold/incomplete thread も
same-thread singleflight で backfill し、cursor pagination と `limit=15` を
使います。restart で復元した thread は process ごとに 1 回、`oldest` shortcut
なしで root から full remote revalidation して authority を取得します。同じ
boot 内の memory LRU reload はその boot の validation を維持し、新しい boot
では persisted row を unknown として full revalidation を 1 回行います。terminal
cursor に加え、完全 snapshot 内に root が存在するときだけ complete です。
成功 snapshot は offline edit を更新し、remote から消えた旧 persisted message
を tombstone にします。per-message compare-and-swap により backfill 中の live
new/edit/delete が常に勝ちます。failure、root 欠落、coverage ambiguity、HTTP
429 では local partial suffix を返し、missing-message delete は行わず、
`Retry-After`（なければ設定した cooldown）まで再試行しません。restart の full
validation は最悪 `ceil(thread messages / 15)` pagination call を 1 回だけ要し、
1 request/min の縮小 tier では長い cold thread に数分かかり得ますが、その後の
warm activation は history API 0 回です。record は既定 64 KiB に制限され、
oversized structured field は破棄、text は UTF-8 safe に truncate されます。
canonical ordering に影響し得る truncated record が 1 件でもあれば、remote
fetch 成功後も snapshot は non-authoritative です。canonical distributed
handoff/budget は partial history を fail-open しません。件数/期限容量で古い記録が切れた場合も、現行 policy の
driving-human reset が truncation boundary より新しいときだけ suffix を
authoritative とし、それ以外は fail-closed です。

Slack の縮小 rate 条件は正確には、**2025-05-29** 以降に新規作成または新規
install された、Marketplace 未承認で商用配布される app です。この場合
`conversations.history` / `conversations.replies` は **1 request/min、
最大 15 objects/request**。Marketplace app と customer-built internal app は
Tier 3 のままで、既存の unlisted distributed installation は公表された縮小
rate の対象外です。Slack 公式の
[`conversations.replies` reference](https://docs.slack.dev/reference/methods/conversations.replies/)
と[rate-limit announcement](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/)
を参照してください。このため activation ごと・local peer ごとの history API
call には依存しません。

| 設定 | 既定 | bound |
|------|------|-------|
| `TRANSCRIPT_MAX_THREADS` | `512` | process 内 memory LRU thread 数 |
| `TRANSCRIPT_MAX_MESSAGES_PER_THREAD` | `50` | memory / SQLite の thread 当たり最新 record 数 |
| `TRANSCRIPT_DB_MAX_THREADS` | `2048` | SQLite transcript thread 数（agent/session row は削除しない） |
| `TRANSCRIPT_MAX_RECORD_BYTES` | `65536` | normalized record 当たり logical bytes（min 256 B、hard max 1 MiB） |
| `TRANSCRIPT_MEMORY_MAX_BYTES` | `33554432`（32 MiB） | process 全体の transcript memory bytes（hard max 512 MiB） |
| `TRANSCRIPT_DB_MAX_BYTES` | `268435456`（256 MiB） | SQLite logical transcript bytes（hard max 4 GiB） |
| `TRANSCRIPT_TTL_SECONDS` | `604800`（7 日） | transcript retention |
| `TRANSCRIPT_RETRY_COOLDOWN_SECONDS` | `60` | backfill failure 後の最短 retry 間隔 |

memory / SQLite byte cap はどちらも record cap 以上が必要です。不正または
逆転した値は message 内容を error に含めず startup 時に拒否します。

`/state` と WebUI が表示するのは memory/persisted message・byte capacity、
complete/partial、eviction、backfill call、failure の aggregate counter
のみで、message text・user content・credential は
公開しません。

## 返信言語

`reply_language`（agent → defaults → `日本語`）を system prompt に注入。  
`agents.yaml` の既定は `English` のことが多い。`中文` / `日本語` / `English` を指定。  
webui 監視タブでも agent ごとにホットスワップ可能（次ターン；`agents.yaml` へ書き戻し）。

## コンソール（Web UI）

```bash
make webui        # → http://127.0.0.1:8765
```

**ガイド**タブ：初回表示される5段階のランブック（role → Slack App →
ローカル認証 → Prompt → 最初の handoff）。進捗は browser-local のみ。
チャンネルルール、タスク開始、構造化 `HANDOFF`、dev/reviewer/planner persona
をコピーできます。静的ガイドを読むだけなら control Bearer は不要です。コントロールトークンは、サーバーが 401 を返したときだけ画面上部の入力欄で求められます（コントロール認証のない単一ノードでは表示されません）。

**監視**タブ：稼働中 multi_app の管理 API から状態・モデル・予算・issue 一覧を表示。モデル / runtime / 返信言語 / reasoning effort のホットスワップ、セッション再起動、UI 言語（中/日/EN）。runtime 切替・セッション再起動・worktree 削除はブラウザのダイアログではなくカード内の確認バー（✓ で確定、✕ か Esc で取消）で行い、バーは自動更新後も残ります。5 秒ごとの自動更新では値が変わった数字だけが回転し、画面はちらつきません。

**設定**タブ：agents.yaml / .env 編集（保存すると稼働中 multi_app へ自動反映；agent の追加/削除は再起動が必要）、Slack App セットアップウィザード（manifest 生成 → token 検証 → .env 保存）、persona 編集・agent 追加。ウィザードの「この manifest で Slack に作成」リンクは、manifest を事前入力した Slack の作成画面を開きます。各 agent カードの**ローカル workspace** で `workspace` と `github_repo` を変更でき、「確認」でディレクトリの有無・git リポジトリか・ブランチ・origin を調べ、origin がリポジトリと一致しない場合（GitHub 連携が無効になる）は警告して origin のリポジトリへワンクリックで切り替えられます。保存するとホットリロードされ、`github_repo` を空にすると既定を使います。ディレクトリは直接入力するほか、「選択…」でこのマシンのフォルダ選択ダイアログ（macOS、または zenity / kdialog のある Linux デスクトップ）を開くか、「参照」で画面内から順にたどって選べます（ホームディレクトリ内のみ、git リポジトリには印が付きます）。GitHub リポジトリも「選択…」から、このマシンの gh アカウントで見えるリポジトリを選べます（owner / 組織の切替、検索可）。確認でリポジトリが存在しないと分かった場合は「GitHub に作成」で作れます（既定は非公開、確認あり）。確認ではまずこのマシンに gh があり、ログイン済みかを調べ、未インストールならインストール先を、未ログインなら「認証」ページを案内し、リポジトリ不可と誤表示しません。Claude Code で `/slack-app-setup`（`.claude/skills/slack-app-setup`）を実行すると、Claude in Chrome 接続時に agent がブラウザで作成 → インストール → App-Level Token → チャンネル招待 → 再起動を進め、効果のある操作は毎回確認を求め、token には触れません（`xoxb-` / `xapp-` は自分でコンソールに貼り付けます）。

**認証**タブ：agent が実際に動く環境（Docker 優先、なければ host）で
Claude、Codex、GitHub のログインを検証します。各 owner は自分のアカウント
だけを接続します。

**見た目**は [rare-ui](https://github.com/swamimalode07/rare-ui) に倣っています：ライト / ダーク / システムの3テーマ、選択タブがバーから分離するナビ、桁ごとに回る数字、チェック時に項目へ取り消し線が引かれるタスクリスト、コピー後にチェックへ変わるボタン。「視差効果を減らす」設定時はすべての動きを止めます。

webui は **127.0.0.1 のみ**（DNS rebinding 対策の Host 検査付き）。owner/分散構成では環境変数 `SLACK_AGENT_CONTROL_TOKEN_<SLACK_USER_ID>` の独立 Bearer（32 文字以上のランダムな printable 文字列）が自動的に必須です。browser は `sessionStorage` のみに保持して `Authorization: Bearer ...` を送り（401 の後にだけ画面内で入力を求め、拒否されたトークンはすぐ破棄）、user-id header は信用しません。owner は自分の agent だけ、top-level admin は全 agent を管理でき、global reload は admin 専用です。匿名 endpoint は `/healthz` だけです。保存時 `.bak` バックアップ（gitignore 済み）。`agents.yaml` のコメントは保存時に失われる。

## Slack App 作成（手動）

（ウィザードの「この manifest で Slack に作成」リンクを使うか、Claude Code で `/slack-app-setup` を実行する場合は省略可。）agent ごとに**独立 App** が必要。

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. `slack-app-manifest-agent.yaml` を貼り、次の 3 箇所を agent ごとに変更：
   - `display_information.name`
   - `features.agent_view.agent_description`
   - `bot_user.display_name`
3. **Install to Workspace**
4. Bot Token（`xoxb-...`）と App-Level Token（`xapp-...`, scope `connections:write`）を取得
5. `.env` に `{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN` として保存
6. 全 agent で繰り返し

`agent_view` 利用には `slack-bolt>=1.29.0`。

### optional の意味

- `optional: true`：token 未設定なら **warning して skip**、プロセスは継続
- 必須 agent（dev / reviewer）の token 欠落 → `RuntimeError`
- 全 agent skip → `RuntimeError`（有効な agent なし）

### Scope

コア送受信・Agent 体験・`conversations.members` 用チャンネル参照・ユーザー参照・リアクション・ファイル・`chat:write.customize`（任意）など。詳細は manifest を参照。

**scope / manifest 変更後は必ず Reinstall to Workspace。**

### Slack ネイティブ拡張

- `agents.yaml` の `trusted_feed_bots: [B0XXXXXXX]` — 許可した Slack App（例: GitHub）の投稿を読み取り専用の `[feed]` コンテキストにする（agent は起動しない）。
- トリガーメッセージの添付ファイルは `<workspace>/.slack-files/` に取得（最大 3 件・各 10MB・48h 後に掃除）。agent がローカルツールで読む。
- 進行状況はトリガーへの reaction（📥 混雑したスレッドや満杯のノードで待機中 → ⏳ 対応中 → ✅ 完了 / ❌ 失敗 / 🤐 新鮮度チェックで返信を取り下げ）。「対応中…」プレースホルダ投稿は出さない。provider cooldown の待機が 30 秒以上なら、再開見込みを1回だけ通知する。
- 失敗はカテゴリ別に次の行動を示す（文脈が長すぎる → `!reset`、認証・請求 → ノード所有者）。patrol は同じカテゴリの失敗が3回続くと停止してチャンネルに1回通知し、6 回に 1 回だけ試行、成功すると自動再開（`/state` の `patrol_fence` / `last_failure`）。
- 再起動で ⏳ が残り続けることはない：受け付けたアクティベーションは state DB（`activation_ledger`）に記録し、起動時に前プロセスが終えられなかったものへ ⚠️ を付け、確認と再メンションを促す通知をスレッドに出す。中断されたターンが push やコメントを済ませている可能性があるため、自動では再実行しない。
- 同じスレッドで待機中のトリガー（処理中に「@dev X を追加」「@dev Y も」と続いた場合など）は、古い順にまとめて1回のターンで返信する。1回目が文脈として見た内容を2回目がもう一度答えることはない。
- 登録済み agent をテキストだけの `@name` で書くと誰にも通知されないため、投稿に1行の警告を付ける。

## .env

`.env.example` を参照。最低限は必須 agent：

```bash
DEV_SLACK_BOT_TOKEN=xoxb-...
DEV_SLACK_APP_TOKEN=xapp-...
REVIEWER_SLACK_BOT_TOKEN=xoxb-...
REVIEWER_SLACK_APP_TOKEN=xapp-...

# 強く推奨：許可する人間の Slack ユーザー ID
ALLOWED_SLACK_USERS=U01ABCDEF

# 任意
CLAUDE_WORKSPACE=/path/to/project

# 任意：運用チューニング（既定値と説明は .env.example）
# FRESHNESS_RECHECK=1                  # 投稿前の新鮮度チェック
# PROVIDER_COOLDOWN_BASE_SECONDS=60    # レート制限 cooldown（provider アカウント単位で共有）
# PROVIDER_PACER_BASE_SECONDS=0.5      # ノード全体のターン開始間隔。0 で無効
# SOCKET_GAP_REVALIDATE_SECONDS=120    # この秒数以上の Socket 切断後に transcript を再検証
# AGENT_PROTECTED_BRANCHES=main,master # agent が直接 push できないブランチ
```

## 起動

### Docker（推奨）

```bash
cp .env.alice.example .env.alice   # Alice の機械では Alice の値だけ
NODE=alice make up                 # Alice node だけを起動
NODE=alice make logs
NODE=alice make restart
NODE=alice make down
NODE=alice make test-docker
make help
```

Bob は `.env.bob` と `NODE=bob` を使います。各 profile は state、workspace、
Claude、Codex、gh volume が完全に別で、共有 mount は read-only の
credential-free `roster.yaml` だけです。container は non-root `agent` で動作し、
admin port は localhost のみ（Alice 8766、Bob 8767）です。

### ローカル venv

```bash
.venv/bin/python -m pip install -r requirements.txt
make run     # = .venv/bin/python multi_app.py
make test
make webui
```

## 使い方

1. 共有チャンネルに bot を招待（最低 `dev`・`reviewer`）
2. 例：`@dev foo.py の bar を実装し、終わったら reviewer に見て`
3. `dev` 完了後に `@reviewer` → メンションで起動
4. `pm` 利用時は先に `@pm` で分解
5. 許可済み人間が対象 agent を `@mention`、または有効な structured handoff を送ると**ターン予算がリセット**（DM は `@` 不要）

DM では人間は `@` なしで会話可能（peer の DM 引き継ぎはしない）。

## コンテキスト自動管理

直近ターンの入力コンテキスト（input + cache tokens）が閾値超過時：

1. **要約**（ツールなし 1 ターン、≤800 字）
2. **セッション切替**
3. 次回起動時に要約を**引き継ぎ注入**

閾値 `context_rollover_tokens` 既定 **60000**（agent → defaults → 60000）。usage 欠落時は 0 扱い・rollover しない。

## 信頼性

| 機構 | 内容 |
|------|------|
| ハードタイムアウト | `claude_timeout` 既定 900 秒 |
| Slack 429 | Retry-After 自動リトライ（最大 2） |
| メモリ回収 | スレッド状態 idle 48h で回収 |
| 長いスレッド | 共有 bounded local transcript。cold/incomplete のみ cursor、最大 15 件/page で backfill。文脈ブロックを切り詰めるときはスレッド先頭（通常はタスク定義）を残し、途中を省略する |
| 返信 freshness gate | 投稿直前に、ターン実行中へ届いた peer/許可済み人間のメッセージを検出し、ちょうど1回だけ再判断（`POST_ORIGINAL` 原文投稿 / 修正全文 / `NO_REPLY` 取り下げ）。最新の非自分メッセージと逐語一致する返信は投稿しない。ローカル transcript のみ参照（追加 Slack API 呼び出しゼロ）、gate 自体の失敗は fail-open。`FRESHNESS_RECHECK=0` で無効化 |
| Provider rate-limit cooldown | AI ターンがレート制限された場合（Claude / Codex / OpenAI の 429・usage limit・overloaded シグナル、`Retry-After` 尊重）、同じ provider アカウントを使うローカル agent 全員で共有する cooldown を作動（Claude agent はローカルの Claude ログイン、Codex agent は Codex ログインを共有。OpenAI はエンドポイント + キー変数ごと）——基本 **60 秒**、連続時は倍増で最大 **480 秒**。終了後の再試行は安全が確認できる場合だけ1回行う：副作用のあるツール（シェル・編集・MCP）が未実行、provider の解除時刻が 15 分以内、スレッドが未リセット。それ以外は黙って再実行せず、スレッドに理由を通知する。cooldown の待機中はノードのスロットを手放すため、他の agent は止まらない。cooldown 中に始まるターンは先に待機、patrol はスキップ（次の epoch で再試行）。成功ターンで streak リセット。状態は `/state` の `provider_cooldown`。`PROVIDER_COOLDOWN_BASE_SECONDS` / `PROVIDER_COOLDOWN_MAX_SECONDS` で調整 |
| Adaptive turn pacer | 全ローカル agent がノード共通の provider ターン開始タイムラインを共有し、適応間隔で開始をずらす——基本 **0.5 秒**、レート制限ごとに倍増で最大 **8 秒**、連続 **5** クリーンターンで半減して基本値へ回帰——共有 provider アカウントの 2-3 agent が同時発火しない。Slack・freshness recheck・patrol の全ターンに適用；待機はターン timeout 窓の外で行われ、総並列度は下げない。状態は `/state` の `turn_pacer`。`PROVIDER_PACER_BASE_SECONDS`（0 で無効）/ `PROVIDER_PACER_MAX_SECONDS` / `PROVIDER_PACER_CLEAN_TURNS` で調整 |
| Socket 配信ギャップ | Socket Mode の切断（またはホストのスリープ）が **120 秒以上**続くと Slack の再配信期間を過ぎた可能性があるため、共有 transcript は次回読み込み時にスレッド先頭から再検証し、欠落を文脈として使い続けない。`SOCKET_GAP_REVALIDATE_SECONDS` で調整 |

## セキュリティ

| 機構 | 内容 |
|------|------|
| 資格情報隔離 | 起動後に環境から Slack token を scrub；`.env` は 0600 |
| コンテキスト認可 | self / 現在チャンネルの peer / 許可済み人間は通常 context、authority のない人間は continuation-safe な `[guest]` 情報のみ。unknown bot/system は除外 |
| 状態 DB 権限 | `STATE_DB`（既定 `state.db`）に session state と bounded local transcript を保存。ディレクトリ `0700`、DB/WAL/SHM `0600`。機密扱いとし、信頼できないユーザーと共有しない |

残リスク：Bash は同一 OS ユーザーでディスク上の `.env` と状態 DB を読める可能性。本番は token ローテと OS ユーザー / コンテナ分離を推奨。

### 運用コマンド

対象 agent を `@` してコマンド（**ターン予算を消費しない**）：

| コマンド | 効果 |
|----------|------|
| `!status <@agent>` | セッション / token / ターン / 要約 / 残予算 |
| `!reset <@agent>` | そのスレッドのセッション等を完全リセット |
| `!roles <@agent>` | 現在チャンネルの agent 構成と職責（`card`、空なら persona 1 行目） |

## チャンネル指針（topic / purpose）

起動時にチャンネルの topic / purpose を読み（5 分キャッシュ）、運用ルールとして prompt 先頭に注入。

## 協調の摩擦対策

1 メッセージ 1 依頼、既出情報の繰り返し禁止、相槌のみ禁止、ターン予算（既定 12）、`dx` による交通整理。

## ループ防止

公開チャンネルは `@` または有効な structured handoff が必要で、agent→agent 連続引き継ぎには上限があります。予算は、許可済み人間が対象 project の agent を `@mention`、または有効な structured handoff を送った場合にリセット（DM は `@` 不要）。guest・雑談・project 外 target ではリセットしません。guest 発言は読み取り専用の `[guest]` context として残りますが、起動/reset・command・handoff 認可・agent への指示には使えません。予算切れ時はスレッドごとに 1 回だけ一時停止通知。

## よくある落とし穴

1. scope 変更後は **reinstall 必須**
2. bot 同士の `@` は `app_mention` が不安定 → 実装は `message` で判定
3. token 名は `{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN`
4. `ALLOWED_SLACK_USERS` 未設定は全員トリガー可
5. Edit/Write/Bash はホストを変更可能 → workspace と許可リストを厳格に
6. `slack-bolt>=1.29.0` が `agent_view` に必要
7. remote agent が unknown bot になる → 各機に `slack_user_id` / `slack_bot_id` を配布

## 多機デプロイ

[`roster.yaml`](roster.yaml)、[`agents.alice.yaml`](agents.alice.yaml)、
[`agents.bob.yaml`](agents.bob.yaml) の「Slack で調整・各機で実行」構成を
使います。全 host で共有するのは credential-free roster だけで、各 node は本人の
local runtime file と env file だけを使います。roster は name、Slack user/bot id、
owner、node、card、project ACL の identity/routing authority です。token/env、
workspace、runtime、GitHub、tool field は拒否されます。local file には本人の
2–3 agent の runtime と credential variable 名だけを書きます。

各人が持つ Slack token は本人 agent 分だけです。Claude/Codex login、OpenAI key、
GitHub auth、workspace、SQLite は本人 node に残します。node 間の協調と context
transfer は **Slack message/handoff のみ**で行い、AI account、login directory、
local session、filesystem、database は共有しません。
全 logical agent に canonical な `owner: U...` を指定し、各機では local owner
の control Bearer を必須設定します。top-level admin Bearer は node ごとに任意で、
明示的に導入した node だけに権限を与えるため、team 共通 token を全 host へ
コピーする必要はありません。owner token の欠落、短すぎる値、導入済み token
の重複は起動時に拒否されます。

共有 roster には owner ごとの UTC 日次 token quota も設定できます。

```yaml
quotas:
  daily_total_tokens:
    U01ALICE: 250000
    U02BOB: 250000
  reservation_tokens:
    U01ALICE: 20000
    U02BOB: 20000
```

同じ人が所有する 2–3 個の local agent は 1 つの ledger を共有し、同じ
`node_id` に属する必要があります。shared roster が 1 owner を複数 node に
分割した場合は拒否します。各 turn は
node/queue slot の取得前に SQLite `BEGIN IMMEDIATE` で quota を原子的に予約し、
`使用済み + 有効な予約 + 新規予約 <= 日次上限` の場合だけ開始できます。queue
full、task 作成失敗、provider 開始前の cancel は予約を解放します。provider 開始後
は output token を含む正確な usage で精算します。実 usage が予約を上回る場合、
その完了 turn に限り日次上限を超え得ますが、後続 turn は拒否されます。開始済みで
error または usage 不完全なら、少なくとも予約量を保守的に精算して
`estimated`/`error` を記録します。起動時には stale な未開始予約を解放し、開始済み
予約を保守的に精算します。

quota の `input_tokens` は cache input を含み、`output_tokens` は必ず日次 total に
加算し、`cache_tokens` は input の診断用 subset として別途保持します。Claude は
direct/cache-read/cache-creation、Codex は direct/cached input を別々に報告します。
OpenAI Responses の `input_tokens` は cached token を既に含むため二重加算しません。
output usage 欠落時は 0 扱いせず推定 turn にします。`/state` と WebUI は limit、
used、remaining、active reserved、denied、errors、agent/runtime drilldown を表示し、
prompt/message 本文は含みません。owner は自分の ledger のみ、Bearer を導入した
top-level admin はその node の全 owner を閲覧できます。

起動時には remote を含む全 logical agent の Slack user/bot id、owner、node、
card を roster に登録します。Socket Mode と AI runtime を開始するのは、自 node
に属して token が揃った agent だけです。これにより AI アカウントを共有せず、
remote bot からの @handoff を peer として認識できます。

remote agent の add/remove/card/owner は local Slack socket を再接続せず
in-memory routing を hot replace します。local Slack identity、owner、node の
変更は restart-only で、restart までは認証済み identity を維持します。不正・競合
roster snapshot は live state を一切変更しません。

各機には Python 環境、自分の `claude` / `codex` ログインまたは `OPENAI_API_KEY`、必要なら自分の
`gh auth login`、書込み agent ごとの clone と
`git config user.name "<agent>-agent"` が必要です。

AI と GitHub の資格情報は node ごとに完全分離します。各人が自分の端末で
Claude/Codex/OpenAI と `gh` にログインし、account/token は共有しません。
repository は agent ごとに
`agents[].github_repo` → `defaults.github_repo` → 従来の `github.repo`
の順で解決します。`github_repo: ""` はその agent の GitHub を明示的に無効化
します。値は canonical な `OWNER/REPO` 形式だけを受け付けます。

`projects` は Slack channel id を human member、admin、許可 agent に束縛します。
設定時は未割当 channel・非 member・project 外 agent に authority を与えません。
handoff roster は project 設定 agent と完全な `conversations.members` 結果の
積集合（projects 未設定時の設定集合は全 logical roster）で、Slack
team/channel ごとに 5 分 cache します。system prompt・`!roles`・受信 routing・
結果 handoff・patrol は同じ view を使い、不在 target は 1 回だけ拒否します。
API 失敗・pagination 不完全・cursor loop は warning を出し、project ACL を
越えず設定境界だけへ fallback します。1:1 DM の agent target は self のみです。
`!reset` は owner/admin のみに制限します。project admin は
`projects[].admins` に明示し、`ALLOWED_SLACK_USERS` で許可された human
である必要があります。その project の destructive agent 管理権限を持ちますが、
通常 member は権限を継承しません。

`node.max_concurrency`（既定 2、`NODE_MAX_CONCURRENCY` でも指定可）がローカル
AI 実行数を制限し、`node.max_queue`（既定 10、`NODE_MAX_QUEUE` でも指定可）が
実際に待機できる job 数を制限します。`0` は待機不可です。node slot と
realpath workspace の両方を即時予約できない job だけを queued と数え、
workspace 待ちで node execution slot を占有しないため、別 workspace は実行を
継続できます。上限超過時は task を生成せず busy 応答を 1 回返します。
`/state` で queued/running と node capacity を確認できます。

設定 reload は最初に YAML 全体を無変更で parse/validate し、その後 running
agent ごとに独立して適用します。idle な安全変更は即時反映し、busy agent の
runtime/workspace/repository を含む snapshot は全体を deferred にします。
後続 reload は pending snapshot を丸ごと置換（last-write-wins）し、queued を
含む Slack/patrol work が success/error/cancel のいずれで終了しても自動消費
します。repository/workspace を active にする前に正確な
`(realpath workspace, repository)` を preflight します。失敗時は旧 config と
session を完全維持します。`/reload` は agent ごとの `applied` / `deferred` /
`skipped` / `restart_required` / `failed` を返し、`/state` で pending を確認
できます。agent の追加/削除、token 環境変数、identity/node/project、patrol
schedule の変更は再起動が必要です。

handoff はコードで 1 target に制限されます。複数 agent を mention した場合は
先頭だけが実行され、Slack に警告を表示します。構造化 handoff では
`target_agent_id` が `@` の有無に関係なく優先され、付随 mention は無視します。
unknown/project 外 target の場合は誰も起動しません。

```text
HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-12","goal":"Review PR 12","done_criteria":["tests pass"],"artifact":"https://.../pull/12"}
<@U02BOBREVIEW>
```

peer 起動前に各機が Slack thread の完全な履歴を読み、最新の許可 human 発言より
後の handoff を Slack source timestamp 順に並べます。先頭
`max_agent_rounds` 件だけが実行可能で、履歴が欠落/取得失敗なら fail-closed
します。Slack bus 自体が順序の共有 authority で、別 coordinator や AI account
共有は不要です。

`alice/dev` のような `/`・`-` は既定 token 環境変数名では `_` に正規化されます。
`optional` は旧来の local optional runtime 用に残り、分散 ownership には
`node_id` を使います。`AGENT_NODE_ID`/`node.id` 設定時は各 agent に
`node_id` または明示的な `local: true/false` が必須で、曖昧な ownership は
起動時に拒否されます。remote logical agent には 2 つの Slack id が必須です。

## スレッド単位の Git worktree

複数のローカル code agent が 1 つの repository を使う場合、
`thread_worktree` は Slack root thread ごとに決定的な branch と worktree
を作ります。同じ node の同一 team/channel/root thread は全 agent が同じ
path と lease を共有して直列実行し、別 thread は node 上限まで並列実行
できます。mapping は `STATE_DB` に永続化され、app 所有の `git worktree`
制御操作は process 間でも直列化されます。

```yaml
worktrees:
  root: /app/worktrees
  base_ref: main
  max_per_repo: 16

defaults:
  workspace_mode: thread_worktree
```

有効時は 3 値すべてが必須で、`WORKTREE_ROOT`、`WORKTREE_BASE_REF`、
`WORKTREE_MAX_PER_REPO` でも指定できます。`WORKTREE_ROOT` は base
repository 外の絶対 path で、symlink component を含められません。Compose
は owner ごとに独立した worktree volume（`alice-worktrees` /
`bob-worktrees`）を state volume と別に mount し、人同士では共有しません。
Claude/Codex の `cwd` は thread worktree です。OpenAI API agent は同じ
scheduler lease を保持しますが local filesystem tool は持ちません。Patrol
と serial mode は設定済み base workspace のままです。

`WORKTREE_ROOT` の変更は process restart 時だけ反映されます。reload は
`worktree_root` を `restart_required` に報告し、稼働中の root、RepoSpec、
mapping、session を変更しません。`base_ref` と `max_per_repo` は従来どおり
idle 時に hot reload、busy 時に deferred されます。restart 後も
`ready` / `creating` mapping が旧 root を指す場合は fail-closed です。
いったん旧設定で restart し、先に clean remove してください。status が
`removed`、旧 path が存在せず、保持された managed branch が完全かつ未
checkout で、両 root に競合 Git metadata がない場合だけ、最初の ensure が
mapping を新しい決定的 path へ atomic に rehome します。dirty、active、
branch 欠落、tamper 状態は移行しません。

repository control lock と thread lease は `WORKTREE_ROOT` ではなく、
canonical Git common-dir 配下の固定 owner-only directory に置かれます。
同じ repository を別 process・別 root・別 state DB から使っても、作成/削除と
同一 thread runtime は相互排他になります。capacity は repository 全体の
`git worktree list --porcelain` から算出し、厳密な
`refs/heads/slack-agent-wt/<64 桁の小文字 hex>` mapping だけを数えます。
managed prefix の形式異常は fail-closed です。

monitor は認証済み owner の mapping だけを表示し、admin は全件を見られます。
手動削除は owner/admin 限定で、active、dirty、untracked、unpushed、
path/`.git`/branch の欠落・改変を fail-closed で拒否します。clean な削除は
worktree だけを外して branch を保持し、次の turn で同じ branch/path を復元
します。破壊的な自動 cleanup はありません。

Provider runtime は引き続き信頼されたローカル process です。app は
`git`/`gh` wrapper を導入せず、system prompt で `git worktree`、
`git gc`、`git prune`、managed branch からの切替、shared ref の破壊を
禁止します。無制限 Bash は技術的にこの指示を回避できるため、信頼する agent
と repository にだけ有効化してください。AI subscription、CLI login directory、
Slack/GitHub token、state DB、base workspace、worktree root は各個人の
ローカル所有のままで、cross-host 協調のために共有する必要はありません。

## GitHub ワークフロー

各 agent は自分の effective `github_repo` を使います。top-level
`github.repo` は後方互換 fallback です。issue → PR → review をチャンネルで回す。

規律：

- ローカルを見る前に **remote 情報を更新**（`git fetch`。serial
  workspace では `git pull` も可）
- 他 agent に見せる前に **commit & push + PR URL**
- `thread_worktree` では managed branch を維持し、
  `gh issue develop --checkout` と `gh pr checkout` を使わない

実装 agent は `issue_claim.py claim` で claim し（issue は `status:in-progress`）、`gh issue view` で issue を読み、既存の managed branch で
commit/push してから `issue_claim.py open-pr` で PR を作ります（`Closes #N` と Slack スレッドへのリンク付き、issue は `status:in-review`、claim は解放）。reviewer は `gh pr view` /
`gh pr diff` と、同じローカル root thread なら共有 worktree を使い、
managed branch を切り替えずに review します。LGTM の後のマージは人間が行い（agent はマージできません）、マージ時に `Closes #N` で issue が自動クローズされます。

### 巡回（patrol）

`status:todo` issue を周期スキャンし、issue 固有の Git-ref lease を原子的に作成して 1 件 claim。lease は 30 分、15 分以内ごとに更新し、GitHub server の `Date` で期限後 5 分の grace を過ぎた場合だけ stale takeover 可能です。作成・更新・takeover・解放は、ref 不在または観測済み SHA を条件に `--force-with-lease` で行います。作業前に ref・metadata commit・marker comment の issue/agent/node/nonce/timestamp/SHA が完全一致することを再確認します。読み取り失敗、marker 欠落/不一致、command 失敗、timeout、結果不明はすべて fail-closed です。運用者も stale ref を無条件削除せず、観測済み SHA を条件に回復してください。共有 GitHub assignee は所有権を表しません。

巡回位相は epoch 基準の絶対 deadline と全 node 共通の安定した logical-agent roster を使います。異なる node も同じ wall-clock schedule となり、長時間実行後は missed period を飛ばして追いつき burst を起こしません。GitHub 有効時は workspace の `origin` fetch/push URL がすべて設定済み canonical `OWNER/REPO` と一致しない限り、その workspace の GitHub workflow と巡回を無効化します。lease CAS push は変更可能な remote 名を使わず、明示的な canonical `https://github.com/OWNER/REPO.git` に固定するため、非対話 HTTPS 認証（例: `gh auth setup-git`）を事前設定してください。巡回と Slack turn は同じ node concurrency limiter と realpath workspace lock を共有します。暇なら `PATROL_IDLE`（投稿なし）。lease プロトコルはモデルではなく `issue_claim.py`（`claim` / `renew` / `release` / `verify`、JSON の判定を1つ出力し、`claimed` / `renewed` だけが所有）が実行する。巡回はホストが todo issue を列挙して最も古い claim 可能な issue を先に claim するため、暇な回は provider ターンを消費しない（`status:in-progress` の issue も確認するが、claim が期限切れのものだけを引き継ぎ、claim ref のない in-progress issue には触れない）。作業中はホストが lease を更新し、更新の失敗・不明時はそのターンを取り消す。対話ターンも同じツールを使う。ref と marker の形式は変わらないので、従来の prompt 駆動プロトコルの node と混在できる。

**ボードと引き継ぎはツールが管理する。** `claim` は issue を `status:todo` → `status:in-progress` に、`open-pr --title … --body-file …` は作業ブランチの push を確認してから `Closes #N` と Slack スレッドへのリンク入りの PR を作り、issue を `status:in-review` にして claim を解放する。`release`（途中で断念）は未完了の issue を `status:todo` に戻す。claim のコミットは `[skip ci]` 付きで、patrol はクローズ済み issue の claim ref を条件付きで削除する（最大1時間に1回）。agent ごとの `patrol_labels`（例 `[role:dev]`、agent → defaults → 制限なし）でそのラベル付き issue だけを patrol 対象にでき、ノード内の agent は 60 秒キャッシュの issue 一覧を共有する。

**agent はマージしない。** マージは人間だけが行う：agent ランタイムの PATH 先頭に `agent_guard_bin/` を置き、その `gh` / `git` ガードが `gh pr merge`（`--auto` を含む）、REST/GraphQL のマージ API、gh エイリアス、保護ブランチ（`AGENT_PROTECTED_BRANCHES`、既定 `main,master`）への push を拒否する。Claude のターンでは `Bash(gh pr merge:*)` も禁止する。これは共有 GitHub アイデンティティ向けのガードレールでありセキュリティ境界ではない。境界にはブランチ保護（レビュー必須）と、マージ権限を持たない agent 用 GitHub アイデンティティを使うこと。

```yaml
github:
  repo: OWNER/DEFAULT-REPO   # 従来互換 fallback
  patrol_channel: C0XXXXXXXX

defaults:
  github_repo: OWNER/TEAM-REPO

agents:
  - name: dev
    github_repo: OWNER/DEV-REPO
    patrol_interval: 300   # 秒。0 = 無効
    patrol_labels: [role:dev]  # これらのラベルも付いた issue だけを claim

  - name: reviewer
    github_repo: ""        # この agent だけ GitHub 無効
```

repository/workspace は target preflight 成功後に hot 適用（busy なら deferred）
します。patrol interval/channel の変更後は再起動してください。active repo、
pending config、patrol 状態は log と `/state` で確認できます。
