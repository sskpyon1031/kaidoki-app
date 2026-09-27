"""テスタ流の考え方を参考にした「買い時」判定ロジック。

公開されている考え方をルールに落とし込んだもの:
  1. トレンドに逆らわない（上昇トレンドの銘柄だけを買う）
  2. 需給・出来高を重視する（買いが集まっている銘柄を買う）
  3. 押し目 or 高値ブレイクというタイミングで入る
  4. 過熱している銘柄に飛び乗らない
  5. 地合い（市場全体）が悪いときは無理をしない
  6. 入る前に損切りラインを決める。損切り幅が大きすぎるなら入らない
  7. 1回の損失は資金の一定割合に抑える（資金管理）
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yfinance as yf

MARKET_INDEX = "^N225"
BUY_THRESHOLD = 75
WATCH_THRESHOLD = 55


OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def normalize_ticker(code: str) -> str:
    """'7203' や '285A' のような日本株コードに '.T' を付ける。全角（７２０３）も受け付ける。"""
    code = unicodedata.normalize("NFKC", code).strip().upper()
    if re.fullmatch(r"\d{3}[0-9A-Z]", code):
        return code + ".T"
    return code


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """欠損行と重複日付を取り除く（Yahooのデータにはたまに混ざる）。"""
    if df.empty or not set(OHLCV) <= set(df.columns):
        return pd.DataFrame(columns=OHLCV)
    df = df[OHLCV].dropna(subset=["Open", "High", "Low", "Close"])
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch(ticker: str, period: str = "2y") -> pd.DataFrame:
    return clean(yf.Ticker(ticker).history(period=period, auto_adjust=False))


STOCK_LIST = Path(__file__).with_name("stocks_jp.csv")


def load_stock_names() -> dict[str, str]:
    """東証の銘柄コード → 銘柄名。stocks_jp.csv は update_stock_list.py で更新する。"""
    if not STOCK_LIST.exists():
        return {}
    df = pd.read_csv(STOCK_LIST, dtype=str)
    return dict(zip(df["コード"], df["銘柄名"]))


UNIVERSES = {
    "TOPIX100（大型株・約100銘柄）": {"TOPIX Core30", "TOPIX Large70"},
    "TOPIX500（大型〜中型株・約500銘柄）": {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"},
}


def load_universe(name: str) -> list[str]:
    """おすすめ探索の対象銘柄。流動性の高い（売買が多い）銘柄に絞る。"""
    df = pd.read_csv(STOCK_LIST, dtype=str).fillna("")
    return [c + ".T" for c in df.loc[df["規模"].isin(UNIVERSES[name]), "コード"]]


def parse_codes(text: str) -> list[str]:
    """カンマ・空白・改行区切りの銘柄コードを正規化して重複なく返す。"""
    codes = [normalize_ticker(c) for c in re.split(r"[\s,、，]+", text) if c.strip()]
    return list(dict.fromkeys(codes))


def fetch_many(tickers: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """複数銘柄をまとめて取得する（1銘柄ずつより速く、レート制限にもかかりにくい）。"""
    out = {}
    for start in range(0, len(tickers), 100):  # 大量に取るときは100銘柄ずつ
        chunk = tickers[start:start + 100]
        raw = yf.download(chunk, period=period, auto_adjust=False, group_by="ticker",
                          threads=True, progress=False)
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = clean(df)
            if not df.empty:
                out[t] = df
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA25"] = df["Close"].rolling(25).mean()
    df["MA75"] = df["Close"].rolling(75).mean()
    df["VolMA20"] = df["Volume"].rolling(20).mean()
    df["Dev25"] = (df["Close"] / df["MA25"] - 1) * 100
    df["High20"] = df["High"].rolling(20).max().shift(1)  # 前日までの20日高値
    df["High60"] = df["High"].rolling(60).max()
    df["Low10"] = df["Low"].rolling(10).min()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss)
    return df


def add_market(df: pd.DataFrame, mkt: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if mkt.empty:  # 日経平均が取れなかったときは地合い不明（地合いの点数は0点）として扱う
        df["MktClose"] = df["MktMA25"] = df["MktChg5"] = math.nan
        return df
    mkt = mkt[~mkt.index.duplicated(keep="last")].sort_index()
    m = mkt["Close"].reindex(df.index, method="ffill")
    df["MktClose"] = m
    df["MktMA25"] = mkt["Close"].rolling(25).mean().reindex(df.index, method="ffill")
    df["MktChg5"] = (m / m.shift(5) - 1) * 100
    return df


@dataclass
class Result:
    score: int
    verdict: str
    breakdown: dict[str, tuple[int, int]]  # 項目 -> (得点, 満点)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entry: float = math.nan
    stop: float = math.nan
    target: float = math.nan


def evaluate(df: pd.DataFrame, i: int = -1) -> Result:
    """df（指標・地合い付き）の i 行目時点で買い時を判定する。"""
    r = df.iloc[i]
    prev = df.iloc[i - 1]
    ma25_5ago = df["MA25"].iloc[i - 5]
    reasons: list[str] = []
    warnings: list[str] = []
    bd: dict[str, tuple[int, int]] = {}

    # 1. トレンド (25点)
    s = 0
    if r.Close > r.MA25:
        s += 10
        reasons.append("株価が25日線の上にある（上昇基調）")
    else:
        warnings.append("株価が25日線の下。下落トレンドの銘柄は買わない")
    if r.MA25 > ma25_5ago:
        s += 8
        reasons.append("25日線が上向き")
    if r.MA25 > r.MA75:
        s += 7
        reasons.append("25日線が75日線より上（中期も上昇）")
    bd["トレンド"] = (s, 25)
    uptrend = r.Close > r.MA25 and r.MA25 > ma25_5ago

    # 2. 需給・出来高 (20点)
    s = 0
    vol_ratio = r.Volume / r.VolMA20 if r.VolMA20 else 0
    up_day = r.Close > prev.Close
    if vol_ratio >= 1.5 and up_day:
        s = 20
        reasons.append(f"出来高が平均の{vol_ratio:.1f}倍で上昇（買いが集まっている）")
    elif vol_ratio >= 1.2 and up_day:
        s = 12
        reasons.append(f"出来高やや増加（平均の{vol_ratio:.1f}倍）で上昇")
    elif vol_ratio >= 1.5 and not up_day:
        warnings.append(f"出来高を伴って下落（平均の{vol_ratio:.1f}倍）。売り圧力に注意")
    else:
        s = 5
    bd["需給・出来高"] = (s, 20)

    # 3. エントリータイミング (20点): 押し目 or ブレイク
    s = 0
    # 押し目 = 25日線付近まで下げて反発、または前日5日線を割った後に陽線で5日線を回復
    touch_ma25 = r.Low <= r.MA25 * 1.02 and r.Close > r.MA25
    regain_ma5 = prev.Close < prev.MA5 and r.Close > r.MA5
    if uptrend and (touch_ma25 or regain_ma5) and r.Close > r.Open:
        s = 20
        reasons.append("上昇トレンド中の押し目（移動平均付近で反発）")
    if r.Close > r.High20 and vol_ratio >= 1.2:
        s = 20
        reasons.append("20日高値を出来高を伴ってブレイク")
    elif r.Close > r.High20:
        s = max(s, 10)
        reasons.append("20日高値をブレイク（出来高はまだ弱い）")
    bd["タイミング"] = (s, 20)

    # 4. 過熱感 (15点)
    s = 0
    if 40 <= r.RSI <= 70:
        s = 15
    elif 70 < r.RSI <= 80:
        s = 6
        warnings.append(f"RSI {r.RSI:.0f}。やや過熱")
    elif r.RSI > 80:
        warnings.append(f"RSI {r.RSI:.0f}。過熱。飛び乗りは危険")
    else:
        s = 5
        warnings.append(f"RSI {r.RSI:.0f}。弱い。反発を確認してから")
    if r.Dev25 > 15:
        s = 0
        warnings.append(f"25日線から+{r.Dev25:.1f}%乖離。高値掴みに注意")
    bd["過熱感"] = (s, 15)

    # 5. 地合い (20点)
    s = 0
    if pd.notna(r.MktMA25) and r.MktClose > r.MktMA25:
        s += 10
        reasons.append("日経平均が25日線の上（地合い良好）")
    else:
        warnings.append("日経平均が25日線の下。地合いが悪いときは無理しない")
    if pd.notna(r.MktChg5) and r.MktChg5 > -3:
        s += 10
    else:
        warnings.append("日経平均が直近5日で3%以上下落。様子見推奨")
    bd["地合い"] = (s, 20)

    score = sum(v[0] for v in bd.values())

    # 6. 損切りラインとリスクリワード
    entry = float(r.Close)
    stop = float(min(r.Low10, r.MA25 * 0.98)) if r.Close > r.MA25 else float(r.Low10)
    if stop >= entry:
        stop = entry * 0.95
    risk_pct = (entry - stop) / entry * 100
    # 上に直近60日高値があればそこが利益目標（上値の壁）。高値更新中なら損切り幅の2倍を目標にする
    target = float(r.High60) if r.High60 > entry else entry + 2 * (entry - stop)
    rr = (target - entry) / (entry - stop)
    if risk_pct > 10:
        score -= 15
        warnings.append(f"損切り幅が{risk_pct:.1f}%と大きい。入る前に損切りを決められない場所では買わない")
    if rr < 1.5:
        # 利益の見込みが損失より小さい（RR<1）なら買い時にはしない
        score -= 25 if rr < 1 else 10
        warnings.append(f"リスクリワード {rr:.1f}。直近高値（上値の壁）が近く、損失に対して利益の見込みが小さい")

    score = max(0, min(100, score))
    if score >= BUY_THRESHOLD:
        verdict = "買い時（エントリー検討）"
    elif score >= WATCH_THRESHOLD:
        verdict = "様子見（条件が揃うのを待つ）"
    else:
        verdict = "見送り"
    return Result(score, verdict, bd, reasons, warnings, entry, stop, target)


def score_history(df: pd.DataFrame, days: int = 250) -> pd.Series:
    """過去 days 日分のスコアを計算する（チャートに買いシグナルを表示するため）。"""
    start = max(80, len(df) - days)
    idx, vals = [], []
    for i in range(start, len(df)):
        idx.append(df.index[i])
        vals.append(evaluate(df, i).score)
    return pd.Series(vals, index=idx)


def position_size(capital: float, risk_pct: float, entry: float, stop: float,
                  unit: int = 100, fx: float = 1.0) -> int:
    """1回の損失を資金の risk_pct% に抑える株数（単元株単位）。資金で買える株数が上限。

    capital は円、entry/stop は現地通貨。fx は現地通貨→円のレート（日本株は1）。
    """
    per_share = (entry - stop) * fx
    if not (math.isfinite(per_share) and per_share > 0 and entry > 0):
        return 0
    shares = min(capital * risk_pct / 100 / per_share, capital / (entry * fx))
    return int(shares // unit * unit)
