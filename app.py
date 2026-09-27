import hmac

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import logic

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
    if pw and hmac.compare_digest(pw, str(expected)):
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


@st.cache_data(ttl=600, show_spinner=False)
def load_market() -> pd.DataFrame:
    return logic.fetch(logic.MARKET_INDEX)


@st.cache_data(ttl=600, show_spinner=False)
def load_many(tickers: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    return logic.fetch_many(list(tickers))


@st.cache_data(ttl=600, show_spinner=False)
def load_usdjpy() -> float:
    return logic.fetch_usdjpy()


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
        f"<div style='font-size:13px;opacity:.7'>{ticker}　{df.index[-1]:%Y-%m-%d} 終値 {res.entry:,.1f}</div>"
        f"<div style='font-size:26px;font-weight:700;color:{color}'>{res.verdict}</div>"
        f"<div style='font-size:18px'>スコア {res.score} / 100</div></div>",
        unsafe_allow_html=True,
    )

    is_jp = ticker.endswith(".T")
    unit, fx = (100, 1.0) if is_jp else (1, load_usdjpy())
    shares = logic.position_size(capital, risk_pct, res.entry, res.stop, unit=unit, fx=fx)
    c1, c2, c3 = st.columns(3)
    c1.metric("損切りライン", f"{res.stop:,.1f}", f"{(res.stop / res.entry - 1) * 100:.1f}%")
    c2.metric("利益目標", f"{res.target:,.1f}", f"+{(res.target / res.entry - 1) * 100:.1f}%")
    c3.metric("推奨株数", f"{shares:,} 株", f"最大損失 約{shares * (res.entry - res.stop) * fx:,.0f}円", delta_color="off")
    if not is_jp:
        st.caption(f"米国株はドル建て。株数は 1ドル={fx:,.1f}円 で換算しています。")
    if shares == 0:
        st.warning(f"{unit}株でも許容損失を超えます。見送りか許容損失の見直しを。")

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
    fig.add_trace(go.Candlestick(x=view.index, open=view.Open, high=view.High, low=view.Low, close=view.Close,
                                 name="株価", increasing_line_color="#dc2626", decreasing_line_color="#2563eb"), 1, 1)
    for ma, c in [("MA5", "#f59e0b"), ("MA25", "#10b981"), ("MA75", "#8b5cf6")]:
        fig.add_trace(go.Scatter(x=view.index, y=view[ma], name=ma, line=dict(width=1.2, color=c)), 1, 1)
    fig.add_hline(y=res.stop, line_dash="dash", line_color="#dc2626", annotation_text="損切り", row=1, col=1)
    fig.add_hline(y=res.target, line_dash="dash", line_color="#16a34a", annotation_text="目標", row=1, col=1)
    if show_signals:
        hist = logic.score_history(df, days=len(view))
        sig = hist[hist >= logic.BUY_THRESHOLD]
        if not sig.empty:
            fig.add_trace(go.Scatter(x=sig.index, y=df.loc[sig.index, "Low"] * 0.98, mode="markers",
                                     marker=dict(symbol="triangle-up", size=10, color="#16a34a"), name="買いシグナル"), 1, 1)
    fig.add_trace(go.Bar(x=view.index, y=view.Volume, name="出来高", marker_color="#94a3b8"), 2, 1)
    fig.update_layout(height=480, xaxis_rangeslider_visible=False, margin=dict(l=4, r=4, t=30, b=4),
                      legend=dict(orientation="h", y=1.08, font=dict(size=10)), dragmode=False)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


tab_bulk, tab_single = st.tabs(["📋 一括判定", "🔍 個別判定"])

with tab_bulk:
    # 銘柄リストはURLに保存する → ブックマークすれば携帯でも次回そのまま使える
    saved = st.query_params.get("codes", DEFAULT_CODES)
    text = st.text_area("銘柄コード（カンマ・スペース・改行区切り）", saved, height=90,
                        help=f"日本株は4桁コード、米国株はティッカー。最大{MAX_TICKERS}銘柄")
    if st.button("一括判定する", type="primary", width="stretch"):
        st.query_params["codes"] = text.strip()
        st.session_state.bulk_codes = logic.parse_codes(text)

    tickers = st.session_state.get("bulk_codes") or logic.parse_codes(saved)
    if len(tickers) > MAX_TICKERS:
        st.warning(f"最大{MAX_TICKERS}銘柄までです。先頭{MAX_TICKERS}銘柄を判定します。")
        tickers = tickers[:MAX_TICKERS]

    with st.spinner(f"{len(tickers)}銘柄のデータを取得中..."):
        data = load_many(tuple(tickers))

    rows, prepared = [], {}
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < 100:
            continue
        df = prepare(df)
        prepared[t] = df
        r = logic.evaluate(df)
        rows.append({"銘柄": t.removesuffix(".T"), "判定": r.verdict.split("（")[0], "スコア": r.score,
                     "終値": r.entry, "損切り": r.stop, "目標": r.target})

    missing = [t for t in tickers if t not in prepared]
    if missing:
        st.warning("取得できなかった銘柄: " + ", ".join(missing))

    if rows:
        table = pd.DataFrame(rows).sort_values("スコア", ascending=False)
        n_buy = (table["スコア"] >= logic.BUY_THRESHOLD).sum()
        st.markdown(f"**買い時 {n_buy}銘柄** / {len(table)}銘柄中")
        st.dataframe(
            table, hide_index=True, width="stretch",
            column_config={
                "スコア": st.column_config.ProgressColumn("スコア", min_value=0, max_value=100, format="%d"),
                "終値": st.column_config.NumberColumn(format="%.1f"),
                "損切り": st.column_config.NumberColumn(format="%.1f"),
                "目標": st.column_config.NumberColumn(format="%.1f"),
            },
        )
        pick = st.selectbox("詳細を見る銘柄", table["銘柄"].tolist())
        if pick:
            t = logic.normalize_ticker(pick)
            render_detail(t, prepared[t], show_signals=True)

with tab_single:
    code = st.text_input("銘柄コード", "7203", help="日本株は4桁コード（例: 7203）。米国株はティッカー（例: AAPL）")
    if code:
        t = logic.normalize_ticker(code)
        with st.spinner(f"{t} のデータを取得中..."):
            df = load_many((t,)).get(t)
        if df is None or len(df) < 100:
            st.error(f"{t} のデータが取得できませんでした。コードを確認してください。")
        else:
            render_detail(t, prepare(df))
