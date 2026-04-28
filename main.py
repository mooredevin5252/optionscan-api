from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import math

app = Flask(__name__)
CORS(app)

TRADIER_KEY = "XbEAA6FWezNdDZXg0rIiHGK9V27e"
TRADIER_BASE = "https://api.tradier.com/v1"
HEADERS = {
    "Authorization": f"Bearer {TRADIER_KEY}",
    "Accept": "application/json"
}

def get_5min_bars(symbol):
    """Fetch real-time 5-min bars from Tradier"""
    try:
        end = datetime.now()
        start = end - timedelta(days=5)
        url = f"{TRADIER_BASE}/markets/timesales"
        params = {
            "symbol": symbol,
            "interval": "5min",
            "start": start.strftime("%Y-%m-%d 09:30"),
            "end": end.strftime("%Y-%m-%d 16:00"),
            "session_filter": "open"
        }
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        series = data.get("series")
        if not series or series == "null":
            return None
        bars_raw = series.get("data", [])
        if not bars_raw:
            return None
        if isinstance(bars_raw, dict):
            bars_raw = [bars_raw]
        bars = []
        for b in bars_raw:
            try:
                bars.append({
                    "o": float(b["open"]),
                    "h": float(b["high"]),
                    "l": float(b["low"]),
                    "c": float(b["close"]),
                    "v": int(b["volume"] or 0)
                })
            except:
                continue
        return bars if len(bars) >= 5 else None
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

