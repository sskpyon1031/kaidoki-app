import hashlib
import hmac
import importlib
import unicodedata
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import logic

# Streamlit Cloud はGitHubの更新時に app.py だけ読み直し、logic.py は古いまま残ることがある。
# 食い違うと存在しない関数を呼んで落ちるので、毎回最新の logic.py を読み込み直す。
importlib.reload(logic)
# キャッシュの鍵に logic.py の中身を含め、判定ルールを変えたら古い採点結果を使わないようにする
LOGIC_VERSION = hashlib.md5(Path(logic.__file__).read_bytes()).hexdigest()

MAX_TICKERS = 30
DEFAULT_CODES = "7203, 6758, 9984, 8306, 6861"

st.set_page_config(page_title="買い時判定アプリ", page_icon="📈", layout="centered")


def check_password() -> bool:
    """secrets に password が設定されていればパスワードを要求する（公開URLでの無断利用対策）。"""
    try:
        expected = st.secrets.get("password")
    except Exception:
        expected = None
    if not expected or st.session_state.get("authed"):
        return True
    pw = st.text_input("パスワード", type="password")
    # 日本語のパスワードでも比較できるようにバイト列で比べる
    if pw and hmac.compare_digest(pw.encode("utf-8"), str(expected).encode("utf-8")):
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("パスワードが違います")
    return False


st.title("📈 買い時判定")
if not check_password():
    st.stop()
st.caption("テスタ氏が公に語っている考え方（損切り徹底・需給・地合い・トレンド・資金管理）を参考にしたルールで判定します。"
           "本人の判断ではなく、投資助言でもありません。最終判断はご自身で。")

with st.expander("⚙️ 資金管理の設定"):
    capital = st.number_input("投資資金（円）", min_value=0, value=1_000_000, step=100_000)
    risk_pct = st.slider("1回の許容損失（資金に対する%）", 0.5, 5.0, 1.0, 0.5)


# 取得に失敗したときは例外を投げてキャッシュさせない（失敗結果が10分〜3時間残り続けるのを防ぐ）
class FetchError(Exception):
    pass


@st.cache_data(ttl=600, show_spinner=False)
def _load_market() -> pd.DataFrame:
    df = logic.fetch(logic.MARKET_INDEX)
    if df.empty:
        raise FetchError("日経平均")
    return df


