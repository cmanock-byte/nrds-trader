import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
import datetime
import pytz
from streamlit_autorefresh import st_autorefresh
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ================================================================
# 1. PAGE SETUP & AUTOREFRESH
# ================================================================
st.set_page_config(page_title="NRDS Trading Bot", layout="wide")
st.title("NRDS Trading Bot 📈")

# Auto-refresh every 30 seconds
count = st_autorefresh(interval=30000, limit=None, key="data_refresh")

# ================================================================
# 2. API KEYS, MODE & CLIENTS
# ================================================================
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
PAPER_MODE = st.secrets.get("PAPER_MODE", "true").lower() == "true"
SEED_CAPITAL = float(st.secrets.get("SEED_CAPITAL", "300"))

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Mode indicator banner
if PAPER_MODE:
    st.success("📝 **PAPER TRADING MODE** - Simulated trades, no real money at risk.")
else:
    st.error("🔴 **LIVE TRADING MODE** - Real money. Real consequences.")

# ================================================================
# 3. TICKER CONFIGURATION
#
# This is the brain of the bot. Each ticker gets its own tuning
# parameters. The order matters - tickers listed first get
# priority when multiple BUY signals fire at the same time.
#
# To add a new ticker: copy any block, change the symbol and
# adjust the numbers. To remove one: delete its block.
#
# bb_std        = Bollinger Band width (lower = tighter = more signals)
# rsi_buy       = Buy when RSI drops below this number
# rsi_sell      = Sell when RSI rises above this number
# profit_target = Sell when price rises this much above entry price
# blackout_start/end = Earnings protection window (set to None if unknown)
# ================================================================
EST = pytz.timezone('America/New_York')

TICKERS = {
    "NRDS": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
        "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59)),
    },
    "OPFI": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
        "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59)),
    },
    "PTON": {
        "bb_std": 1.2,
        "rsi_buy": 30,
        "rsi_sell": 60,
        "profit_target": 0.06,
        "blackout_start": None,
        "blackout_end": None,
    },
    "OPEN": {
        "bb_std": 1.8,
        "rsi_buy": 30,
        "rsi_sell": 60,
        "profit_target": 0.10,
        "blackout_start": None,
        "blackout_end": None,
    },
    "PENN": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.12,
        "blackout_start": None,
        "blackout_end": None,
    },
    "PUBM": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": None,
        "blackout_end": None,
    },
}

# ================================================================
# 4. FETCH 1-MINUTE DATA FOR ALL TICKERS
# ================================================================
end_time = datetime.datetime.now(EST)
start_time = end_time - datetime.timedelta(days=3)

ticker_data = {}

for symbol, config in TICKERS.items():
    try:
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start_time,
            end=end_time,
            feed="iex"
        )
        bars = data_client.get_stock_bars(request_params)
        df = bars.df.reset_index()
        df.set_index('timestamp', inplace=True)
        df.index = df.index.tz_convert('America/New_York')

        # Bollinger Bands (20 period, custom std per ticker)
        bbands = ta.bbands(df['close'], length=20, std=config["bb_std"])
        df = pd.concat([df, bbands], axis=1)

        # RSI (6 period - fast, for 1-min scalping)
        df['RSI_6'] = ta.rsi(df['close'], length=6)

        # VWAP
        df['VWAP'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])

        # Safely detect dynamic Bollinger Band column names
        lower_bb_col = [col for col in df.columns if col.startswith('BBL')][0]
        upper_bb_col = [col for col in df.columns if col.startswith('BBU')][0]

        latest = df.iloc[-1]

        ticker_data[symbol] = {
            "df": df,
            "current_price": latest['close'],
            "rsi_val": latest['RSI_6'],
            "lower_bb": latest[lower_bb_col],
            "upper_bb": latest[upper_bb_col],
            "lower_bb_col": lower_bb_col,
            "upper_bb_col": upper_bb_col,
        }
    except Exception as e:
        st.warning(f"⚠️ Could not fetch data for {symbol}: {e}")

