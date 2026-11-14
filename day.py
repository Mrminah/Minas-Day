import os, json, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

RR_MIN = 2.0
BUFFER = 0.5
MIN_RISK = 4.0
MAX_RISK = 15.0
MIN_RANGE = 8.0
PEND_MAX = 30
MAX_HOLD = 180
MAX_EXT = 1
MAX_PER_DAY = 3
USE_TREND = True
STATE_FILE = "day_trades.json"
TAG = "Minas Day"

def send(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": text[:4000]}, timeout=20)
        print("TELEGRAM:", r.status_code)
    except Exception as ex:
        print("Telegram error:", ex)

def parse(s):
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("sent", [])
    s.setdefault("trades", [])
    s.setdefault("stats", {"w": 0, "l": 0, "be": 0, "r": 0.0})
    s.setdefault("day", {"d": "", "n": 0})
    s.setdefault("watching", None)
    return s

def save_state(s):
    s["sent"] = s["sent"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)

CACHE, ERRORS = {}, []
def candles(interval, size):
    if interval in CACHE:
        return CACHE[interval]
    rows = None
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "XAU/USD", "interval": interval,
                    "outputsize": size, "apikey": KEY}, timeout=30)
        d = r.json()
        if "values" in d:
            rows = [{"t": v["datetime"], "dt": parse(v["datetime"]),
                     "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])}
                    for v in reversed(d["values"])][:-1]  # drop forming candle
        else:
            ERRORS.append(f"{interval}: {d.get('message', 'unknown')}")
    except Exception as ex:
        ERRORS.append(f"{interval}: {ex}")
    CACHE[interval] = rows
    return rows

def swings(rows, n):
    highs, lows = [], []
    for i in range(n, len(rows) - n):
        w = rows[i - n:i + n + 1]
        if rows[i]["h"] == max(x["h"] for x in w):
            highs.append(rows[i]["h"])
        if rows[i]["l"] == min(x["l"] for x in w):
            lows.append(rows[i]["l"])
    return highs, lows

def bias(rows):
    highs, lows = swings(rows, 3)
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BUY"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "SELL"
    return None

def displaced(rows, m, side):
    if m < 21:
        return False
    c = rows[m]
    bodies = [abs(x["c"] - x["o"]) for x in rows[m - 20:m]]
    avg = sum(bodies) / len(bodies)
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    if rng <= 0 or body < 1.3 * avg or body < 0.5 * rng:
        return False
    return c["c"] < c["o"] if side == "SELL" else c["c"] > c["o"]

def sweep_info(m15, m1, trend):
    """Last closed 15m candle = range. The current 15m window sweeps one
    side and price is back inside the range."""
    if len(m15) < 3 or len(m1) < 60:
        return None
    A = m15[-1]
    crth, crtl = A["h"], A["l"]
    if crth - crtl < MIN_RANGE:
        return None
    b_start = A["dt"] + timedelta(minutes=15)
    b_end = b_start + timedelta(minutes=15)
    base = next((i for i, x in enumerate(m1) if x["dt"] >= b_start), None)
    if base is None:
        return None
    stop = next((i for i in range(base, len(m1))
                 if m1[i]["dt"] >= b_end), len(m1))
    seg = list(range(base, stop))
    if len(seg) < 3:
        return None
    swept_hi = any(m1[i]["h"] > crth for i in seg)
    swept_lo = any(m1[i]["l"] < crtl for i in seg)
    if swept_hi == swept_lo:
        return None
    side = "SELL" if swept_hi else "BUY"
    if trend and side != trend:
        return None
    last = m1[-1]
    if side == "SELL" and last["c"] >= crth:
        return None
    if side == "BUY" and last["c"] <= crtl:
        return None
    return {"side": side, "crth": crth, "crtl": crtl, "t": A["t"], "seg": seg}

def find_crt(m15, m1, trend):
    """Sweep confirmed by a 1m market shift -> limit entry."""
    w = sweep_info(m15, m1, trend)
    if not w:
        return None
    side, crth, crtl, seg = w["side"], w["crth"], w["crtl"], w["seg"]
    if side == "SELL":
        ext = max(seg, key=lambda i: m1[i]["h"])
        lvl = None
        for i in range(ext - 1, max(ext - 40, 3), -1):
            if m1[i]["l"] == min(x["l"] for x in m1[i - 2:i + 3]):
                lvl = m1[i]["l"]
                break
        if lvl is None:
            return None
        j = next((k for k in range(ext + 1, len(m1))
                  if m1[k]["c"] < lvl), None)
        entry = lvl
        sl = m1[ext]["h"] + BUFFER
        tp = crtl + 0.5
        risk, reward = sl - entry, entry - tp
        retest = j is not None and any(x["h"] >= lvl for x in m1[j + 1:])
    else:
        ext = min(seg, key=lambda i: m1[i]["l"])
        lvl = None
        for i in range(ext - 1, max(ext - 40, 3), -1):
            if m1[i]["h"] == max(x["h"] for x in m1[i - 2:i + 3]):
                lvl = m1[i]["h"]
                break
        if lvl is None:
            return None
        j = next((k for k in range(ext + 1, len(m1))
                  if m1[k]["c"] > lvl), None)
        entry = lvl
        sl = m1[ext]["l"] - BUFFER
        tp = crth - 0.5
        risk, reward = entry - sl, tp - entry
        retest = j is not None and any(x["l"] <= lvl for x in m1[j + 1:])
    if j is None or retest or j < len(m1) - 10:
        return None
    if not displaced(m1, j, side):
        return None
    if risk < MIN_RISK or risk > MAX_RISK or reward <= 0:
        return None
    rr = reward / risk
    if rr < RR_MIN:
        return None
    return {"side": side, "entry": entry, "sl": sl, "tp": tp, "risk": risk,
            "rr": rr, "crth": crth, "crtl": crtl, "t": w["t"]}

state = load_state()

def record_text():
    s = state["stats"]
    return f"Record: {s['w']}W {s['l']}L {s['be']}BE | total {s['r']:+.1f}R"

def finish(t, r, why):
    st = state["stats"]
    st["r"] += r
    if r > 0.01:
        st["w"] += 1
    elif r < -0.01:
        st["l"] += 1
    else:
        st["be"] += 1
    send(f"{TAG}: Gold {t['side']}: {why}\nResult: {r:+.1f}R\n{record_text()}")

def track(m1):
    keep = []
    for t in state["trades"]:
        side = t["side"]
        sign = 1 if side == "BUY" else -1
        e, risk = t["entry"], t["risk"]
        done = False
        for x in m1:
            if x["dt"] <= parse(t["checked"]):
                continue
            t["checked"] = x["t"]
            t["n"] += 1
            if side == "SELL":
                hit_e, hit_sl, hit_tp = (x["h"] >= e, x["h"] >= t["sl"],
                                         x["l"] <= t["tp"])
            else:
                hit_e, hit_sl, hit_tp = (x["l"] <= e, x["l"] <= t["sl"],
                                         x["h"] >= t["tp"])
            if t["status"] == "pending":
                if hit_e:
                    t["status"] = "live"
                    t["n"] = 0
                    send(f"{TAG}: Gold {side} order FILLED at {e:.2f}\n"
                         f"Trade is live. SL {t['sl']:.2f} | TP {t['tp']:.2f}")
                    if hit_sl:
                        finish(t, -1.0, "Stop loss hit right after the fill")
                        done = True
                        break
                    continue
                if hit_tp:
                    send(f"{TAG}: Gold {side}: price ran to TP without filling.\n"
                         f"CANCEL your limit order at {e:.2f}.")
                    done = True
                    break
                if t["n"] >= PEND_MAX:
                    send(f"{TAG}: Gold {side}: limit order expired.\n"
                         f"CANCEL your order at {e:.2f}.")
                    done = True
                    break
                continue
            if hit_sl:
                r = sign * (t["sl"] - e) / risk
                finish(t, r, "Stop loss hit" if r < -0.01
                       else "Stopped at breakeven")
                done = True
                break
            if hit_tp:
                finish(t, sign * (t["tp"] - e) / risk, "Take profit hit")
                done = True
                break
            if t["n"] >= MAX_HOLD:
                finish(t, sign * (x["c"] - e) / risk,
                       "Time stop: close the trade at market")
                done = True
                break
        if done:
            continue
        if t["status"] == "live" and t["ext"] < MAX_EXT and len(m1) > 20:
            last, prev = m1[-1], m1[-2]
            if last["t"] != t.get("ext_t"):
                highs, lows = swings(m1[-80:], 3)
                ok = False
                if side == "SELL" and lows:
                    ok = (prev["c"] >= lows[-1] > last["c"]
                          and last["c"] < last["o"] and last["c"] <= e - risk)
                if side == "BUY" and highs:
                    ok = (prev["c"] <= highs[-1] < last["c"]
                          and last["c"] > last["o"] and last["c"] >= e + risk)
                if ok:
                    old = t["tp"]
                    t["tp"] = old + sign * risk
                    t["sl"] = max(t["sl"], e) if side == "BUY" \
                        else min(t["sl"], e)
                    t["ext"] += 1
                    t["ext_t"] = last["t"]
                    send(f"{TAG}: Gold {side}: still strong\n"
                         f"New structure break in your direction.\n"
                         f"Move TP: {old:.2f} -> {t['tp']:.2f}\n"
                         f"Move SL to breakeven: {t['sl']:.2f}")
        keep.append(t)
    state["trades"] = keep

today = datetime.utcnow().strftime("%Y-%m-%d")
if state["day"]["d"] != today:
    state["day"] = {"d": today, "n": 0}

m1 = candles("1min", 200)
if m1 and state["trades"]:
    track(m1)
if state["trades"]:
    state["watching"] = None

status, trend = "no setup yet", None
if state["trades"]:
    status = f"{len(state['trades'])} active trade(s), not scanning"
elif state["day"]["n"] >= MAX_PER_DAY:
    status = "daily signal limit reached"
elif m1:
    m15 = candles("15min", 60)
    if USE_TREND:
        h1 = candles("1h", 100)
        trend = bias(h1) if h1 else None
    if m15 and (trend or not USE_TREND):
        s = find_crt(m15, m1, trend)
        w = sweep_info(m15, m1, trend)
        if s:
            key = f"{s['side']}:{s['t']}"
            if key not in state["sent"]:
                state["sent"].append(key)
                state["day"]["n"] += 1
                state["watching"] = None
                state["trades"].append({
                    "side": s["side"], "entry": s["entry"], "sl": s["sl"],
                    "tp": s["tp"], "risk": s["risk"], "status": "pending",
                    "time": s["t"], "checked": m1[-1]["t"], "n": 0, "ext": 0})
                status = "new signal sent"
                took = "high" if s["side"] == "SELL" else "low"
                send(f"{TAG}: Gold {s['side']} CONFIRMED\n"
                     f"Setup: CRT sweep + 1m market shift\n"
                     f"15m range: CRTH {s['crth']:.2f} | CRTL {s['crtl']:.2f}\n"
                     f"Price swept the {took} and came back inside.\n"
                     f"Place a {s['side']} LIMIT at {s['entry']:.2f}\n"
                     f"SL: {s['sl']:.2f}\n"
                     f"TP: {s['tp']:.2f} (other side of the range)\n"
                     f"RR: 1:{s['rr']:.1f}\n"
                     f"Valid about 30 minutes. If price reaches TP first, "
                     f"cancel it. Minas tells you when it fills.")
        elif w:
            wkey = f"W:{w['side']}:{w['t']}"
            if wkey not in state["sent"]:
                state["sent"].append(wkey)
                state["watching"] = {"side": w["side"], "t": w["t"]}
                status = "heads-up sent"
                took = "high (CRTH)" if w["side"] == "SELL" else "low (CRTL)"
                aim = w["crtl"] if w["side"] == "SELL" else w["crth"]
                send(f"{TAG}: {w['side']} POSITION COMING\n"
                     f"Price swept the 15m {took} and is back inside the "
                     f"range.\n"
                     f"Target area: {aim:.2f}\n"
                     f"Watching the 1m chart for a market shift.\n"
                     f"Do NOT enter yet. If it confirms, I send Entry, SL "
                     f"and TP. If not, I tell you to ignore it.")
        wt = state.get("watching")
        if wt and not s and (not w or w["t"] != wt["t"]):
            send(f"{TAG}: {wt['side']} heads-up cancelled.\n"
                 f"No confirmation. Ignore this setup.")
            state["watching"] = None
    elif USE_TREND and not trend:
        status = "no clear 1h trend"

if MANUAL:
    if ERRORS:
        send(f"{TAG} data error: " + "; ".join(ERRORS))
    elif status not in ("new signal sent", "heads-up sent"):
        send(f"{TAG} scan done.\n1h trend: {trend or 'none'}\n"
             f"Status: {status}\n{record_text()}")
print("Status:", status, "| Errors:", ERRORS)
save_state(state)