def load_market() -> pd.DataFrame:
    try:
        return _load_market()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def _load_many(tickers: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    data = logic.fetch_many(list(tickers))
    if tickers and not data:
        raise FetchError("全銘柄の取得に失敗")
    return data


def load_many(tickers: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    try:
        return _load_many(tickers)
    except Exception:
        return {}


@st.cache_data(ttl=600, show_spinner=False)
def _load_usdjpy() -> float:
    df = logic.fetch("JPY=X", period="5d")
    if df.empty:
        raise FetchError("ドル円")
    return float(df["Close"].iloc[-1])


def load_usdjpy() -> float | None:
    try:
        return _load_usdjpy()
    except Exception:
        return None


def is_intraday(last_date) -> bool:
    """最新データが東証の取引時間中（未確定）の値かどうか。"""
    now = pd.Timestamp.now(tz="Asia/Tokyo")
    return pd.Timestamp(last_date).date() == now.date() and (now.hour, now.minute) < (15, 30)


INTRADAY_NOTE = "取引時間中のため、最新の値は未確定（約20分遅れ）です。出来高が少なめに出るので、判定は15:30以降に確認するのが確実です。"


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    return logic.add_market(logic.add_indicators(df), load_market())


def verdict_color(score: int) -> str:
    if score >= logic.BUY_THRESHOLD:
        return "#16a34a"
    if score >= logic.WATCH_THRESHOLD:
        return "#ca8a04"
    return "#dc2626"


def render_detail(ticker: str, df: pd.DataFrame, show_signals: bool = True) -> None:
    res = logic.evaluate(df)
    color = verdict_color(res.score)
    st.markdown(
        f"<div style='padding:14px;border-radius:12px;border:2px solid {color};margin-bottom:12px'>"
        f"<div style='font-size:13px;opacity:.7'>{label(ticker)}　{df.index[-1]:%Y-%m-%d} 終値 {res.entry:,.1f}"
        f"　配当利回り {logic.dividend_yield(df):.2f}%（実績）</div>"
        f"<div style='font-size:26px;font-weight:700;color:{color}'>{res.verdict}</div>"
        f"<div style='font-size:18px'>スコア {res.score} / 100</div></div>",
        unsafe_allow_html=True,
    )

    is_jp = ticker.endswith(".T")
    if is_jp and is_intraday(df.index[-1]):
        st.warning(INTRADAY_NOTE)
    if df["MktClose"].isna().iloc[-1]:
        st.warning("日経平均のデータを取得できなかったため、地合いは0点として判定しています。")
    unit, fx = (100, 1.0) if is_jp else (1, load_usdjpy())
    c1, c2, c3 = st.columns(3)
    c1.metric("損切りライン", f"{res.stop:,.1f}", f"{(res.stop / res.entry - 1) * 100:.1f}%")
    c2.metric("利益目標", f"{res.target:,.1f}", f"+{(res.target / res.entry - 1) * 100:.1f}%")
    if fx is None:
        c3.metric("推奨株数", "—")
        st.warning("為替レートを取得できなかったため、推奨株数を計算できません。少し時間をおいて開き直してください。")
    else:
        shares = logic.position_size(capital, risk_pct, res.entry, res.stop, unit=unit, fx=fx)
        c3.metric("推奨株数", f"{shares:,} 株", f"最大損失 約{shares * (res.entry - res.stop) * fx:,.0f}円", delta_color="off")
        if not is_jp:
            st.caption(f"米国株はドル建て。株数は 1ドル={fx:,.1f}円 で換算しています。")
        if shares == 0:
            st.warning(f"{unit}株でも許容損失を超えるか、資金が足りません。見送りか設定の見直しを。")

    st.markdown("##### 採点の内訳")
    for name, (s, full) in res.breakdown.items():
        st.progress(s / full, text=f"{name}　{s} / {full}")

    st.markdown("##### ✅ 買い材料")
    for r in res.reasons or ["なし"]:
        st.markdown(f"- {r}")
    st.markdown("##### ⚠️ 注意点")
    for w in res.warnings or ["なし"]:
        st.markdown(f"- {w}")
    st.info("鉄則: 買う前に損切りラインを決める／ナンピンしない／地合いが悪いときは休むのも仕事。")

    view = df.iloc[-120:]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
    # ローソク足のカーソル表示は英語（open/high…）になるので、日本語の説明文を自前で作る
    hover = [f"始値 {o:,.1f}<br>高値 {h:,.1f}<br>安値 {l:,.1f}<br>終値 {c:,.1f}"
             for o, h, l, c in zip(view.Open, view.High, view.Low, view.Close)]
    fig.add_trace(go.Candlestick(x=view.index, open=view.Open, high=view.High, low=view.Low, close=view.Close,
                                 name="株価", text=hover, hoverinfo="x+text",
                                 increasing_line_color="#dc2626", decreasing_line_color="#2563eb"), 1, 1)
    for ma, name, c in [("MA5", "5日線", "#f59e0b"), ("MA25", "25日線", "#10b981"), ("MA75", "75日線", "#8b5cf6")]:
        fig.add_trace(go.Scatter(x=view.index, y=view[ma], name=name, line=dict(width=1.2, color=c),
                                 hovertemplate=f"{name} %{{y:,.1f}}<extra></extra>"), 1, 1)
    fig.add_hline(y=res.stop, line_dash="dash", line_color="#dc2626", annotation_text="損切り", row=1, col=1)
    fig.add_hline(y=res.target, line_dash="dash", line_color="#16a34a", annotation_text="目標", row=1, col=1)
    if show_signals:
        hist = logic.score_history(df, days=len(view))
        sig = hist[hist >= logic.BUY_THRESHOLD]
        if not sig.empty:
            fig.add_trace(go.Scatter(x=sig.index, y=df.loc[sig.index, "Low"] * 0.98, mode="markers",
                                     marker=dict(symbol="triangle-up", size=10, color="#16a34a"), name="買いシグナル",
                                     hovertemplate="買いシグナル<extra></extra>"), 1, 1)
    fig.add_trace(go.Bar(x=view.index, y=view.Volume, name="出来高", marker_color="#94a3b8",
                         hovertemplate="出来高 %{y:,.0f}<extra></extra>"), 2, 1)
    fig.update_layout(height=480, xaxis_rangeslider_visible=False, margin=dict(l=4, r=4, t=30, b=4),
                      legend=dict(orientation="h", y=1.08, font=dict(size=10)), dragmode=False,
                      hovermode="x unified", separators=".,")
    # 日付を「9/25」「2026/09/25」の形にする（初期設定だと Sep 25 のような英語表記になる）
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])], tickformat="%-m/%-d", hoverformat="%Y/%m/%d")
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


@st.cache_data(show_spinner=False)
def stock_names() -> dict[str, str]:
    return logic.load_stock_names()


NAMES = stock_names()
OPTIONS = [f"{code} {name}" for code, name in NAMES.items()]
OPTION_SET = set(OPTIONS)
SEARCH_HELP = "銘柄名の一部（例: トヨタ）か4桁コードを入力して候補から選択。米国株はティッカー（例: AAPL）を入力して確定"


