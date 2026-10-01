# 共通チャンネル運用ルール テンプレート

このテンプレートを埋めて、**Slack の共通チャンネルのトピックまたは説明**に貼ってください。
agent は起動・応答のたびにチャンネルの topic / purpose を読み、ここに書かれた運用ルールに従います
（`_fetch_channel_guidance`、5 分キャッシュ）。`{{...}}` を実際の値に置き換えてください。

---

## 対象リポジトリ
- repo: `{{OWNER/REPO}}`   （例: acme/agent-sandbox）
- タスクの唯一の正: **GitHub Issues**。着手前に必ず `gh issue list --repo {{OWNER/REPO}} --label "status:todo"` を確認。

## パイプライン（崩さない）
dev 実装 → **@reviewer** コードレビュー → **@qa** 行動検証 → **@pm / 人間** 最終承認。
- 自己レビュー・自己検証・独断のリリースはしない。
- 対外発表 / 課金 / 本番 push が必要になったら止めて **@{{HUMAN_HANDLE}}** に確認。

## Git 規律
- ローカルコードを見る前に毎回 `git pull`。
- 他 agent に見せる成果は **commit & push 済み + PR リンク** を貼ってから。push していない作業は存在しない扱い。
- ブランチは `gh issue develop <番号> --checkout`。ラベルは status:todo → in-progress → in-review。

## 認領（claim）プロトコル
`gh issue edit <番号> --add-assignee @me` → issue を読み直して自分が assignee か確認 → issue に「claimed by <name>」 → Slack 宣言。
既に他の assignee がいる issue には手を出さない。

## コラボ規律
- 1 メッセージ 1 依頼。同じタスクを複数 agent に同時に振らない。
- 既出情報を繰り返さない。相槌だけの返信はしない。
- 依頼には「対象ファイル・目的・完了条件」を必ず書く。

## Handoff プロトコル（全队通用）
- handoff 时必须带上：**目标** / **验收标准** / **相关 issue·PR 链接**（缺一不可时先补齐再 @）。
- 通用协议写在本频道 topic/说明；各 agent 的差异（何时找谁、交付什么）写在 agents.yaml 的 **`card`** 字段，不要重复贴进频道规则。

## この channel の目的 / 追加ルール
{{この channel は何のためか。例: #project は開発の唯一の対外窓口。レビュー依頼は必ず PR リンク付き}}
{{追加ルールがあればここに}}

---

<!-- Slack のトピックは短いので、長い場合は「説明(purpose)」に貼るのがおすすめ。 -->
