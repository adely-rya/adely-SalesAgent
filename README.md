# adely Sales Agent

最近企業に起きた変化を見つけ、その変化から映像制作需要とadelyの受注・制作可能性を評価し、営業候補を提示します。V1とV2.2を備え、V1の既存CLIは維持しています。**営業メール本文の生成、メール・DM・フォーム送信、電話、SNS投稿は行いません。**

## Setup

Docker Engine / Docker DesktopとDocker Compose v2を用意します。

```bash
git clone <repository-url>
cd adely-SalesAgent
cp .env.example .env
```

`.env` の `OPENAI_API_KEY` と、通知を利用する場合は `DISCORD_WEBHOOK_URL` を編集します。Windows PowerShellでは `Copy-Item .env.example .env` でもコピーできます。

```bash
docker compose up -d --build
```

デフォルトは毎日 **08:00 Asia/Tokyo**。起動直後には探索せず、次の指定時刻を待ちます。ダミーキーでもdaemonは常駐し続けます。キー未設定でジョブが実行されると失敗履歴と設定方法を記録して、次回時刻を待ちます。

Linuxで所有者がUID/GID 1000でない場合は、`.env` の `LOCAL_UID` / `LOCAL_GID` を `id -u` / `id -g` の結果に合わせ、`data/` をそのユーザーが書き込めるようにしてください。Windows/macOSは通常デフォルトで利用できます。コンテナは非root実行です。

## 手動実行

```bash
docker compose run --rm sales-agent python -m app.main run-once
```

完了は終了コード0、一部失敗または失敗は1、設定形式エラーは2です。キー未設定の場合は次を表示します。

```text
OPENAI_API_KEY is not configured.
Set OPENAI_API_KEY in .env.
```

Discordだけ未設定・`hogehoge` の場合は分析を続け、レポートをログに出力します。通常の手動実行はOpenAI API利用料金が発生し、Webhookを設定していればDiscordに投稿します。

## ログ・停止

```bash
docker compose logs -f
docker compose down
```

環境変数変更後は `docker compose up -d --force-recreate`。プロンプトは各処理時にファイルから読みます。実行途中で変更しないでください。変更時は対応する `*_PROMPT_VERSION` も上げ、コンテナを再作成してください。

## DBと処理の流れ

DBはホスト側 **`./data/sales.db`** に保存されます。コンテナの停止・再作成で消えません。SQLiteのWAL/SHMも同じフォルダに作られます。バックアップはサービス停止後に `data/` をコピーするか、SQLiteのbackup機能を利用してください。

1. 6カテゴリをそれぞれWeb Searchで探索（各2〜5社、質を優先）。
2. Web Searchのsources／引用メタデータとURLを照合。不正な候補、出典未確認、未来の公開日は除外。
3. domainを優先し、法人格と空白を除いた名前で補完。企業とトリガーを別テーブルへ保存。
4. 新規トリガー、またはまだ採点成功していないトリガーを6項目で採点。0〜10へclampし、Pythonで100点満点を計算。
5. 企業単位で最も高いトリガーを選び、異なる上位5社の順位を保存して戦略を生成。
6. 結果を保存後、Discordへ文字数制限内で分割送信。メンションとリンクプレビューを無効化。

`candidate_count` はそのrunで出典確認後に見つかった企業数（既知企業を含む）、`scored_count` は採点に成功した企業数、`strategy_count` は戦略の保存件数です。企業に複数トリガーがある場合、scores件数はscored_countより多くなります。既に採点済みのトリガーは次の日に再通知しません。カテゴリ・企業単位の失敗は `processing_errors` に保存し、他の処理を続けます。runの状態は `running` / `completed` / `partial` / `failed` です。

保存テーブル：`runs`, `companies`, `triggers`, `scores`, `strategies`, `human_ratings`, `processing_errors`。