def label(ticker: str) -> str:
    """'7203.T' → '7203 トヨタ自動車'。一覧にない銘柄はコードのみ。"""
    code = ticker.removesuffix(".T")
    name = NAMES.get(code) if ticker.endswith(".T") else None
    return f"{code} {name}" if name else code


def resolve(text: str) -> str:
    """候補から選んだ文字列、コード、または手入力した銘柄名をティッカーに変換する。"""
    # 全角英数字（７２０３、ＡＡＰＬ）は半角にしてから判定する
    head = unicodedata.normalize("NFKC", text).strip().split()[0]
    if head.upper().removesuffix(".T") in NAMES or not any(ord(ch) > 127 for ch in head):
        return logic.normalize_ticker(head)
    # 候補を選ばずに日本語の名前を確定した場合は、名前の部分一致で最初の銘柄を採用する
    for code, name in NAMES.items():
        if head in name:
            return code + ".T"
    return head


def result_row(ticker: str, res: logic.Result, df: pd.DataFrame) -> dict:
    return {"銘柄": label(ticker), "判定": res.verdict.split("（")[0], "スコア": res.score,
            "配当(%)": logic.dividend_yield(df), "終値": res.entry, "損切り": res.stop, "目標": res.target,
            "損益比": (res.target - res.entry) / (res.entry - res.stop), "_ticker": ticker}


def show_table(table: pd.DataFrame) -> None:
    st.dataframe(
        table.drop(columns="_ticker"), hide_index=True, width="stretch",
        column_config={
            "スコア": st.column_config.ProgressColumn("スコア", min_value=0, max_value=100, format="%d"),
            "配当(%)": st.column_config.NumberColumn("配当(%)", format="%.2f", help="実績配当利回り（直近1年の配当合計÷株価）"),
            "終値":st.column_config.NumberColumn(format="%.1f"),
            "損切り": st.column_config.NumberColumn(format="%.1f"),
            "目標": st.column_config.NumberColumn(format="%.1f"),
            "損益比": st.column_config.NumberColumn("損益比", format="%.1f", help="損失1に対する利益の見込み（リスクリワード）。1.5以上が目安"),
        },
    )


@st.cache_data(ttl=3 * 3600, show_spinner=False)
def _scan(universe: str, logic_version: str) -> tuple[pd.DataFrame, str]:
    """対象銘柄をすべて採点する。重いので3時間キャッシュ（全利用者で共有）。"""
    tickers = logic.load_universe(universe)
    if load_market().empty:
        raise FetchError("日経平均")
    data = logic.fetch_many(tickers, period="1y")
    # 取得できた銘柄が8割未満なら一時的な失敗とみなしてキャッシュしない
    if len(data) < len(tickers) * 0.8:
        raise FetchError(f"{len(data)}/{len(tickers)}銘柄しか取得できず")
    rows = [result_row(t, logic.evaluate(prepare(df)), df) for t, df in data.items() if len(df) >= 100]
    as_of = max(df.index[-1] for df in data.values()).strftime("%Y-%m-%d")
    return pd.DataFrame(rows), as_of


def scan(universe: str) -> tuple[pd.DataFrame, str]:
    try:
        return _scan(universe, LOGIC_VERSION)
    except Exception:
        return pd.DataFrame(), ""


def market_banner() -> None:
    mkt = load_market()
    if len(mkt) < 25:
        st.warning("日経平均のデータを取得できませんでした（地合い不明）。")
        return
    close, ma25 = mkt["Close"].iloc[-1], mkt["Close"].rolling(25).mean().iloc[-1]
    chg5 = (close / mkt["Close"].iloc[-6] - 1) * 100
    good = close > ma25 and chg5 > -3
    msg = (f"地合い：{'良好' if good else '悪化'}（日経平均 {close:,.0f}円、25日線{'より上' if close > ma25 else 'より下'}、"
           f"5日で{chg5:+.1f}%）")
    (st.success if good else st.warning)(msg)


tab_rec, tab_bulk, tab_single = st.tabs(["⭐ おすすめ", "📋 一括判定", "🔍 個別判定"])