def get_quote(symbol):
    """Get current quote from Tradier"""
    try:
        url = f"{TRADIER_BASE}/markets/quotes"
        r = requests.get(url, headers=HEADERS, params={"symbols": symbol}, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        quote = data.get("quotes", {}).get("quote", {})
        return {
            "price": float(quote.get("last", 0)),
            "open": float(quote.get("open", 0)),
            "high": float(quote.get("high", 0)),
            "low": float(quote.get("low", 0)),
            "volume": int(quote.get("volume", 0)),
            "avg_volume": int(quote.get("average_volume", 0)),
            "change_pct": float(quote.get("change_percentage", 0))
        }
    except:
        return None

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def vwap(bars):
    total_vol = sum(b["v"] for b in bars)
    if total_vol == 0:
        return None
    return sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / total_vol

def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        tr = max(bars[i]["h"] - bars[i]["l"],
                 abs(bars[i]["h"] - bars[i-1]["c"]),
                 abs(bars[i]["l"] - bars[i-1]["c"]))
        trs.append(tr)
    return sum(trs[-period:]) / period

def calc_rvol(bars):
    if len(bars) < 2:
        return 1.0
    avg = sum(b["v"] for b in bars[:-1]) / max(len(bars) - 1, 1)
    if avg == 0:
        return 1.0
    return round(bars[-1]["v"] / avg, 1)

def vol_status(rvol):
    if rvol >= 2.0:
        return "STRONG"
    elif rvol >= 1.5:
        return "CONFIRMED"
    elif rvol >= 1.0:
        return "WEAK"
    return "LOW"

def detect_patterns(bars):
    """Detect day trading patterns from 5-min bars"""
    if not bars or len(bars) < 10:
        return []

    signals = []
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    last = bars[-1]
    prev = bars[-2]
    avg_vol = sum(vols[-10:]) / 10 if len(vols) >= 10 else sum(vols) / len(vols)
    rvol = calc_rvol(bars)
    atr = calc_atr(bars, 14)
    vw = vwap(bars)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)

    body_l = abs(last["c"] - last["o"])
    body_p = abs(prev["c"] - prev["o"])
    upper_wick = last["h"] - max(last["c"], last["o"])
    lower_wick = min(last["c"], last["o"]) - last["l"]
    range_l = last["h"] - last["l"]

    price = last["c"]
    atr_val = atr or price * 0.01

    def make_sig(name, bias, conf, note):
        stop = (price - atr_val * 1.5) if bias == "Long" else (price + atr_val * 1.5)
        return {
            "name": name, "bias": bias, "conf": conf,
            "stop": round(stop, 2), "note": note,
            "rvol": rvol, "volStatus": vol_status(rvol),
            "volConfirmed": rvol >= 1.5
        }

    # ORB Breakout
    if len(bars) >= 6:
        orb_high = bars[0]["h"]
        orb_low = bars[0]["l"]
        if last["c"] > orb_high and last["v"] > avg_vol * 1.5:
            signals.append(make_sig("ORB Breakout", "Long", 80,
                "Broke opening range high on volume surge"))
        if last["c"] < orb_low and last["v"] > avg_vol * 1.5:
            signals.append(make_sig("ORB Breakdown", "Short", 77,
                "Broke opening range low on volume surge"))

    # VWAP
    if vw:
        if prev["c"] < vw and last["c"] > vw and last["v"] > avg_vol * 1.2:
            signals.append(make_sig("VWAP Reclaim", "Long", 78,
                "Price reclaimed VWAP with above-average volume"))
        if prev["c"] > vw and last["c"] < vw and last["v"] > avg_vol * 1.2:
            signals.append(make_sig("VWAP Rejection", "Short", 75,
                "Price failed VWAP on volume"))

    # Bull Flag
    if len(bars) >= 8:
        imp = bars[-8:-3]
        flg = bars[-3:]
        iup = all(imp[i]["c"] >= imp[i-1]["c"] for i in range(1, len(imp)))
        fdn = all(b["c"] <= imp[-1]["c"] for b in flg)
        fv = sum(b["v"] for b in flg) / len(flg)
        if iup and fdn and fv < avg_vol * 0.8:
            sl = min(b["l"] for b in flg)
            signals.append({
                "name": "Bull Flag", "bias": "Long", "conf": 82,
                "stop": round(sl, 2), "note": "Tight flag on low volume after strong impulse",
                "rvol": rvol, "volStatus": vol_status(rvol), "volConfirmed": rvol >= 1.5
            })

    # Bullish Engulfing
    if prev["c"] < prev["o"] and last["c"] > last["o"] and \
       last["o"] < prev["c"] and last["c"] > prev["o"] and last["v"] > avg_vol * 1.2:
        signals.append(make_sig("Bullish Engulfing", "Long", 81,
            "Green candle fully engulfs prior red candle"))

    # Bearish Engulfing
    if prev["c"] > prev["o"] and last["c"] < last["o"] and \
       last["o"] > prev["c"] and last["c"] < prev["o"] and last["v"] > avg_vol * 1.2:
        signals.append(make_sig("Bearish Engulfing", "Short", 81,
            "Red candle fully engulfs prior green candle"))

    # Hammer
    if lower_wick > body_l * 2 and upper_wick < body_l * 0.3 and \
       last["c"] > last["o"] and last["v"] > avg_vol * 1.1:
        signals.append(make_sig("Hammer", "Long", 76,
            "Long lower wick rejecting lower prices"))

    # Shooting Star
    if upper_wick > body_l * 2 and lower_wick < body_l * 0.3 and \
       last["c"] < last["o"] and last["v"] > avg_vol * 1.1:
        signals.append(make_sig("Shooting Star", "Short", 76,
            "Long upper wick rejecting higher prices"))

    # EMA Pullback
    if e9 and e21 and e9 > e21 and \
       abs(last["c"] - e21) / e21 < 0.005 and last["c"] > last["o"]:
        signals.append(make_sig("EMA Pullback", "Long", 74,
            "Bouncing off EMA21 in uptrend"))

    # Gap & Go
    if len(bars) >= 2:
        pc = bars[-2]["c"]
        gp = (last["o"] - pc) / pc * 100
        if gp > 1.5 and last["c"] > last["o"] and last["v"] > avg_vol * 2:
            signals.append(make_sig("Gap & Go", "Long", 85,
                f"Gapped up +{gp:.1f}% with massive volume"))
        if gp < -1.5 and last["c"] < last["o"] and last["v"] > avg_vol * 2:
            signals.append(make_sig("Gap & Go", "Short", 83,
                f"Gapped down {gp:.1f}% with massive volume"))

    return signals