- Discoveryのmodel/prompt_versionと出典メタデータはtriggers、採点はscores、戦略はstrategiesへ保存します。
- runs.config_jsonにも全段階のモデル・プロンプト版を記録し、候補0件でも設定を追跡できます。秘密情報は含めません。
- scores.selected_rankが選定時のTop 5を固定します。戦略生成失敗でも順位は残ります。
- human_ratingsは企業・runごとにgood/maybe/badを1件保存できます。入力UIやDiscordリアクション取得はありません。

全候補を評価済みのrunについてPrecision@5を計算する例（SQLite）:

```sql
SELECT s.run_id,
       SUM(CASE WHEN h.rating = 'good' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS precision_at_5
FROM scores s
LEFT JOIN human_ratings h ON h.company_id = s.company_id AND h.run_id = s.run_id
WHERE s.selected_rank BETWEEN 1 AND 5
GROUP BY s.run_id
HAVING COUNT(*) = 5 AND COUNT(h.id) = 5;
```

未評価・5社未満のrunはこの例では除外します。モデル比較時はscores.model/prompt_versionまたはruns.config_jsonで集計してください。

## 設定

`.env.example` に全必須設定を記載しています。`TOP_CANDIDATES` は1〜5、時刻は指定タイムゾーンのローカル時刻です。追加設定:

| 変数 | デフォルト | 用途 |
| --- | --- | --- |
| OPENAI_TIMEOUT_SECONDS | 120 | APIリクエスト単位のタイムアウト秒 |
| STRATEGY_WEB_SEARCH | false | 上位企業の追加Web調査 |
| DISCOVERY_REASONING_EFFORT | xhigh | Discoveryのシンキングレベル |
| SCORING_REASONING_EFFORT | 未指定 | Scoringのシンキングレベル（空欄ならモデル既定） |
| STRATEGY_REASONING_EFFORT | 未指定 | Strategyのシンキングレベル（空欄ならモデル既定） |
| PROMPTS_DIR | /app/prompts（Docker内） | プロンプトの場所 |
| LOCAL_UID / LOCAL_GID | 1000 / 1000 | bind mountへ書き込む実行ユーザー |

APIアクセスは `app/llm.py` に集約。公式SDKのResponses APIを非同期で呼び、JSONをPydanticで検証します。互換性を優先し、スキーマをプロンプトで指定したJSON出力を使用しています。サーバー側のstrict Structured Outputsは利用していません。不正JSON/スキーマは最大2回再試行。接続・タイムアウト・429・5xxは指数バックオフで最大3回再試行し、SDK側の追加リトライは無効です。認証など他の4xxはその呼び出しを即時失敗させます。両種の再試行が重なると1生成あたり最大12リクエストです。

