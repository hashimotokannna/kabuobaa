# Kabuobaa Web（第二段階）

毎晩、日本の全上場銘柄から「いつもより安い」上位100銘柄を自動選定し、
業種別の帳簿型Webページとして公開する仕組みです。
iPhoneではSafariでURLを開き「ホーム画面に追加」すればアプリのように使えます。

## 仕組み

```
GitHub Actions（毎晩 平日20:30 JST に自動実行）
  └─ screener.py
       1. JPX公式の上場銘柄一覧を取得（プライム/スタンダード/グロース 約3,900銘柄）
       2. 各銘柄の日足1年分をYahoo Financeから取得（40〜60分）
       3. 「終わった株」を除外（1年高値から40%以上下落、または長期下落トレンド継続）
       4. 直近20日高値からの下落率で上位100銘柄を選定
       5. docs/index.html（帳簿ページ）と docs/data.json を生成してコミット
GitHub Pages が docs/ を自動公開 → iPhoneから閲覧
```

## セットアップ手順（初回だけ・約10分）

1. GitHubアカウントを作成（無料）し、新しいリポジトリを作る
   - 名前は自由（例: `kabuobaa`）。**Public**にすると Actions が無制限に使えます
   - URLを知られたくない場合はPrivateでも可（無料枠 月2,000分。夜間バッチ約60分×平日で収まります。ただしPrivateのPagesは有料プランが必要な点に注意）
2. このフォルダの中身を丸ごとリポジトリにアップロード
   - **重要**: フォルダ直下にある `nightly.yml` は、リポジトリ上で
     `.github/workflows/nightly.yml` に配置してください（GitHubのWeb画面なら
     「Add file → Create new file」でファイル名に `.github/workflows/nightly.yml` と
     入力し、`nightly.yml` の中身を貼り付けるのが簡単です）
3. リポジトリの Settings → Pages → Source を「Deploy from a branch」、
   Branch を `main` / `docs` フォルダに設定して Save
4. Actions タブ → `nightly-screener` → 「Run workflow」で手動実行して動作確認
   - 初回は40〜60分かかります。完了すると docs/ にコミットが積まれます
5. `https://<ユーザー名>.github.io/<リポジトリ名>/` をiPhoneのSafariで開き、
   共有メニュー →「ホーム画面に追加」

以降は毎晩自動で更新されます。夜にホーム画面から開くだけです。

## 動作確認（ネット接続・GitHub不要）

```
python screener.py --demo
```

ダミーデータで `docs/index.html` を生成します。デザイン確認用。

## 調整したいとき

`screener.py` 冒頭の `CONFIG` の数字を変えるだけです。

| 設定 | 意味 | 初期値 |
|---|---|---|
| `TOP_N` | ピックアップ銘柄数 | 100 |
| `RECENT_DAYS` | 「普段の値段」とみなす期間 | 20営業日 |
| `CHEAP_PCT` / `MILD_PCT` | ◎/○の閾値（高値からの下落%） | 5% / 3% |
| `DEAD_DRAWDOWN` | 「終わった株」判定: 1年高値からの下落率 | 40% |
| `DEAD_BELOW_MA_RATIO` | 同: 200日線割れ継続の割合 | 90% |
| `MIN_TURNOVER` | 流動性の下限（平均売買代金/日） | 5,000万円 |

札幌・名古屋・福岡の単独上場銘柄を加えたい場合は `EXTRA_TICKERS` に追記します
（書き方の例はコード内コメント参照）。

## 注意

- 株価データはYahoo Financeの非公式な取得口を使っています。将来仕様変更で
  取れなくなる可能性があり、その場合はJPX公式のJ-Quants APIへの差し替えが正攻法です。
- このページは判断材料の表示のみで、投資判断はご自身で行ってください。