def suggest_option(sym, price, bias, setup, stop_price):
    """Suggest an options contract"""
    is_call = bias == "Long"
    # Calculate strike
    if price > 200:
        snap = 5
    elif price > 50:
        snap = 2.5
    else:
        snap = 1
    if is_call:
        strike = round(math.ceil(price / snap) * snap + snap, 2)
    else:
        strike = round(math.floor(price / snap) * snap - snap, 2)

    # Expiry = next Friday (or 2 weeks out)
    today = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday < 2:
        days_to_friday += 7
    expiry = today + timedelta(days=days_to_friday + 7)
    exp_str = expiry.strftime("%b %-d")

    # Estimate premium (rough)
    iv = 0.45
    days_out = (expiry - today).days
    premium = price * iv * math.sqrt(days_out / 365) * 0.3
    premium = max(0.05, round(premium, 2))
    contract_cost = round(premium * 100)

    return {
        "type": "CALL" if is_call else "PUT",
        "strike": strike,
        "expStr": exp_str,
        "premStr": f"${premium}",
        "contractCost": contract_cost,
        "optStop": round(contract_cost * 0.5),
        "t1val": round(contract_cost * 1.75),
        "t2val": round(contract_cost * 2.0),
        "iv": f"{int(iv*100)}%",
        "delta": "~0.35",
        "daysOut": days_out
    }

TICKERS = [
    "SPY","QQQ","IWM","SOXL",
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA",
    "BAC","C","WFC","SOFI","HOOD",
    "PLTR","SNAP","UBER","RBLX","LYFT",
    "RIVN","NIO","F",
    "COIN","MARA","MSTR",
    "AAL","DAL","CCL","PENN",
    "MU","SMCI","QCOM","AVGO",
    "GME","SPCE"
]

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/scan/day")
def scan_day():
    """Scan all tickers for day trade signals using real-time Tradier data"""
    results = []
    spy_bars = get_5min_bars("SPY")
    spy_bias = "neutral"
    if spy_bars and len(spy_bars) >= 5:
        vw = vwap(spy_bars)
        closes = [b["c"] for b in spy_bars]
        e9 = ema(closes, 9)
        e21 = ema(closes, 21)
        last = spy_bars[-1]
        if vw and e9 and e21 and last["c"] > vw and e9 > e21:
            spy_bias = "bullish"
        elif vw and e9 and e21 and last["c"] < vw and e9 < e21:
            spy_bias = "bearish"

    for sym in TICKERS:
        bars = get_5min_bars(sym)
        if not bars or len(bars) < 5:
            continue
        price = bars[-1]["c"]
        if price < 3:
            continue
        patterns = detect_patterns(bars)
        for p in patterns:
            # Filter: only block when both sector and SPY strongly oppose
            if p["bias"] == "Long" and spy_bias == "bearish":
                continue
            if p["bias"] == "Short" and spy_bias == "bullish":
                continue
            conf = p["conf"]
            rvol = p["rvol"]
            if rvol >= 2.0:
                conf = min(97, conf + 5)
            elif rvol >= 1.5:
                conf = min(97, conf + 3)
            if spy_bias == "bullish" and p["bias"] == "Long":
                conf = min(97, conf + 3)
            if spy_bias == "bearish" and p["bias"] == "Short":
                conf = min(97, conf + 3)

            opt = suggest_option(sym, price, p["bias"], p["name"], p["stop"])
            atr = calc_atr(bars, 14)
            results.append({
                "sym": sym,
                "price": round(price, 2),
                "name": p["name"],
                "bias": p["bias"],
                "conf": conf,
                "stop": p["stop"],
                "note": p["note"],
                "opt": opt,
                "mode": "day",
                "time": datetime.now().strftime("%I:%M:%S %p"),
                "rvol": rvol,
                "volStatus": p["volStatus"],
                "volConfirmed": p["volConfirmed"],
                "spyBias": spy_bias,
                "atr": round(atr, 2) if atr else None,
                "dataSource": "Tradier RT"
            })

    results.sort(key=lambda x: x["conf"], reverse=True)
    return jsonify({"signals": results, "count": len(results), "spyBias": spy_bias})

@app.route("/scan/swing")
def scan_swing():
    """Return swing scan - tells app to use its own Alpaca daily data"""
    return jsonify({"message": "Use app swing scanner with Alpaca daily bars"})

@app.route("/quote/<symbol>")
def quote(symbol):
    q = get_quote(symbol.upper())
    if q:
        return jsonify(q)
    return jsonify({"error": "Not found"}), 404

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