# ================================================================
# 5. CHECK ALL POSITIONS (Only 1 allowed at a time)
# ================================================================
current_position_symbol = None
current_qty = 0
unrealized_pl = 0.0
entry_price = 0.0

for symbol in TICKERS:
    try:
        position = trading_client.get_open_position(symbol)
        current_position_symbol = symbol
        current_qty = float(position.qty)
        unrealized_pl = float(position.unrealized_pl)
        entry_price = float(position.avg_entry_price)
        break
    except Exception:
        continue

# ================================================================
# 6. BUILD COMBINED TRADE LEDGER (All tickers, one equity curve)
# ================================================================
all_symbols = list(TICKERS.keys())
orders_req = GetOrdersRequest(
    status=QueryOrderStatus.CLOSED,
    symbols=all_symbols,
    limit=500
)
closed_orders = trading_client.get_orders(filter=orders_req)

trade_data = []
for o in closed_orders:
    if o.filled_qty and float(o.filled_qty) > 0:
        trade_data.append({
            "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
            "Symbol": o.symbol,
            "Side": o.side.name,
            "Qty": float(o.filled_qty),
            "Avg Price": float(o.filled_avg_price),
            "Status": o.status.name
        })

ledger_df = pd.DataFrame(trade_data)
if not ledger_df.empty:
    ledger_df = ledger_df.sort_values("Time").reset_index(drop=True)

# Calculate unified equity curve across all tickers
current_challenge_equity = SEED_CAPITAL
equity_curve = [{"Time": start_time.strftime("%Y-%m-%d %H:%M:%S"), "Equity": SEED_CAPITAL}]

if not ledger_df.empty:
    holdings = {}
    realized_pnl = 0
    for idx, row in ledger_df.iterrows():
        sym = row["Symbol"]
        qty = row["Qty"]
        price = row["Avg Price"]

        if sym not in holdings:
            holdings[sym] = {"qty": 0, "avg_cost": 0}

        if row["Side"] == "BUY":
            total_cost = (holdings[sym]["qty"] * holdings[sym]["avg_cost"]) + (qty * price)
            holdings[sym]["qty"] += qty
            if holdings[sym]["qty"] > 0:
                holdings[sym]["avg_cost"] = total_cost / holdings[sym]["qty"]
        elif row["Side"] == "SELL":
            trade_pnl = (price - holdings[sym]["avg_cost"]) * qty
            realized_pnl += trade_pnl
            holdings[sym]["qty"] -= qty
            if holdings[sym]["qty"] == 0:
                holdings[sym]["avg_cost"] = 0
            equity_curve.append({
                "Time": row["Time"],
                "Equity": SEED_CAPITAL + realized_pnl
            })
    current_challenge_equity = SEED_CAPITAL + realized_pnl

equity_df = pd.DataFrame(equity_curve)

# ================================================================
# 7. SIGNAL LOGIC FOR ALL TICKERS
#
# Rules:
#   - Only ONE position at a time across ALL tickers
#   - If we're flat (no position), scan all tickers for BUY signals
#   - If multiple BUY signals fire, the first ticker in TICKERS wins
#   - If we're holding, only that ticker can fire a SELL signal
# ================================================================
signals = {}
buy_candidate = None

