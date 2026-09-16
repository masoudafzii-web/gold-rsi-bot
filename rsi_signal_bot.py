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
DEEPSEEK_API_KEY    = os.environ.get("DEEPSEEK_API_KEY")  # optional, free credit -> AI analysis

SYMBOL       = "XAU/USD"
INTERVAL     = "5min"
RSI_PERIOD   = 14
OVERSOLD     = 30.0
OVERBOUGHT   = 70.0
STATE_FILE   = "state.json"

ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5   # Stop Loss  = ATR * this
ATR_TP_MULT  = 3.0   # Take Profit = ATR * this (risk:reward ~1:2)

# ---------------------------------------------------------------------------


def fetch_candles():
    """Fetch recent closed candles from Twelve Data (most recent first)."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": RSI_PERIOD + ATR_PERIOD + 20,  # extra bars for stable calcs
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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_alerted_candle": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_ai_analysis(signal, price, sl, tp, rsi, atr, closes):
    """Ask DeepSeek (free credit on signup, not US-restricted) for a brief
    Persian opinion on whether this RSI signal looks reasonable, based on
    recent price action. Returns a short string, or None if AI is not
    configured / the call fails (never blocks sending the base signal)."""
    if not DEEPSEEK_API_KEY:
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
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
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
    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    times  = [c["datetime"]     for c in candles]

    rsi_values = calculate_rsi(closes)
    atr_values = calculate_atr(highs, lows, closes)

    # Use the last two *closed* candles: index -2 (previous) and -1 (last closed)
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
            f"{emoji} سیگنال {signal} روی XAUUSD (M5)\n"
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
