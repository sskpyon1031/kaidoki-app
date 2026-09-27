"""JPX（日本取引所グループ）の東証上場銘柄一覧から stocks_jp.csv を作り直す。

新規上場や社名変更を反映したいときに実行する:
    python update_stock_list.py
"""
import io
import re
import unicodedata
import urllib.request
from pathlib import Path

import pandas as pd

PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
OUT = Path(__file__).with_name("stocks_jp.csv")
EXCLUDE = {"PRO Market", "出資証券"}


def main() -> None:
    html = urllib.request.urlopen(PAGE).read().decode("utf-8")
    link = re.search(r'href="([^"]+data_j\.xlsx?)"', html).group(1)
    data = urllib.request.urlopen("https://www.jpx.co.jp" + link).read()
    df = pd.read_excel(io.BytesIO(data), dtype=str)
    df = df[~df["市場・商品区分"].isin(EXCLUDE)]
    out = pd.DataFrame({
        "コード": df["コード"].str.strip(),
        # 全角英数字を半角に（ｉＦｒｅｅ → iFree）して検索しやすくする
        "銘柄名": df["銘柄名"].map(lambda s: unicodedata.normalize("NFKC", s).strip()),
        "市場": df["市場・商品区分"].str.replace("（内国株式）", "", regex=False),
    })
    out.to_csv(OUT, index=False, encoding="utf-8")
    print(f"{len(out)}銘柄を {OUT.name} に保存しました")


if __name__ == "__main__":
    main()
