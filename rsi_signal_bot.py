"""
RSI Signal Bot for XAU/USD (Gold) -> sends BUY/SELL alerts to Telegram.
Runs on a schedule (e.g. every 15 minutes) via GitHub Actions - no server needed.

⚠ This is a template for educational/personal use, not a proven profitable
strategy. It only sends alerts; YOU decide whether to place the trade
manually in your MT5 app. Not financial advice.
"""

import os
import json
import sys
import requests

# ---- Config from environment variables (set as GitHub Secrets) ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]

SYMBOL       = "XAU/USD"
INTERVAL     = "15min"
RSI_PERIOD   = 14
OVERSOLD     = 30.0
OVERBOUGHT   = 70.0
STATE_FILE   = "state.json"

# ---------------------------------------------------------------------------


def fetch_candles():
    """Fetch recent closed candles from Twelve Data (most recent first)."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": RSI_PERIOD + 20,  # extra bars for a stable RSI calc
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Unexpected API response: {data}")
    # API returns newest first; reverse to chronological order
    candles = list(reversed(data["values"]))
    return candles


def calculate_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. closes = list of floats, oldest to newest.
    Returns a list of RSI values aligned to closes (None where not enough data)."""
    rsi = [None] * len(closes)
    if len(closes) <= period:
        return rsi

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
    rsi[period] = 100 - (100 / (1 + rs))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi[i + 1] = 100 - (100 / (1 + rs))

    return rsi


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_alerted_candle": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    resp.raise_for_status()


def main():
    candles = fetch_candles()
    closes = [float(c["close"]) for c in candles]
    times = [c["datetime"] for c in candles]

    rsi_values = calculate_rsi(closes)

    # Use the last two *closed* candles: index -2 (previous) and -1 (last closed)
    if len(candles) < 2 or rsi_values[-1] is None or rsi_values[-2] is None:
        print("Not enough data yet.")
        return

    rsi_prev, rsi_last = rsi_values[-2], rsi_values[-1]
    last_candle_time = times[-1]

    state = load_state()
    if state.get("last_alerted_candle") == last_candle_time:
        print("Already alerted for this candle, skipping.")
        return

    signal = None
    if rsi_prev < OVERSOLD <= rsi_last:
        signal = "BUY"
    elif rsi_prev > OVERBOUGHT >= rsi_last:
        signal = "SELL"

    if signal:
        price = closes[-1]
        emoji = "🟢" if signal == "BUY" else "🔴"
        msg = (
            f"{emoji} سیگنال {signal} روی XAUUSD (M15)\n"
            f"قیمت: {price:.2f}\n"
            f"RSI: {rsi_last:.1f}\n"
            f"زمان کندل: {last_candle_time}\n\n"
            f"⚠ فقط یک سیگنال است، نه توصیه قطعی. تصمیم نهایی و مدیریت ریسک با خودت."
        )
        send_telegram(msg)
        print(f"Sent {signal} signal.")
        state["last_alerted_candle"] = last_candle_time
        save_state(state)
    else:
        print(f"No signal. RSI={rsi_last:.1f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
