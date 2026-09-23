"""
RSI Signal Bot for XAU/USD (Gold) -> sends BUY/SELL alerts to Telegram.
Runs on a schedule (e.g. every 5 minutes) via GitHub Actions - no server needed.

v2 changes (accuracy improvements):
  - EMA50(H1) trend filter: BUY only allowed when price is above the H1 trend,
    SELL only allowed when price is below it. Cuts fake reversal signals a lot.
  - Guard against using an unclosed/forming candle from the API.
  - RSI thresholds are now configurable constants (default made a bit stricter).

⚠ This is a template for educational/personal use, not a proven profitable
strategy. It only sends alerts; YOU decide whether to place the trade
manually in your MT5 app. Not financial advice.
"""

import os
import json
import sys
from datetime import datetime, timezone

import requests

# ---- Config from environment variables (set as GitHub Secrets) ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # optional, free tier -> AI analysis

SYMBOL       = "XAU/USD"
INTERVAL     = "5min"
RSI_PERIOD   = 14
OVERSOLD     = 30.0
OVERBOUGHT   = 70.0
STATE_FILE   = "state.json"

ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5   # Stop Loss  = ATR * this
ATR_TP_MULT  = 3.0   # Take Profit = ATR * this (risk:reward ~1:2)

# --- New: trend filter config ---
TREND_INTERVAL   = "1h"
TREND_EMA_PERIOD = 50
USE_TREND_FILTER = True   # set False to go back to old RSI-only behavior

# ---------------------------------------------------------------------------


def fetch_candles(interval=INTERVAL, outputsize=None):
    """Fetch recent candles from Twelve Data (most recent first -> reversed to
    chronological order). Twelve Data can include the currently-forming bar
    as the most recent entry, so callers that care about "closed" candles
    should use is_last_candle_closed() before trusting the final bar."""
    if outputsize is None:
        outputsize = RSI_PERIOD + ATR_PERIOD + 20
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Unexpected API response: {data}")
    candles = list(reversed(data["values"]))
    return candles


def is_last_candle_closed(last_candle_time_str, interval_minutes):
    """Twelve Data timestamps are UTC in 'YYYY-MM-DD HH:MM:SS' format for
    intraday intervals. A bar is only closed once interval_minutes have
    fully elapsed since it opened."""
    try:
        candle_open = datetime.strptime(last_candle_time_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        # Some responses use date-only for daily+ intervals; treat as closed.
        return True
    now = datetime.now(timezone.utc)
    elapsed_minutes = (now - candle_open).total_seconds() / 60.0
    return elapsed_minutes >= interval_minutes


def calculate_atr(highs, lows, closes, period=ATR_PERIOD):
    """Wilder's ATR. Returns a list aligned to closes (None where not enough data)."""
    atr = [None] * len(closes)
    if len(closes) <= period:
        return atr

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    avg_tr = sum(trs[:period]) / period
    atr[period] = avg_tr

    for i in range(period, len(trs)):
        avg_tr = (avg_tr * (period - 1) + trs[i]) / period
        atr[i + 1] = avg_tr

    return atr


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


def calculate_ema(values, period):
    """Standard EMA. Returns a list aligned to values (None where not enough data)."""
    ema = [None] * len(values)
    if len(values) < period:
        return ema

    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]

    return ema