for symbol, config in TICKERS.items():
    if symbol not in ticker_data:
        signals[symbol] = {"signal": "ERROR", "reason": "Data fetch failed."}
        continue

    td = ticker_data[symbol]
    price = td["current_price"]
    rsi = td["rsi_val"]
    lower_bb = td["lower_bb"]
    upper_bb = td["upper_bb"]

    # Check earnings blackout for this ticker
    is_blackout = False
    if config["blackout_start"] and config["blackout_end"]:
        is_blackout = config["blackout_start"] <= end_time <= config["blackout_end"]

    signal = "HOLD"
    reason = "Awaiting technical triggers."

    if is_blackout:
        if current_position_symbol == symbol and current_qty > 0:
            signal = "SELL_LIQUIDATE"
            reason = f"🚨 Earnings blackout active. Liquidating {symbol}."
        else:
            signal = "STANDBY"
            reason = f"Earnings blackout active for {symbol}."
    else:
        # BUY: only if we have ZERO open positions anywhere
        if current_position_symbol is None and (rsi < config["rsi_buy"] or price < lower_bb):
            signal = "BUY"
            reasons = []
            if rsi < config["rsi_buy"]:
                reasons.append(f"RSI ({rsi:.2f}) < {config['rsi_buy']}")
            if price < lower_bb:
                reasons.append(f"Price (${price:.2f}) < Lower BB (${lower_bb:.2f})")
            reason = "BUY Signal: " + " | ".join(reasons)

        # SELL: only if THIS ticker is the one we're holding
        elif current_position_symbol == symbol and current_qty > 0:
            pnl_per_share = price - entry_price

            # Exit Priority 1: Profit target
            if pnl_per_share >= config["profit_target"]:
                signal = "SELL"
                reason = f"💰 PROFIT TARGET HIT: +${pnl_per_share:.2f}/share (target: +${config['profit_target']:.2f})"

            # Exit Priority 2: Technical overbought
            elif rsi > config["rsi_sell"] or price > upper_bb:
                signal = "SELL"
                reasons = []
                if rsi > config["rsi_sell"]:
                    reasons.append(f"RSI ({rsi:.2f}) > {config['rsi_sell']}")
                if price > upper_bb:
                    reasons.append(f"Price (${price:.2f}) > Upper BB (${upper_bb:.2f})")
                reason = "SELL Signal: " + " | ".join(reasons)

    signals[symbol] = {"signal": signal, "reason": reason}

    # Track the first BUY candidate (priority = order in TICKERS dict)
    if signal == "BUY" and buy_candidate is None:
        buy_candidate = symbol

# ================================================================
# 8. ORDER EXECUTION
# ================================================================

