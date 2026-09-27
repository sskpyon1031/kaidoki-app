# 買い時判定アプリ

テスタ氏が公に語っている考え方（損切り徹底・需給・地合い・トレンド・資金管理）を参考にしたルールで、
指定銘柄の「買い時」を100点満点で判定するアプリです。本人の判断ではなく、投資助言でもありません。

- ⭐ おすすめ：TOPIX100 / TOPIX500 の銘柄をすべて採点し、「買い時」の銘柄をスコア順に表示（結果は3時間キャッシュ）
- 📋 一括判定：複数銘柄をまとめて採点し、スコア順に並べる（最大30銘柄）
- 🔍 個別判定：1銘柄の詳細（損切りライン・利益目標・推奨株数・チャート）

銘柄は名前の一部（例: トヨタ）やコードで検索できます。米国株はティッカー（例: AAPL）を入力します。

## 銘柄一覧を最新にする

新規上場や社名変更を反映するには、JPX の東証上場銘柄一覧から `stocks_jp.csv` を作り直して GitHub に反映します（月1回程度でOK）。

```
pip install openpyxl
python update_stock_list.py
```

## ローカルで動かす

```
pip install -r requirements.txt
python -m streamlit run app.py
```

## 公開する（Streamlit Community Cloud・無料）

1. GitHub で新しいリポジトリを作り、このフォルダのファイルをアップロード（`.streamlit/config.toml` も含める）
2. https://share.streamlit.io に GitHub アカウントでログイン →「Create app」
3. リポジトリ・ブランチ・`app.py` を選ぶ。「Advanced settings」で Python 3.12 を選ぶ
4. 同じ画面の「Secrets」にパスワードを設定（他人に使われないようにするため・推奨）

   ```toml
   password = "好きなパスワード"
   ```

5. 「Deploy」→ 発行された URL を携帯のブラウザで開き、ホーム画面に追加

一括判定で選んだ銘柄はブラウザに保存され、同じ端末・同じブラウザで次に開いたときに自動で復元されます。