def get_trend_direction():
    """Fetch H1 candles and return 'up', 'down', or None (not enough data /
    error -> caller should treat as 'unknown' and be conservative)."""
    try:
        candles = fetch_candles(interval=TREND_INTERVAL, outputsize=TREND_EMA_PERIOD + 20)
    except Exception as e:
        print(f"Trend fetch failed, skipping trend filter this run: {e}")
        return None

    closes = [float(c["close"]) for c in candles]
    ema_values = calculate_ema(closes, TREND_EMA_PERIOD)

    if ema_values[-1] is None:
        return None

    last_price = closes[-1]
    last_ema = ema_values[-1]
    return "up" if last_price > last_ema else "down"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_alerted_candle": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_ai_analysis(signal, price, sl, tp, rsi, atr, closes):
    """Ask Groq for a brief Persian opinion on whether this RSI signal looks
    reasonable, based on recent price action. Returns a short string, or None
    if AI is not configured / the call fails (never blocks sending the base
    signal)."""
    if not GROQ_API_KEY:
        return None

    recent_trend = closes[-10:]
    trend_desc = ", ".join(f"{c:.2f}" for c in recent_trend)

    prompt = (
        f"یک سیگنال معاملاتی خودکار روی طلا (XAU/USD) بر اساس RSI صادر شده:\n"
        f"جهت: {signal}\n"
        f"قیمت فعلی: {price:.2f}\n"
        f"RSI: {rsi:.1f}\n"
        f"ATR: {atr:.2f}\n"
        f"حد ضرر: {sl:.2f} | حد سود: {tp:.2f}\n"
        f"قیمت‌های ۱۰ کندل اخیر: {trend_desc}\n\n"
        f"فقط بر اساس همین داده‌های قیمتی (بدون دسترسی به اخبار بیرونی)، در حداکثر "
        f"۳ جمله‌ی فارسی و خیلی خلاصه بگو آیا این سیگنال با روند اخیر قیمت "
        f"هم‌راستاست یا در تضاده. لحن تحلیلی و محتاطانه باشه، نه توصیه قطعی."
    )

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return text or None
    except Exception as e:
        print(f"AI analysis skipped (error: {e})")
        return None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    resp.raise_for_status()


def main():
    candles = fetch_candles()

    # Drop the last candle if it's still forming (not fully closed yet).
    interval_minutes = int(INTERVAL.replace("min", ""))
    if candles and not is_last_candle_closed(candles[-1]["datetime"], interval_minutes):
        print("Last candle not fully closed yet, dropping it from calculations.")
        candles = candles[:-1]

    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    times  = [c["datetime"]     for c in candles]

    rsi_values = calculate_rsi(closes)
    atr_values = calculate_atr(highs, lows, closes)

    if (len(candles) < 2 or rsi_values[-1] is None or rsi_values[-2] is None
            or atr_values[-1] is None):
        print("Not enough data yet.")
        return

    rsi_prev, rsi_last = rsi_values[-2], rsi_values[-1]
    atr_last = atr_values[-1]
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

    # --- Trend filter: only allow the signal if it agrees with the H1 trend ---
    if signal and USE_TREND_FILTER:
        trend = get_trend_direction()
        if trend is None:
            print("Trend unknown (fetch/data issue) - skipping signal to be safe.")
            signal = None
        elif signal == "BUY" and trend != "up":
            print(f"BUY signal rejected: H1 trend is '{trend}', not 'up'.")
            signal = None
        elif signal == "SELL" and trend != "down":
            print(f"SELL signal rejected: H1 trend is '{trend}', not 'down'.")
            signal = None

    if signal:
        price = closes[-1]
        sl_dist = atr_last * ATR_SL_MULT
        tp_dist = atr_last * ATR_TP_MULT

        if signal == "BUY":
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist

        emoji = "🟢" if signal == "BUY" else "🔴"
        ai_note = get_ai_analysis(signal, price, sl, tp, rsi_last, atr_last, closes)

        msg = (
            f"{emoji} سیگنال {signal} روی XAUUSD (M5, هم‌راستا با روند H1)\n"
            f"قیمت ورود: {price:.2f}\n"
            f"حد ضرر (SL): {sl:.2f}\n"
            f"حد سود (TP): {tp:.2f}\n"
            f"RSI: {rsi_last:.1f}   |   ATR: {atr_last:.2f}\n"
            f"زمان کندل: {last_candle_time}\n"
        )
        if ai_note:
            msg += f"\n🤖 تحلیل هوش مصنوعی:\n{ai_note}\n"
        msg += (
            f"\n⚠ فقط یک سیگنال است، نه توصیه قطعی. تصمیم نهایی و مدیریت ریسک با خودت."
        )
        send_telegram(msg)
        print(f"Sent {signal} signal. SL={sl:.2f} TP={tp:.2f}")
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