# Execute BUY on the highest-priority ticker that fired
if buy_candidate and buy_candidate in ticker_data:
    price = ticker_data[buy_candidate]["current_price"]
    qty_to_buy = int(current_challenge_equity // price) if price > 0 else 0
    if qty_to_buy > 0:
        try:
            buy_order = MarketOrderRequest(
                symbol=buy_candidate,
                qty=qty_to_buy,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            )
            trading_client.submit_order(order_data=buy_order)
            st.success(f"✅ Executed BUY: {qty_to_buy} shares of **{buy_candidate}** at ~${price:.2f}")
        except Exception as e:
            st.error(f"Buy failed for {buy_candidate}: {e}")

# Execute SELL on whichever ticker we're holding
for symbol, sig_data in signals.items():
    if sig_data["signal"] in ["SELL", "SELL_LIQUIDATE"] and current_position_symbol == symbol and current_qty > 0:
        try:
            sell_order = MarketOrderRequest(
                symbol=symbol,
                qty=current_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            trading_client.submit_order(order_data=sell_order)
            st.success(f"✅ Executed SELL: {int(current_qty)} shares of **{symbol}**. {sig_data['reason']}")
        except Exception as e:
            st.error(f"Sell failed for {symbol}: {e}")

# ================================================================
# 9. DASHBOARD UI - PORTFOLIO OVERVIEW
# ================================================================
st.subheader("🏆 Portfolio Overview")
colA, colB, colC, colD = st.columns(4)
colA.metric("Starting Capital", f"${SEED_CAPITAL:.2f}")
colB.metric("Challenge Equity", f"${current_challenge_equity:.2f}", f"${current_challenge_equity - SEED_CAPITAL:.2f} PnL")

if current_position_symbol:
    colC.metric("Holding", f"{current_position_symbol}", f"{int(current_qty)} Shares")
    colD.metric("Open PnL", f"${unrealized_pl:.2f}")
else:
    colC.metric("Holding", "None", "Scanning all tickers...")
    colD.metric("Open PnL", "$0.00")

# Show profit target when holding a position
if current_position_symbol and entry_price > 0:
    target = TICKERS[current_position_symbol]["profit_target"]
    st.info(f"📍 Holding **{current_position_symbol}** | Entry: ${entry_price:.2f} | 🎯 Target: ${entry_price + target:.2f} (+${target:.2f}/share)")

st.markdown("---")

# ================================================================
# 10. SIGNAL SCANNER - All tickers at a glance
# ================================================================
st.subheader("📡 Signal Scanner")
signal_cols = st.columns(len(TICKERS))

for i, (symbol, sig_data) in enumerate(signals.items()):
    sig = sig_data["signal"]
    with signal_cols[i]:
        if symbol in ticker_data:
            price = ticker_data[symbol]["current_price"]
            rsi = ticker_data[symbol]["rsi_val"]
            if sig == "BUY":
                st.success(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n🟢 **{sig}**")
            elif sig in ["SELL", "SELL_LIQUIDATE"]:
                st.error(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n🔴 **{sig}**")
            elif sig == "STANDBY":
                st.warning(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n⚠️ **BLACKOUT**")
            else:
                st.info(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n⏳ **{sig}**")
        else:
            st.error(f"**{symbol}**\n\n❌ DATA ERROR")

st.markdown("---")

# ================================================================
# 11. PER-TICKER TABS + EQUITY CURVE + TRADE LOG
# ================================================================
tab_names = list(TICKERS.keys()) + ["📈 Equity Curve", "📋 Trade Log"]
tabs = st.tabs(tab_names)

# Individual ticker tabs with charts and stats
for i, symbol in enumerate(TICKERS.keys()):
    with tabs[i]:
        if symbol not in ticker_data:
            st.error(f"No data available for {symbol}.")
            continue

        td = ticker_data[symbol]
        config = TICKERS[symbol]
        sig_data = signals[symbol]
        df = td["df"]

        # Metrics row
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Price", f"${td['current_price']:.2f}")
        col2.metric("RSI (6)", f"{td['rsi_val']:.2f}")
        col3.metric("Signal", sig_data["signal"])
        qty_possible = int(current_challenge_equity // td["current_price"]) if td["current_price"] > 0 else 0
        col4.metric("Max Buy Qty", f"{qty_possible}")

        st.write(f"**Status:** {sig_data['reason']}")
        st.write(f"**Tuning:** BB(20, {config['bb_std']}) | RSI Buy < {config['rsi_buy']} | RSI Sell > {config['rsi_sell']} | Profit Target: ${config['profit_target']:.2f}")

        # Candlestick chart
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df.index, open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name='Price'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df['VWAP'],
            line=dict(color='orange', width=2), name='VWAP'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[td["upper_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Upper BB'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[td["lower_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Lower BB',
            fill='tonexty', fillcolor='rgba(128,128,128,0.1)'))
        fig.update_layout(
            title=f"{symbol} Live Chart - 1 Min",
            xaxis_title="Time", yaxis_title="Price ($)",
            template="plotly_dark",
            xaxis_rangeslider_visible=False, height=500)
        st.plotly_chart(fig, use_container_width=True)

# Equity Curve tab
with tabs[-2]:
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(
        x=equity_df["Time"], y=equity_df["Equity"],
        mode='lines+markers', name='Equity',
        line=dict(color='#00FF00', width=3)))
    fig_eq.update_layout(
        title=f"Compounding Growth from ${SEED_CAPITAL:.0f} Seed (All Tickers)",
        xaxis_title="Time", yaxis_title="Account Equity ($)",
        template="plotly_dark", height=500)
    st.plotly_chart(fig_eq, use_container_width=True)

# Trade Log tab
with tabs[-1]:
    if not ledger_df.empty:
        st.dataframe(ledger_df, use_container_width=True)
    else:
        st.info("No closed trades yet in the ledger.")
