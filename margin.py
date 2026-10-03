"""JPXの「銘柄別信用取引残高」（毎営業日公表のPDF）から信用買い残・売り残を集める。

GitHub Actions で毎日実行し、data/margin_history.csv に追記する:
    python margin.py
アプリはこのCSVを読むだけなので、PDFの読み取り（約50秒）を待たずに済む。
"""
from __future__ import annotations

import io
import re
import urllib.request
from pathlib import Path

import pandas as pd

PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
HISTORY = Path(__file__).with_name("data") / "margin_history.csv"
KEEP_DAYS = 30  # 保存する営業日数（推移の確認用。多すぎるとファイルが大きくなる）

# PDFの1銘柄分の行（株数）:
#   "... 13010 JP3257200000 株数 Shs. 8,800 ▲ 200 0.1% 157,700 1,100 1.3% ..."
#   新規上場銘柄は前日比が "-" になる
_ROW = re.compile(
    r"(\w{4})0 JP\w{10} 株数 Shs\. ([\d,]+) (?:▲ ?)?(?:[\d,]+|-) \S+ ([\d,]+) "
)
_DATE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2}) 申込み現在")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=60).read()


def pdf_links() -> list[str]:
    html = _get(PAGE).decode("utf-8")
    return ["https://www.jpx.co.jp" + p for p in dict.fromkeys(re.findall(r'href="([^"]+_mtall\.pdf)"', html))]


def parse_pdf(data: bytes) -> pd.DataFrame:
    """PDFを読み取り [日付, コード, 売残, 買残]（株数）の表にする。"""
    from pypdf import PdfReader  # アプリ本体では使わないので、ここで読み込む

    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    y, m, d = _DATE.search(text).groups()
    rows = [(code, int(sell.replace(",", "")), int(buy.replace(",", ""))) for code, sell, buy in _ROW.findall(text)]
    df = pd.DataFrame(rows, columns=["コード", "売残", "買残"]).drop_duplicates("コード")
    df.insert(0, "日付", f"{y}-{int(m):02d}-{int(d):02d}")
    return df


def load_history() -> pd.DataFrame:
    if not HISTORY.exists():
        return pd.DataFrame(columns=["日付", "コード", "売残", "買残"])
    return pd.read_csv(HISTORY, dtype={"コード": str})


def update() -> None:
    hist = load_history()
    have = set(hist["日付"])
    new = []
    for url in pdf_links():
        day = parse_pdf(_get(url))
        if day["日付"].iloc[0] in have:
            continue
        print(f"{day['日付'].iloc[0]}: {len(day)}銘柄を追加")
        new.append(day)
    if not new:
        print("新しいデータはありません")
        return
    hist = pd.concat([hist, *new], ignore_index=True)
    days = sorted(hist["日付"].unique())[-KEEP_DAYS:]
    hist = hist[hist["日付"].isin(days)].sort_values(["日付", "コード"])
    HISTORY.parent.mkdir(exist_ok=True)
    hist.to_csv(HISTORY, index=False, encoding="utf-8")
    print(f"{HISTORY.name}: {len(days)}営業日分・{len(hist)}行")


if __name__ == "__main__":
    update()
