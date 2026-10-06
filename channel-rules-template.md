# 共通チャンネル運用テンプレート

agent は応答のたびにチャンネルの **トピック** と **説明** を読み、そこに書かれたルールに従います
（`_fetch_channel_guidance`、5 分キャッシュ）。Slack ではどちらも 250 文字まで。
長いルールは対象リポジトリの `AGENTS.md` に書き（`templates/repo-AGENTS.md` を出発点に）、説明から参照します。
`{{...}}` を実際の値に置き換えてください。

## トピック（250 文字以内）

```
{{OWNER/REPO}} の開発チャンネル。1 issue = 1 スレッド、親メッセージに issue URL。依頼は 1 回につき agent 1 体だけを @。実装は assignee が自分（gh の @me）の issue だけ、レビューと QA は指名されれば誰の PR でも。マージは人間のみ。
```

## 説明（250 文字以内）

```
各メンバーが developer・reviewer・QA を運用。流れ: developer が実装・テスト・push・PR → reviewer（既定は同じ owner）が PR に行内コメントと判定 → PASS なら PR の owner の QA が PR の commit で動作確認 → 人間がマージ。仕様・互換性・DB・本番の判断は止めて issue 担当者に @。詳細は repo の AGENTS.md。
```

## ピン留め（人間向け、文字数制限なし）

```
このチャンネルの使い方
• タスク開始: issue ごとに新しいメッセージを投稿し、自分の developer を @（issue URL と完了条件を書く）
• まだ曖昧な依頼は @pm へ（issue に分けて担当者を決め、その人の developer に渡す）
• スレッドが堂々巡り・止まったら @dx（次の一手を 1 つに絞り、詰まった agent を立て直す）
• 追加の指示・質問は同じスレッドで。別の issue は別スレッド
• エージェントと担当: !roles @agent で一覧を表示
• レビュー: reviewer が PR に行内コメントと Verdict を書く → PASS なら QA が PR の commit で動作確認し「QA: PASS/FAIL」を書く → 人間が確認してマージ
• 他の人の reviewer / QA に頼むときは、その人に一声かける（その人の利用枠を使う）
• agent 同士の自動往復には上限がある。止まったら人間が @ して再開
• 困ったとき: !status @agent / !reset @agent。暴走したらコンソールの「監視」→「停止」
• token・.env・本番の接続情報はスレッドに貼らない
```

## 書かなくてよいこと

次はすべての agent の system prompt に入っているので、チャンネルに繰り返さない:
@ されたときだけ反応する、依頼は 1 メッセージ 1 件、構造化 HANDOFF の書式、
GitHub Issues がタスクの正、claim ツールによる status ラベルの付け替え、引き継ぎ前の commit & push、
`open-pr` での PR 作成、レビュー結果は PR と Slack の両方、PR のマージは人間だけ。