with tab_rec:
    st.markdown("流動性の高い東証の銘柄から、今のスコアが高い順に「買い時」の銘柄を探します。")
    market_banner()
    universe = st.selectbox("探す対象", list(logic.UNIVERSES), index=1)
    min_yield = st.select_slider("配当利回りで絞り込み", options=[0.0, 2.0, 3.0, 3.5, 4.0, 5.0], value=0.0,
                                 format_func=lambda v: "指定なし" if v == 0 else f"{v:g}%以上")
    c1, c2 = st.columns(2)
    order = c1.segmented_control("並び順", ["スコア順", "配当利回り順"], default="スコア順")
    top_n = c2.segmented_control("表示件数", [10, 20, 50], default=10)
    if st.button("おすすめを探す", type="primary", width="stretch"):
        st.session_state.rec_universe = universe

    if st.session_state.get("rec_universe") == universe:
        with st.spinner("銘柄を採点中...（初回は1分ほどかかります。2回目以降はすぐ表示されます）"):
            table, as_of = scan(universe)
        if table.empty:
            st.error("データを取得できませんでした。時間をおいて再度お試しください。")
        else:
            if is_intraday(as_of):
                st.caption(f"{as_of} 取引時間中の値で{len(table)}銘柄を採点（最大3時間前の結果）")
                st.warning(INTRADAY_NOTE)
            else:
                st.caption(f"{as_of} 終値時点・{len(table)}銘柄を採点")

            if order == "配当利回り順":
                table = table.sort_values(["配当(%)", "スコア"], ascending=False)
            else:
                table = table.sort_values(["スコア", "損益比"], ascending=False)
            if min_yield:
                table = table[table["配当(%)"] >= min_yield]
            buys = table[table["スコア"] >= logic.BUY_THRESHOLD]
            cond = f"配当利回り{min_yield:g}%以上で" if min_yield else ""
            if buys.empty:
                st.info(f"今は{cond}「買い時」の銘柄がありません。休むも相場です。参考として{cond}スコア上位を表示します。")
                shown = table.sort_values(["スコア", "損益比"], ascending=False).head(5)
            else:
                st.markdown(f"**{cond}買い時 {len(buys)}銘柄**（{order or 'スコア順'}）")
                shown = buys.head(top_n or 10)
            if not shown.empty:
                st.caption("配当(%)は実績配当利回り（直近1年の配当合計÷株価）。予想配当や減配の予定は反映されません。")
                show_table(shown)
            pick = (st.selectbox("詳細を見る銘柄", shown["_ticker"].tolist(), format_func=label, key="rec_pick")
                    if not shown.empty else None)
            if pick:
                df = load_many((pick,)).get(pick)
                if df is not None and len(df) >= 100:
                    render_detail(pick, prepare(df))
                else:
                    st.error("この銘柄のデータを取得できませんでした。少し時間をおいて再度お試しください。")

with tab_bulk:
    # 銘柄リストはURLに保存する → ブックマークすれば携帯でも次回そのまま使える
    saved = logic.parse_codes(st.query_params.get("codes", DEFAULT_CODES))[:MAX_TICKERS]
    default = [label(t) for t in saved]
    picked = st.multiselect(
        "判定する銘柄", OPTIONS + [d for d in default if d not in OPTION_SET],
        default=default, accept_new_options=True, max_selections=MAX_TICKERS,
        placeholder="銘柄名やコードで検索", help=SEARCH_HELP + f"。最大{MAX_TICKERS}銘柄", key="bulk_pick",
    )
    if st.button("一括判定する", type="primary", width="stretch"):
        new = list(dict.fromkeys(resolve(p) for p in picked))
        st.query_params["codes"] = ",".join(t.removesuffix(".T") for t in new)
        st.session_state.bulk_codes = new

    tickers = st.session_state.get("bulk_codes", saved)
    if not tickers:
        st.info("判定する銘柄を選んでください。")

    with st.spinner(f"{len(tickers)}銘柄のデータを取得中..."):
        data = load_many(tuple(tickers))

    rows, prepared = [], {}
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < 100:
            continue
        prepared[t] = prepare(df)
        rows.append(result_row(t, logic.evaluate(prepared[t]), prepared[t]))

    missing = [t for t in tickers if t not in prepared]
    if missing:
        st.warning("取得できなかった銘柄: " + ", ".join(label(t) for t in missing))

    if rows:
        table = pd.DataFrame(rows).sort_values(["スコア", "損益比"], ascending=False)
        n_buy = (table["スコア"] >= logic.BUY_THRESHOLD).sum()
        st.markdown(f"**買い時 {n_buy}銘柄** / {len(table)}銘柄中")
        show_table(table)
        pick = st.selectbox("詳細を見る銘柄", table["_ticker"].tolist(), format_func=label, key="bulk_detail")
        if pick:
            render_detail(pick, prepared[pick], show_signals=True)

with tab_single:
    first = label("7203.T")
    choice = st.selectbox("銘柄（名前やコードで検索）", OPTIONS, index=OPTIONS.index(first) if first in OPTION_SET else None,
                          accept_new_options=True, placeholder="銘柄名やコードで検索", help=SEARCH_HELP)
    if choice:
        t = resolve(choice)
        with st.spinner(f"{label(t)} のデータを取得中..."):
            df = load_many((t,)).get(t)
        if df is None or len(df) < 100:
            st.error(f"「{choice}」のデータが取得できませんでした。候補から選び直すか、コードを確認してください。")
        else:
            render_detail(t, prepare(df))