参照した公式仕様：[Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)、[JSON出力とスキーマ](https://developers.openai.com/api/docs/guides/structured-outputs)。指定のモデル名を環境変数に維持しています。利用するアカウントでモデルとWeb Searchが使えることを確認してください。

## Test

外部APIをモックし、実際のOpenAI呼び出し・Discord投稿を行いません。

```bash
docker compose run --rm sales-agent python -m pytest -q
```

ローカルPython 3.12でも実行できます。

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q
```

## V2.2: Opportunity Pipeline

固定情報源の記事とWeb Searchの発見結果をEventへ揃え、企業単位のOpportunityにまとめてからGate、Deep Research、NEED / WIN / DELIVER Scoringへ進みます。CollectorはAIを使わず、Daily PipelineではCheap Gateを通過したOpportunityだけをDeep Researchします。

```text
Fixed Sources → Raw Items → Fixed Events ─┐
                                           ├→ Events → Opportunities → Gate
Web Search → Web Events ───────────────────┘                              ↓
                                                        RESEARCH only → Score → Top5
```

データ概念は次のとおりです。

- Raw Item: 外部Sourceから取得した未解釈の記事。重複取得を避け、元Evidenceを保つ。
- Event: 記事ではなく、記事から確認した企業変化。Source Evidenceと未確認事項を保持する。
- Opportunity: Eventを企業単位に束ねた営業候補。Gate、Research、Scoreを同じOpportunityへ追加する。
- VC Profile: 低頻度で更新するVC支援情報。Gate / Researchから参照する。
- Human Feedback: OpportunityとRunに結びつく、人間評価の独立データ。

実行コマンド:

```bash
# 固定情報源を1回取得してRaw Item Storeへ保存（AIなし）
docker compose run --rm sales-agent python -m app.main collect-fixed

# 設定された間隔で固定情報源だけを継続収集（既定3時間）
docker compose run --rm sales-agent python -m app.main collect-fixed-daemon

# 1日1回のWeb DiscoveryとOpportunity Pipeline（Raw Itemの収集は別コマンド）
docker compose run --rm sales-agent python -m app.main daily-run

# 必要なときだけ、日次実行前に固定情報源も収集
docker compose run --rm sales-agent python -m app.main daily-run --collect-before-run

# 旧CLI名も互換用に維持
docker compose run --rm sales-agent python -m app.main v2-run-once

# 検索結果、Gate、Research、Scoreを確認
docker compose run --rm sales-agent python -m app.main trace-opportunity --company '株式会社AAA'
```

固定情報源は@Press RSSとIncubate Fund公式News一覧から始めます。robots.txtを確認し、Sourceごとに上限を設けた少量のHTTP取得を行います。`collect-fixed`はOpenAI、Web Search、Discordを呼びません。

推奨運用は固定Collectorを3時間ごと、保存済みRaw Itemを処理する`daily-run`を1日1回です。Daily Pipelineは既定で収集を行わず、必要なら`--collect-before-run`で明示的に追加できます。実際のCronやsystemd timerは実行環境で設定します。Collectorの間隔は `FIXED_COLLECTOR_INTERVAL_HOURS` で調整できます。日次時刻は外部スケジューラが決めます。

VC Newsではサイト側カテゴリを優先し、ランキング掲載や注意喚起を出資と誤認しません。@Pressの単純商品、グッズ、単発イベント、出展、施工事例を候補から抑え、周年だけでは強いEventにしません。Fixed Event抽出の内部にPASS / HOLD / DROP、`event_strength`、柔軟なSource diversity routingを閉じ込めています。

主要DB入出力は `V2Repository` に集約しています。Daily PipelineはRaw ItemsとVC Profilesをロードし、Stage間ではPython Objectsを渡し、Runの結果・Gate・Research・Score・Prefilter判断を最後にまとめて保存します。`source_events` はRaw Item Store、`candidates` はOpportunityのスナップショットとして利用します。`source_event_prefilters`、`diagnostics`、`research_scores` は監査・旧V2互換用の投影テーブルとして残し、既存テーブルを削除しません。`human_ratings` (V1) と `human_feedback` (V2) は互換性のため残りますが、V2.2 Pipelineからの書き込みはありません。

V2.2の設計原則とSemantic Auditの概要は [DESIGN.md](DESIGN.md) を参照してください。

旧V2設定 (`SOURCE_PREFILTER_BATCH_SIZE`, `FIXED_DISCOVERY_BATCH_SIZE`, `FIXED_DISCOVERY_INCLUDE_HOLD_EVENTS`, `ATPRESS_PREFILTER_PASS_SCORE`, `ATPRESS_PREFILTER_DROP_SCORE`, `WIN_PRE_DROP_THRESHOLD`, `WIN_PRE_DIAGNOSTIC_THRESHOLD`, `PEER_RESEARCH_ENABLED`, `DIAGNOSTIC_INCLUDE_HOLD`, `V2_GENERATE_STRATEGY`) は引き続き利用されます。

ローカル実行時は `.env` の `DATABASE_URL=sqlite:///data/sales.db` にします。

## Known limitations

- 毎日12〜30社や営業精度は保証しません。候補0件も正常終了です。需要・予算・接触先は仮説で、人間評価が必要です。
- 出典URLの検索取得を検証しますが、記事本文と全主張の一致、公開日の正確性までは機械的に保証しません。戦略の追加調査はnotesと出典メタデータを分けて保存します。
- ドメインはwwwを除いたホスト単位です。サブドメイン、ブランド別サイト、同名法人の完全同定、異なる記事で言い換えられた同一イベントの意味的重複排除には対応しません。同じ記事URL・trigger_typeは同一トリガーとして扱います。
- 単一daemon運用を想定しています。手動実行と定時ジョブを重ねないでください。プロセス間の排他・複数レプリカ・停止中の全日程の追いつき実行はありません。
- 採点済みトリガーの戦略生成失敗は履歴に残しますが、次回の自動再生成はしません。Discord分割送信の途中失敗では一部のみ届く可能性があります。自動再送キューはありません。
- 強制終了するとrunningの履歴が残ることがあります。マイグレーション機構は未導入です。既存DBのスキーマ変更時はバックアップと移行作業が必要です。
- 実APIによる探索精度と実Webhookへの配信は未検証です。

### この開発環境での検証

Python 3.12のDocker内で全テストを外部通信なしで実行しています。ダミーキーでのrun-once失敗表示、daemonの常駐・SIGTERM終了、公式SDKを使ったモックHTTP通信も含みます。

このWSL環境はDocker BuildxおよびComposeが参照する `docker-credential-desktop.exe` が欠けており、標準の `docker compose up -d --build` のビルド段階は実行できませんでした。Docker Desktop/Buildxと認証ヘルパーの環境設定を修復すると通常のSetup手順を使用できます。この環境では `docker build -t adely-salesagent-sales-agent .` でイメージを作成し、`docker compose up -d --no-build` で常駐起動を確認しました。指定のrun-onceコマンドによるダミーキーのエラー表示とホストDB保存も確認し、サービスは停止済みです（タグ名は本リポジトリのディレクトリ名によるCompose既定名）。


## Discovery・Scoringの低コスト検証

少数カテゴリだけ探索し、Strategy・Discord送信を実行せずに採点まで確認できます。

```bash
python -m app.main run-once --scoring-only --topic リブランディング --discovery-cache data/discovery-v2.json
```

保存した同じ候補を使って、検索せずに採点だけ比較できます（ScoringのAPI利用は発生します）。
自分で保存・確認したローカルキャッシュだけを使用してください。

```bash
python -m app.main run-once --scoring-only --replay data/discovery-v2.json
```

`--topic` は複数指定可能です。キャッシュは受理した候補をカテゴリごとに保存し、指定ファイルを上書きします。
通常実行では既に新形式で採点したトリガーを省略します。replayでは既存候補も再採点します。

Strategyを生成せず、Scoring後の上位候補だけをDiscordへ送る場合は `--scoring-report` を使います。`--scoring-only` と同時には指定できません。

```bash
python -m app.main run-once --scoring-report --topic '新規事業 発表'
```

全候補の調査メモと15項目の採点はDBの `research_scores.candidate_json` / `evaluation_json` に保存します。
旧 `scores` テーブルは維持され、新形式とは混在しません。画面ログには上位候補と段階別の平均・根拠充実度を表示します。

Discoveryでは企業規模、変化と時期、既存表現、SNS、制作体制の確認済み事実に出典を付けます。
調査メモの出典も検索ツールが返したURLと照合し、未確認URLを含む候補は除外します。
SNSアカウントの存在だけから継続運用・内製・既存制作会社を断定しません。
追加確認は有望候補あたり原則1ページ程度というプロンプト上の目安であり、厳密な検索回数・料金上限ではありません。
Scoringでは検索せず、NEED / WIN / DELIVER各5項目を評価します。未知情報は `unknowns` と根拠充実度へ保持し、一律に中間点へ変換しません。
順位用の参考点は各段階平均の幾何平均×10（0〜100）です。3段階を等しく扱う暫定ルールで、受注確率ではありません。
根拠充実度は参考点に乗算せず、各段階と併記して人間が判断します。
新形式の既定プロンプトバージョンはDiscovery・Scoringともv6です。環境変数で上書きしている場合も更新してください。
