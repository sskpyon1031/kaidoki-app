"""テスタ流の考え方を参考にした「買い時」判定ロジック。

公開されている考え方をルールに落とし込んだもの:
  1. トレンドに逆らわない（上昇トレンドの銘柄だけを買う）
  2. 需給・出来高を重視する（買いが集まっている銘柄を買う）
  3. 押し目 or 高値ブレイクというタイミングで入る
  4. 過熱している銘柄に飛び乗らない
  5. 地合い（市場全体）が悪いときは無理をしない
  6. 入る前に損切りラインを決める。損切り幅が大きすぎるなら入らない
  7. 1回の損失は資金の一定割合に抑える（資金管理）
  8. 中長期の下落相場の銘柄は、短期で反発しても買わない
  9. 信用買い残が多い（将来の売り圧力）・上値にしこりがある銘柄は割り引いて考える
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
        return pd.DataFrame(columns=OHLCV + ["Dividends"])
    df = df.copy()
    df["Dividends"] = df["Dividends"].fillna(0) if "Dividends" in df else 0.0
    if "Stock Splits" in df:
        # Yahooは株式分割と同じ日の配当だけ分割前の金額のまま（例: 日本製鉄 2025/9/29 の60円→正しくは12円）
        split = df["Stock Splits"].fillna(0)
        same_day = (split > 0) & (df["Dividends"] > 0)
        df.loc[same_day, "Dividends"] = df.loc[same_day, "Dividends"] / split[same_day]
    df = df[OHLCV + ["Dividends"]].dropna(subset=["Open", "High", "Low", "Close"])
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
    "大型株（約100銘柄）": {"TOPIX Core30", "TOPIX Large70"},
    "大型〜中型株（約500銘柄）": {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"},
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
        raw = yf.download(chunk, period=period, auto_adjust=False, actions=True, group_by="ticker",
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
class Supply:
    """信用残などの需給の情報（日本株のみ）。"""
    date: str            # 信用残の基準日
    buy: int             # 信用買い残（株）
    sell: int            # 信用売り残（株）
    buy_chg: float       # 信用買い残の増減率（%）。比較できる過去データがなければ nan
    chg_days: int        # buy_chg が何営業日前との比較か
    ratio: float         # 信用倍率 = 買い残 ÷ 売り残
    buy_days: float      # 買い残が平均出来高（20日）の何日分か
    overhead: float      # 価格帯別出来高のうち、現在値〜+10%で売買された割合（%）
    overhead_price: float  # 現在値より上で最も出来高が多い価格帯の中心


def volume_profile(df: pd.DataFrame, days: int = 120, bins: int = 24) -> pd.DataFrame:
    """価格帯別出来高。各日の出来高をその日の平均的な価格（高値・安値・終値の平均）に割り当てる。"""
    w = df.tail(days)
    tp = (w["High"] + w["Low"] + w["Close"]) / 3
    edges = pd.interval_range(float(w["Low"].min()), float(w["High"].max()) * 1.0001, periods=bins)
    vol = w["Volume"].groupby(pd.cut(tp, edges), observed=False).sum()
    return pd.DataFrame({"下限": [iv.left for iv in vol.index], "上限": [iv.right for iv in vol.index],
                         "出来高": vol.to_numpy()})


def overhead_ratio(df: pd.DataFrame) -> float:
    """直近半年の出来高のうち、現在値〜+10%の価格帯で売買された割合（%）。上値のしこりの目安。"""
    w = df.tail(120)
    tp = (w["High"] + w["Low"] + w["Close"]) / 3
    close = float(df["Close"].iloc[-1])
    total = w["Volume"].sum()
    return float(w["Volume"][(tp > close) & (tp <= close * 1.10)].sum() / total * 100) if total else 0.0


def supply_metrics(df: pd.DataFrame, margin: pd.DataFrame | None) -> Supply | None:
    """margin は margin.py で集めたその銘柄の信用残の履歴（日付, 売残, 買残）。"""
    if margin is None or margin.empty:
        return None
    m = margin.sort_values("日付")
    now = m.iloc[-1]
    past = m.iloc[max(0, len(m) - 6)]  # 5営業日前（なければ一番古い日）
    chg_days = len(m) - 1 - max(0, len(m) - 6)
    buy, sell = int(now["買残"]), int(now["売残"])
    buy_chg = (buy / past["買残"] - 1) * 100 if chg_days and past["買残"] else math.nan
    vol20 = df["Volume"].tail(20).mean()

    close = float(df["Close"].iloc[-1])
    overhead = overhead_ratio(df)
    prof = volume_profile(df)
    above = prof[(prof["下限"] + prof["上限"]) / 2 > close]
    overhead_price = float(((above["下限"] + above["上限"]) / 2)[above["出来高"].idxmax()]) if len(above) else math.nan
    return Supply(str(now["日付"]), buy, sell, buy_chg, chg_days,
                  buy / sell if sell else math.inf, buy / vol20 if vol20 else math.nan, overhead, overhead_price)


def supply_adjust(sup: Supply, price_chg5: float) -> tuple[int, list[str], list[str]]:
    """需給による加点・減点。しきい値は TOPIX500 の分布（2026/10/1時点）から決めた。"""
    adj, reasons, warnings = 0, [], []
    if sup.buy_days >= 1.5:
        adj -= 10
        warnings.append(f"信用買い残が出来高の{sup.buy_days:.1f}日分と多い。将来の売り圧力（しこり）が大きい")
    elif sup.buy_days >= 0.9:
        adj -= 5
        warnings.append(f"信用買い残が出来高の{sup.buy_days:.1f}日分とやや多い")
    if sup.ratio >= 30 and sup.buy_days >= 0.5:
        adj -= 5
        warnings.append(f"信用倍率{sup.ratio:.0f}倍と買いに偏っている。上がると利益確定の売りが出やすい")
    elif sup.ratio < 1:
        adj += 5
        reasons.append(f"信用倍率{sup.ratio:.2f}倍で売り残の方が多い。買い戻し（踏み上げ）が入りやすい")
    if pd.notna(sup.buy_chg) and sup.buy_chg >= 10 and price_chg5 < 0:
        adj -= 5
        warnings.append(f"株価が下がる中で信用買い残が{sup.chg_days}日で{sup.buy_chg:+.0f}%増加。投げ売りの予備軍に注意")
    elif pd.notna(sup.buy_chg) and sup.buy_chg <= -5 and price_chg5 > 0:
        reasons.append(f"信用買い残が{sup.chg_days}日で{sup.buy_chg:+.0f}%減りながら上昇（売り圧力が軽くなっている）")
    if sup.overhead >= 65:
        adj -= 5
        warnings.append(f"現在値〜+10%の価格帯で直近半年の出来高の{sup.overhead:.0f}%が売買されている。"
                        f"{sup.overhead_price:,.0f}円付近に戻り売りのしこり")
    return adj, reasons, warnings


def supply_comment(sup: Supply | None, overhead: float, adj: int) -> tuple[str, list[str]]:
    """需給の数字を、初心者にもわかる文章にまとめる。（見出し, 説明の行）を返す。

    sup が None（米国株・信用残データなし）のときは、価格帯別出来高だけで書く。
    """
    lines = []
    if sup is not None:
        if sup.buy_days >= 0.9:
            lines.append(f"信用買い残が出来高の{sup.buy_days:.1f}日分と多めです。いずれ売られる株が多く、上値が重くなりやすい状態です。")
        elif sup.buy_days >= 0.3:
            lines.append(f"信用買い残は出来高の{sup.buy_days:.1f}日分で、ふつうの水準です。")
        else:
            lines.append(f"信用買い残は出来高の{sup.buy_days:.1f}日分と少なく、将来の売り圧力は軽い状態です。")

        if math.isinf(sup.ratio):
            lines.append("信用売り残がないため、信用倍率は計算できません。")
        elif sup.ratio < 1:
            lines.append(f"信用倍率は{sup.ratio:.2f}倍で、売り残の方が多い「売り長」です。株価が上がると売った人の買い戻しが入り、上昇に弾みがつきやすくなります。")
        elif sup.ratio < 5:
            lines.append(f"信用倍率は{sup.ratio:.1f}倍で、買いと売りのバランスは比較的とれています。")
        elif sup.ratio < 30:
            lines.append(f"信用倍率は{sup.ratio:.1f}倍で、やや買いに偏っています。")
        else:
            lines.append(f"信用倍率は{sup.ratio:.0f}倍で、大きく買いに偏っています。上がったところで利益確定の売りが出やすくなります。")

        if pd.notna(sup.buy_chg):
            if sup.buy_chg >= 10:
                lines.append(f"信用買い残が{sup.chg_days}日で{sup.buy_chg:+.0f}%増えました。下がったところを信用で買う人が増えていて、さらに下がると投げ売りが出やすくなります。")
            elif sup.buy_chg <= -5:
                lines.append(f"信用買い残が{sup.chg_days}日で{sup.buy_chg:+.0f}%減りました。決済が進み、売り圧力が軽くなっています。")
            else:
                lines.append(f"信用買い残は{sup.chg_days}日で{sup.buy_chg:+.0f}%と、大きな変化はありません。")

    if overhead >= 65:
        lines.append(f"現在値のすぐ上（+10%まで）で、直近半年の出来高の{overhead:.0f}%が売買されています。そこで買って含み損の人が多く、株価が戻ると「やれやれ売り」が出やすい価格帯です。")
    elif overhead <= 20:
        lines.append(f"現在値のすぐ上（+10%まで）で売買された量は半年分の{overhead:.0f}%と少なく、上値は軽い状態です。")
    else:
        lines.append(f"現在値のすぐ上（+10%まで）で売買された量は半年分の{overhead:.0f}%で、ふつうの水準です。")

    if sup is None:  # 信用残がないときは上値のしこりだけで判断する
        adj = -5 if overhead >= 65 else 0
    if adj > 0:
        head = "需給は良好です"
    elif adj < 0:
        head = "需給に注意が必要です"
    else:
        head = "需給に大きな問題はありません"
    return head, lines


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
    supply: Supply | None = None
    supply_adj: int = 0  # 需給による加点・減点


def evaluate(df: pd.DataFrame, i: int = -1, margin: pd.DataFrame | None = None) -> Result:
    """df（指標・地合い付き）の i 行目時点で買い時を判定する。

    margin（信用残の履歴）を渡すと需給も判定に反映する。信用残は最新分しかないので i=-1 のときだけ使う。
    """
    r = df.iloc[i]
    prev = df.iloc[i - 1]
    ma25_5ago = df["MA25"].iloc[i - 5]
    ma75_20ago = df["MA75"].iloc[i - 20]
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
        warnings.append(f"買われすぎ度 {r.RSI:.0f}（100に近いほど過熱）。やや過熱")
    elif r.RSI > 80:
        warnings.append(f"買われすぎ度 {r.RSI:.0f}（100に近いほど過熱）。過熱。飛び乗りは危険")
    else:
        s = 5
        warnings.append(f"買われすぎ度 {r.RSI:.0f}と低く勢いが弱い。反発を確認してから")
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
        warnings.append(f"損益比 {rr:.1f}（損失1に対する利益の見込み）。直近高値（上値の壁）が近く、利益の見込みが小さい")

    # 7. 需給（信用残・価格帯別出来高）
    sup = supply_metrics(df.iloc[: len(df) + i + 1], margin) if i == -1 else None
    adj = 0
    if sup is not None:
        adj, sup_reasons, sup_warnings = supply_adjust(sup, (r.Close / df["Close"].iloc[i - 5] - 1) * 100)
        score += adj
        reasons += sup_reasons
        warnings += sup_warnings

    score = max(0, min(100, score))

    # 8. 中長期の下落相場（75日線の下で、75日線も下向き）は、短期で反発していても買い時にしない
    if r.Close < r.MA75 and r.MA75 < ma75_20ago:
        score = min(score, BUY_THRESHOLD - 1)
        warnings.insert(0, "中長期の下落トレンド中（75日線の下で75日線も下向き）。短期の反発は戻り売りに注意。買い時にはしない")

    if score >= BUY_THRESHOLD:
        verdict = "買い時（エントリー検討）"
    elif score >= WATCH_THRESHOLD:
        verdict = "様子見（条件が揃うのを待つ）"
    else:
        verdict = "見送り"
    return Result(score, verdict, bd, reasons, warnings, entry, stop, target, sup, adj)


def score_history(df: pd.DataFrame, days: int = 250) -> pd.Series:
    """過去 days 日分のスコアを計算する（チャートに買いシグナルを表示するため）。"""
    start = max(80, len(df) - days)
    idx, vals = [], []
    for i in range(start, len(df)):
        idx.append(df.index[i])
        vals.append(evaluate(df, i).score)
    return pd.Series(vals, index=idx)


def dividend_yield(df: pd.DataFrame) -> float:
    """実績配当利回り（%）= 直近1年の1株配当の合計 ÷ 最新の株価。

    予想配当ではなく過去1年の実績。減配・増配の予定や記念配当は反映されない。
    """
    if df.empty or "Dividends" not in df:
        return math.nan
    last = df.index[-1]
    paid = df.loc[df.index > last - pd.Timedelta(days=365), "Dividends"].sum()
    return float(paid / df["Close"].iloc[-1] * 100)


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
