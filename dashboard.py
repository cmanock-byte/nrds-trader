import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
import datetime
import pytz
from streamlit_autorefresh import st_autorefresh
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- 1. PAGE SETUP & AUTOREFRESH ---
st.set_page_config(page_title="NRDS Mean Reversion Bot", layout="wide")
st.title("NRDS $300 Challenge Dashboard 📈")

# Auto-refresh every 30 seconds (30,000 milliseconds)
count = st_autorefresh(interval=30000, limit=None, key="data_refresh")

# --- 2. API KEYS & CLIENTS ---
# Securely fetching keys from Streamlit Secrets
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# --- 3. FETCH DATA (Alpaca IEX 1-Min Data) ---
EST = pytz.timezone('America/New_York')
end_time = datetime.datetime.now(EST)
start_time = end_time - datetime.timedelta(days=3) # Get last 3 days for indicators

request_params = StockBarsRequest(
    symbol_or_symbols="NRDS",
    timeframe=TimeFrame.Minute,
    start=start_time,
    end=end_time,
    feed="iex"  # Required for free-tier users
)

bars = data_client.get_stock_bars(request_params)
df = bars.df.reset_index()
df.set_index('timestamp', inplace=True)
df.index = df.index.tz_convert('America/New_York')

# --- 4. CALCULATE INDICATORS ---
# Bollinger Bands (20, 2)
bbands = ta.bbands(df['close'], length=20, std=2)
df = pd.concat([df, bbands], axis=1)

# RSI (10)
df['RSI_10'] = ta.rsi(df['close'], length=10)

# VWAP
df['VWAP'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])

# Get latest values
latest = df.iloc[-1]
current_price = latest['close']
rsi_val = latest['RSI_10']
lower_bb = latest['BBL_20_2.0']
upper_bb = latest['BBU_20_2.0']

# --- 5. CHECK ACCOUNT & POSITION (Compounding Engine) ---
account = trading_client.get_account()
buying_power = float(account.buying_power)

try:
    position = trading_client.get_open_position('NRDS')
    current_qty = float(position.qty)
except Exception:
    current_qty = 0

# Max-Buy Logic (Seed $300 + gains)
# For the $300 challenge, we reinvest total dedicated equity.
# Note: You can adjust total_challenge_equity below if you separated the $300 virtually.
total_challenge_equity = 300.00 # Base tracking, update if tracking dynamically via order ledger
qty_to_buy = int(buying_power // current_price) # Uses max available cash

# --- 6. EARNINGS BLACKOUT & SIGNAL LOGIC ---
BLACKOUT_START = EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0))
BLACKOUT_END = EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59))
is_blackout_active = BLACKOUT_START <= end_time <= BLACKOUT_END

signal = "HOLD"
reason = "Awaiting technical triggers."

if is_blackout_active:
    st.error("⚠️ **EARNINGS BLACKOUT ACTIVE (May 4 - May 13)**")
    
    # Auto-liquidate if we are holding during blackout
    if current_qty > 0:
        signal = "SELL_LIQUIDATE"
        reason = "🚨 Blackout triggered. Liquidating open position to protect capital."
    else:
        signal = "STANDBY"
        reason = "Bot paused for earnings. Manual trading enabled."

else:
    # Standard Mean Reversion Logic
    if rsi_val < 30 and current_price < lower_bb:
        signal = "BUY"
        reason = f"RSI ({rsi_val:.2f}) < 30 AND Price (${current_price:.2f}) < Lower BB."
    elif current_qty > 0 and (rsi_val > 70 and current_price > upper_bb):
        signal = "SELL"
        reason = f"RSI ({rsi_val:.2f}) > 70 AND Price (${current_price:.2f}) > Upper BB."

# --- 7. ORDER EXECUTION ---
if signal == "BUY":
    try:
        buy_order = MarketOrderRequest(
            symbol="NRDS",
            qty=qty_to_buy,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=buy_order)
        st.success(f"Executed BUY for {qty_to_buy} shares at ~${current_price:.2f}")
    except Exception as e:
        st.error(f"Buy failed: {e}")

elif signal in ["SELL", "SELL_LIQUIDATE"]:
    try:
        sell_order = MarketOrderRequest(
            symbol="NRDS",
            qty=current_qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=sell_order)
        st.success(f"Executed SELL for {current_qty} shares. Reason: {reason}")
    except Exception as e:
        st.error(f"Sell failed: {e}")

# --- 8. DASHBOARD UI ---
col1, col2, col3, col4 = st.columns(4)
col1.metric("Current Price", f"${current_price:.2f}")
col2.metric("Current Position", f"{current_qty} Shares")
col3.metric("RSI (10)", f"{rsi_val:.2f}")
col4.metric("Current Signal", signal)

st.write(f"**Bot Status:** {reason}")

# --- 9. PLOTLY CHART ---
fig = go.Figure()

# Candlesticks
fig.add_trace(go.Candlestick(x=df.index,
                open=df['open'], high=df['high'],
                low=df['low'], close=df['close'],
                name='Price'))

# VWAP
fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='orange', width=2), name='VWAP'))

# Bollinger Bands
fig.add_trace(go.Scatter(x=df.index, y=df['BBU_20_2.0'], line=dict(color='gray', width=1, dash='dash'), name='Upper BB'))
fig.add_trace(go.Scatter(x=df.index, y=df['BBL_20_2.0'], line=dict(color='gray', width=1, dash='dash'), name='Lower BB', fill='tonexty', fillcolor='rgba(128,128,128,0.1)'))

fig.update_layout(title="NRDS Live Chart - 1 Min", xaxis_title="Time", yaxis_title="Price ($)", template="plotly_dark", xaxis_rangeslider_visible=False, height=600)
st.plotly_chart(fig, use_container_width=True)
