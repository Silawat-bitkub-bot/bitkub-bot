"""
Bitkub Adaptive Bot V2
- Multi-coin (BTC, ETH, KUB)
- RSI Alert เมื่อใกล้จุดซื้อ
- Weekly Backtest อัตโนมัติทุกวันจันทร์
- Market Regime Detection (Daily)
- Adaptive Strategy (RSI/MA)
- Trailing Stop + Hard Stop
- Telegram Commands
"""
import os, time, hmac, hashlib, requests, json, threading, math
import http.server, socketserver
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_KEY          = os.getenv("BITKUB_API_KEY")
API_SECRET       = os.getenv("BITKUB_API_SECRET")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL         = "https://api.bitkub.com"

# ========== ตั้งค่าหลัก ==========
COINS = [
    {"symbol": "THB_BTC", "name": "BTC", "min_order": 50, "reserve": 0.000579},
    {"symbol": "THB_ETH", "name": "ETH", "min_order": 50, "reserve": 0.098222},
]
POSITION_PCT      = 0.10        # 10% ต่อเหรียญ (BTC+ETH = 20% รวม)
STOP_LOSS_PCT     = 0.05
TRAILING_STOP_PCT = 0.03
MAX_RETRIES       = 3

# ความถี่
SCAN_INTERVAL     = 300         # ตรวจทุก 5 นาที
REGIME_INTERVAL   = 14400       # วิเคราะห์ตลาดทุก 4 ชั่วโมง
TRADE_COOLDOWN    = 14400       # cooldown 4 ชั่วโมงต่อเหรียญ

# RSI
RSI_PERIOD        = 14
RSI_OVERSOLD      = 30
RSI_OVERBOUGHT    = 70
RSI_ALERT         = 35          # แจ้งเตือนก่อนถึงจุดซื้อ
RSI_RESOLUTION    = "60"

# MA
MA_FAST           = 12
MA_SLOW           = 26
MA_RESOLUTION     = "240"

# Regime
ADX_PERIOD        = 14
ADX_THRESHOLD     = 25
BB_SQUEEZE_PCT    = 0.05
WEEKLY_RANGE_PCT  = 0.08
REGIME_SCORE_MIN  = 2
# =================================

# ========== Global State ==========
positions = {c["name"]: {
    "entry_price": None,
    "peak_price":  None,
    "last_trade":  0,
    "prev_signal": None,
    "alerted":     False,   # ป้องกันแจ้งซ้ำ
} for c in COINS}

bot_running       = True
is_paused         = False
pause_until       = None
trade_count       = 0
win_count         = 0
total_pnl         = 0.0
start_time        = datetime.now()
last_update_id    = 0
current_regime    = "UNKNOWN"
current_strategy  = "NONE"
last_regime_check = 0
regime_details    = {}
last_backtest_day = -1          # วันอาทิตย์ที่รัน backtest ล่าสุด
# ==================================


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ==================== Telegram ====================

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for i in range(MAX_RETRIES):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
            return
        except:
            if i < MAX_RETRIES - 1: time.sleep(2)


def get_updates():
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 5}, timeout=10
        ).json()
        if r.get("ok"): return r.get("result", [])
    except: pass
    return []


def process_commands():
    global is_paused, pause_until, bot_running, last_update_id
    while bot_running:
        try:
            for upd in get_updates():
                last_update_id = upd["update_id"]
                msg     = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip().lower()
                if chat_id != TELEGRAM_CHAT_ID: continue
                cmds = {
                    "/status":    handle_status,
                    "/balance":   handle_balance,
                    "/stop":      handle_stop,
                    "/start":     handle_start,
                    "/pnl":       handle_pnl,
                    "/regime":    handle_regime,
                    "/strategy":  handle_strategy,
                    "/positions": handle_positions,
                    "/backtest":  lambda: threading.Thread(target=run_weekly_backtest, args=(True,), daemon=True).start(),
                    "/help":      handle_help,
                }
                if text in cmds: cmds[text]()
                elif text.startswith("/pause"): handle_pause(text)
        except Exception as e: log(f"⚠️ Cmd error: {e}")
        time.sleep(3)


# ==================== Handlers ====================

def handle_status():
    status     = "⏸️ หยุดชั่วคราว" if is_paused else "🟢 กำลังทำงาน"
    regime_icon = {"SIDEWAY": "📊", "TRENDING": "📈", "VOLATILE": "⚡"}.get(current_regime, "❓")
    strat_icon  = {"RSI": "RSI14", "MA": "MA12/26", "NONE": "รอ"}.get(current_strategy, "—")

    pos_lines = []
    for coin in [c["name"] for c in COINS]:
        p = positions[coin]
        if p["entry_price"]:
            try:
                _, _, closes = get_candles(f"THB_{coin}", RSI_RESOLUTION, 5)
                cur  = closes[-1] if closes else 0
                pct  = (cur - p["entry_price"]) / p["entry_price"] * 100
                pos_lines.append(f"  {coin}: {p['entry_price']:,.0f} → {cur:,.0f} ({pct:+.2f}%)")
            except:
                pos_lines.append(f"  {coin}: {p['entry_price']:,.0f}")

    pos_text  = "\n".join(pos_lines) if pos_lines else "  ไม่มี"
    wr        = win_count / trade_count * 100 if trade_count > 0 else 0
    uptime    = str(datetime.now() - start_time).split(".")[0]

    send_telegram(
        f"📊 <b>สถานะ Bot</b>\n{'─'*25}\n"
        f"สถานะ: {status}\n"
        f"ตลาด: {regime_icon} {current_regime} | กลยุทธ์: {strat_icon}\n"
        f"Positions:\n{pos_text}\n"
        f"Trade: {trade_count} | Win: {wr:.0f}% | PnL: {total_pnl:+.2f} บาท\n"
        f"รันมาแล้ว: {uptime}"
    )


def handle_balance():
    b = get_balances()
    if not b: send_telegram("❌ ดึงยอดเงินไม่ได้"); return
    def v(k):
        x = b.get(k, {}); return float(x.get("available", 0)) if isinstance(x, dict) else float(x)
    lines = [f"💵 THB: {v('THB'):>12,.2f} บาท"]
    for c in COINS:
        lines.append(f"   {c['name']}: {v(c['name']):>16.6f}")
    send_telegram(f"💰 <b>ยอดเงินในกระเป๋า</b>\n{'─'*25}\n" + "\n".join(lines))


def handle_stop():
    global is_paused
    is_paused = True
    send_telegram("⏸️ <b>Bot หยุดเทรดแล้ว</b>\nพิมพ์ /start เพื่อเริ่มใหม่")


def handle_start():
    global is_paused, pause_until
    is_paused = False; pause_until = None
    send_telegram("▶️ <b>Bot เริ่มเทรดใหม่แล้ว!</b>")


def handle_pause(text):
    global is_paused, pause_until
    try:
        parts = text.split(); val = parts[1] if len(parts) >= 2 else "1h"
        secs  = int(val[:-1]) * (3600 if val.endswith("h") else 60)
        label = f"{val[:-1]} {'ชั่วโมง' if val.endswith('h') else 'นาที'}"
        is_paused = True; pause_until = time.time() + secs
        send_telegram(f"⏸️ <b>Bot หยุดพัก {label}</b>")
    except: send_telegram("❌ ใช้: /pause 4h หรือ /pause 30m")


def handle_pnl():
    if trade_count == 0: send_telegram("📈 <b>ยังไม่มี Trade</b>"); return
    wr = win_count / trade_count * 100
    send_telegram(
        f"📈 <b>สรุปกำไร/ขาดทุน</b>\n{'─'*25}\n"
        f"Trade: {trade_count} ครั้ง | Win Rate: {wr:.1f}%\n"
        f"กำไร/ขาดทุนรวม: {total_pnl:+.2f} บาท\n"
        f"เฉลี่ยต่อ trade: {total_pnl/trade_count:+.2f} บาท"
    )


def handle_regime():
    d = regime_details
    send_telegram(
        f"🔍 <b>สภาพตลาด: {current_regime}</b>\n{'─'*25}\n"
        f"ADX: {d.get('adx','—')} {'✅' if d.get('adx_ok') else '❌'}\n"
        f"BB Width: {d.get('bb','—')} {'✅' if d.get('bb_ok') else '❌'}\n"
        f"Weekly Range: {d.get('wr','—')} {'✅' if d.get('wr_ok') else '❌'}\n"
        f"Score: {d.get('score',0)}/3\n"
        f"กลยุทธ์ที่ใช้: <b>{current_strategy}</b>"
    )


def handle_strategy():
    desc = {
        "RSI":  "RSI14 (1H) — ซื้อ RSI &lt; 30, ขาย RSI &gt; 70\nเหมาะกับ: Sideway",
        "MA":   "MA12/26 (4H) — ซื้อตาม Trend\nเหมาะกับ: Trending",
        "NONE": "รอ — ตลาด Volatile หรือไม่ชัดเจน"
    }
    send_telegram(f"🧠 <b>กลยุทธ์ปัจจุบัน</b>\n{'─'*25}\n{desc.get(current_strategy,'—')}")


def handle_positions():
    lines = []
    for c in COINS:
        p = positions[c["name"]]
        if p["entry_price"]:
            try:
                _, _, closes = get_candles(f"THB_{c['name']}", RSI_RESOLUTION, 5)
                cur  = closes[-1] if closes else 0
                pct  = (cur - p["entry_price"]) / p["entry_price"] * 100
                trail_pct = (cur - p["peak_price"]) / p["peak_price"] * 100 if p["peak_price"] else 0
                lines.append(
                    f"₿ <b>{c['name']}</b>\n"
                    f"  ซื้อที่: {p['entry_price']:,.0f} | ปัจจุบัน: {cur:,.0f}\n"
                    f"  P&L: {pct:+.2f}% | Trail: {trail_pct:.2f}%"
                )
            except:
                lines.append(f"₿ <b>{c['name']}</b>: {p['entry_price']:,.0f} บาท")
    if not lines:
        send_telegram("📋 <b>Positions</b>\nไม่มี position ที่เปิดอยู่")
    else:
        send_telegram(f"📋 <b>Positions ที่เปิดอยู่</b>\n{'─'*25}\n" + "\n\n".join(lines))


def handle_help():
    send_telegram(
        "📋 <b>คำสั่งที่ใช้ได้</b>\n"
        "─────────────────────\n"
        "/status     — สถานะ Bot\n"
        "/balance    — ยอดเงิน\n"
        "/positions  — Positions ที่เปิดอยู่\n"
        "/pnl        — กำไร/ขาดทุน\n"
        "/regime     — สภาพตลาด\n"
        "/strategy   — กลยุทธ์ที่ใช้\n"
        "/backtest   — รัน Backtest ตอนนี้เลย\n"
        "/stop       — หยุดเทรด\n"
        "/start      — เริ่มเทรด\n"
        "/pause 4h   — หยุดพัก\n"
        "/help       — คำสั่งทั้งหมด"
    )


# ==================== API ====================

def api_request(method, path, body=None):
    body_str  = json.dumps(body, separators=(',', ':')) if body else ""
    ts        = str(int(time.time() * 1000))
    sig       = hmac.new(API_SECRET.encode(), (ts+method+path+body_str).encode(), hashlib.sha256).hexdigest()
    headers   = {"Content-Type": "application/json", "X-BTK-APIKEY": API_KEY, "X-BTK-TIMESTAMP": ts, "X-BTK-SIGN": sig}
    for i in range(MAX_RETRIES):
        try:
            fn  = requests.post if method == "POST" else requests.get
            res = fn(f"{BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
            return res.json()
        except requests.exceptions.ConnectionError:
            if i < MAX_RETRIES - 1: time.sleep(5*(i+1))
        except:
            if i < MAX_RETRIES - 1: time.sleep(3)
    send_telegram("🔴 <b>API ไม่ตอบสนอง!</b>")
    return {}


def get_candles(symbol, resolution="60", limit=60):
    try:
        parts = symbol.lower().split("_")
        sym   = f"{parts[1]}_{parts[0]}"
        end   = int(time.time())
        mult  = {"1":60,"5":300,"60":3600,"240":14400,"1D":86400}.get(resolution, 3600)
        r     = requests.get(f"{BASE_URL}/tradingview/history", params={
            "symbol": sym.upper(), "resolution": resolution,
            "from": end - limit*mult, "to": end
        }, timeout=10).json()
        if r.get("s") == "ok":
            return r.get("h",[]), r.get("l",[]), r.get("c",[])
        return [], [], []
    except: return [], [], []


def get_balances():
    d = api_request("POST", "/api/v3/market/balances")
    return d.get("result", {}) if d.get("error") == 0 else {}


def place_order(side, symbol, amount):
    coin = symbol.split("_")[1].lower()
    path = "/api/v3/market/place-bid" if side == "buy" else "/api/v3/market/place-ask"
    return api_request("POST", path, {"sym": f"{coin}_thb", "amt": amount, "rat": 0, "typ": "market"})


def get_coin_balance(coin_name, reserve=0.0):
    b = get_balances(); v = b.get(coin_name, {})
    total = float(v.get("available", 0)) if isinstance(v, dict) else float(v)
    return max(total - reserve, 0)


def get_thb_balance():
    b = get_balances(); v = b.get("THB", {})
    return float(v.get("available", 0)) if isinstance(v, dict) else float(v)


# ==================== Indicators ====================

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(-period, 0)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(-period, 0)]
    ag, al = sum(gains)/period, sum(losses)/period
    return round(100-(100/(1+ag/al)), 2) if al != 0 else 100


def calc_ma(closes, period):
    return sum(closes[-period:])/period if len(closes) >= period else None


def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period*2: return None
    tr_l, pdm_l, ndm_l = [], [], []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr_l.append(max(h-l, abs(h-pc), abs(l-pc)))
        pdm_l.append(max(h-highs[i-1], 0) if (h-highs[i-1]) > (lows[i-1]-l) else 0)
        ndm_l.append(max(lows[i-1]-l, 0) if (lows[i-1]-l) > (h-highs[i-1]) else 0)
    def smooth(data, p):
        s = sum(data[:p]); res = [s]
        for v in data[p:]: s = s-s/p+v; res.append(s)
        return res
    atr, pdm_s, ndm_s = smooth(tr_l, period), smooth(pdm_l, period), smooth(ndm_l, period)
    dx_l = []
    for i in range(len(atr)):
        pdi = 100*pdm_s[i]/atr[i] if atr[i] else 0
        ndi = 100*ndm_s[i]/atr[i] if atr[i] else 0
        dx_l.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi) else 0)
    return round(sum(dx_l[-period:])/period, 2) if len(dx_l) >= period else None


def calc_bb_width(closes, period=20):
    if len(closes) < period: return None
    sl   = closes[-period:]; mean = sum(sl)/period
    std  = math.sqrt(sum((x-mean)**2 for x in sl)/period)
    return round((mean+2*std-(mean-2*std))/mean, 4)


def calc_volatility(closes, period=24):
    if len(closes) < period: return None
    sl = closes[-period:]; mean = sum(sl)/period
    return math.sqrt(sum((x-mean)**2 for x in sl)/period) / mean


# ==================== Market Regime ====================

def detect_regime():
    global current_regime, current_strategy, last_regime_check, regime_details
    log("🔍 วิเคราะห์สภาพตลาด (Daily BTC)...")

    highs_1d, lows_1d, closes_1d = get_candles("THB_BTC", "1D", ADX_PERIOD*3+10)
    _, _, closes_1h               = get_candles("THB_BTC", "60", 200)

    if len(closes_1d) < ADX_PERIOD*2 or len(closes_1h) < 20:
        return current_regime

    score = 0; details = {}

    adx    = calc_adx(highs_1d, lows_1d, closes_1d, ADX_PERIOD)
    adx_ok = adx is not None and adx < ADX_THRESHOLD
    if adx_ok: score += 1
    details["adx"] = f"{adx:.1f}" if adx else "—"; details["adx_ok"] = adx_ok

    bb    = calc_bb_width(closes_1h, 20)
    bb_ok = bb is not None and bb < BB_SQUEEZE_PCT
    if bb_ok: score += 1
    details["bb"] = f"{bb*100:.2f}%" if bb else "—"; details["bb_ok"] = bb_ok

    wr    = (max(closes_1h[-168:])-min(closes_1h[-168:]))/min(closes_1h[-168:]) if len(closes_1h) >= 168 else None
    wr_ok = wr is not None and wr < WEEKLY_RANGE_PCT
    if wr_ok: score += 1
    details["wr"] = f"{wr*100:.2f}%" if wr else "—"; details["wr_ok"] = wr_ok
    details["score"] = score

    vol        = calc_volatility(closes_1h, 24)
    prev_r     = current_regime
    prev_s     = current_strategy

    if vol and vol > 0.04:
        new_r, new_s = "VOLATILE", "NONE"
    elif score >= REGIME_SCORE_MIN:
        new_r, new_s = "SIDEWAY", "RSI"
    else:
        new_r, new_s = "TRENDING", "MA"

    current_regime   = new_r
    current_strategy = new_s
    last_regime_check = time.time()
    regime_details   = details

    log(f"🔍 Regime: {new_r} | Strategy: {new_s} | Score: {score}/3 | ADX: {details['adx']}")

    if new_r != prev_r or new_s != prev_s:
        icons  = {"SIDEWAY":"📊","TRENDING":"📈","VOLATILE":"⚡"}
        sdesc  = {"RSI":"RSI14 (1H)","MA":"MA12/26 (4H)","NONE":"หยุดรอ"}
        send_telegram(
            f"{icons.get(new_r,'❓')} <b>ตลาดเปลี่ยนเป็น {new_r}!</b>\n"
            f"ADX: {details['adx']} {'✅' if adx_ok else '❌'} | "
            f"BB: {details['bb']} {'✅' if bb_ok else '❌'} | "
            f"WR: {details['wr']} {'✅' if wr_ok else '❌'}\n"
            f"🧠 กลยุทธ์ใหม่: <b>{sdesc.get(new_s,'—')}</b>"
        )
    return new_r


# ==================== Strategies ====================

def strategy_rsi(closes):
    rsi = calc_rsi(closes, RSI_PERIOD)
    if rsi is None: return None, None, rsi
    if rsi < RSI_OVERSOLD:  return "buy",  f"RSI {rsi:.1f} Oversold",   rsi
    if rsi > RSI_OVERBOUGHT: return "sell", f"RSI {rsi:.1f} Overbought", rsi
    return "hold", f"RSI {rsi:.1f}", rsi


def strategy_ma(closes_4h):
    maf = calc_ma(closes_4h, MA_FAST)
    mas = calc_ma(closes_4h, MA_SLOW)
    if not maf or not mas: return None, None
    diff = (maf-mas)/mas*100
    if diff > 0.3:  return "buy",  f"MA{MA_FAST}>{MA_SLOW} (+{diff:.2f}%)"
    if diff < -0.3: return "sell", f"MA{MA_FAST}<{MA_SLOW} ({diff:.2f}%)"
    return "hold", f"MA spread {diff:.2f}%"


# ==================== Weekly Backtest ====================

def run_weekly_backtest(manual=False):
    log("📊 เริ่ม Weekly Backtest...")
    send_telegram("📊 <b>Weekly Backtest เริ่มแล้ว...</b>\nรอสักครู่นะครับ")

    results = []
    for c in COINS:
        try:
            _, _, closes = get_candles(c["symbol"], RSI_RESOLUTION, RSI_PERIOD+50)
            if len(closes) < RSI_PERIOD+10:
                results.append(f"❌ {c['name']}: ข้อมูลไม่พอ")
                continue

            thb = 10000; coin_bal = 0; entry = None
            prev_sig = None; trades = []; wins = 0

            for i in range(RSI_PERIOD+5, len(closes)):
                price = closes[i]
                rsi   = calc_rsi(closes[:i+1], RSI_PERIOD)
                if rsi is None: continue

                if entry and coin_bal > 0:
                    if (price-entry)/entry <= -STOP_LOSS_PCT:
                        pnl = (price-entry)/entry*(coin_bal*entry)
                        thb += coin_bal*price*0.9975; coin_bal = 0; entry = None
                        trades.append(pnl); prev_sig = "sell"; continue
                    if (price-entry)/entry >= 0.05:
                        pnl = (price-entry)/entry*(coin_bal*entry)
                        thb += coin_bal*price*0.9975; coin_bal = 0; entry = None
                        trades.append(pnl)
                        if pnl > 0: wins += 1
                        prev_sig = "sell"; continue

                sig = "buy" if rsi < RSI_OVERSOLD else "sell" if rsi > RSI_OVERBOUGHT else "hold"
                if sig == "buy" and prev_sig != "buy":
                    amt = thb*0.1
                    if amt >= 50:
                        coin_bal = amt*0.9975/price; thb -= amt; entry = price
                        trades.append(None)
                    prev_sig = "buy"
                elif sig == "sell" and prev_sig != "sell" and coin_bal > 0:
                    pnl = (price-entry)/entry*(coin_bal*entry) if entry else 0
                    thb += coin_bal*price*0.9975; coin_bal = 0; entry = None
                    trades.append(pnl)
                    if pnl > 0: wins += 1
                    prev_sig = "sell"

            if coin_bal > 0: thb += coin_bal*closes[-1]*0.9975
            sell_trades = [t for t in trades if t is not None]
            wr  = wins/len(sell_trades)*100 if sell_trades else 0
            ret = (thb-10000)/10000*100
            bnh = (closes[-1]-closes[RSI_PERIOD+5])/closes[RSI_PERIOD+5]*100
            results.append(
                f"{'📈' if ret>=0 else '📉'} <b>{c['name']}</b>: {ret:+.2f}% "
                f"(B&H: {bnh:+.2f}%) | WR: {wr:.0f}% | {len(sell_trades)} trades"
            )
        except Exception as e:
            results.append(f"❌ {c['name']}: Error — {e}")

    label = "Manual" if manual else "Weekly Auto"
    send_telegram(
        f"📊 <b>Backtest Report ({label})</b>\n"
        f"{'─'*25}\n"
        f"ช่วงเวลา: {RSI_PERIOD+55} แท่ง ({RSI_RESOLUTION}H)\n\n"
        + "\n".join(results)
    )
    log("📊 Backtest เสร็จแล้ว")


def check_weekly_backtest():
    global last_backtest_day
    now = datetime.now()
    if now.weekday() == 0 and now.day != last_backtest_day:
        last_backtest_day = now.day
        threading.Thread(target=run_weekly_backtest, daemon=True).start()


# ==================== Trade Execution ====================

def do_sell(symbol, coin_name, coin_bal, price, reason):
    global trade_count, win_count, total_pnl
    p      = positions[coin_name]
    result = place_order("sell", symbol, coin_bal)
    if result.get("error") == 0:
        pnl        = (price-p["entry_price"])/p["entry_price"]*(coin_bal*p["entry_price"]) if p["entry_price"] else 0
        total_pnl += pnl; trade_count += 1
        if pnl > 0: win_count += 1
        log(f"✅ {coin_name} {reason} | {pnl:+.2f} บาท")
        send_telegram(
            f"{'🚨' if 'Stop' in reason else '🔴'} <b>{reason} — {coin_name}</b>\n"
            f"ซื้อที่: {p['entry_price']:,.0f} | ขายที่: {price:,.0f}\n"
            f"{'📈' if pnl>=0 else '📉'} {pnl:+.2f} บาท | รวม: {total_pnl:+.2f} บาท"
        )
        p["entry_price"] = p["peak_price"] = None
        p["last_trade"]  = time.time()
        return True
    send_telegram(f"❌ <b>ขาย {coin_name} ไม่สำเร็จ</b>\n{result}")
    return False


def process_coin(c):
    reserve = c.get("reserve", 0.0)
    global trade_count
    symbol    = c["symbol"]
    coin_name = c["name"]
    p         = positions[coin_name]

    _, _, closes_1h = get_candles(symbol, RSI_RESOLUTION, RSI_PERIOD+15)
    _, _, closes_4h = get_candles(symbol, MA_RESOLUTION,  MA_SLOW+10)
    if not closes_1h: return

    price = closes_1h[-1]

    # อัปเดต peak
    if p["entry_price"] and price > (p["peak_price"] or 0):
        p["peak_price"] = price

    # Hard Stop
    if p["entry_price"] and (price-p["entry_price"])/p["entry_price"] <= -STOP_LOSS_PCT:
        cb = get_coin_balance(coin_name, reserve)
        if cb > 0: do_sell(symbol, coin_name, cb, price, "Hard Stop Loss")
        p["prev_signal"] = "sell"; return

    # Trailing Stop
    if p["entry_price"] and p["peak_price"] and (price-p["peak_price"])/p["peak_price"] <= -TRAILING_STOP_PCT:
        cb  = get_coin_balance(coin_name, reserve)
        pct = (price-p["peak_price"])/p["peak_price"]*100
        if cb > 0: do_sell(symbol, coin_name, cb, price, f"Trailing Stop {abs(pct):.1f}%")
        p["prev_signal"] = "sell"; return

    if current_strategy == "NONE": return

    # Cooldown
    if not p["entry_price"] and p["last_trade"] and (time.time()-p["last_trade"]) < TRADE_COOLDOWN:
        return

    # เลือก strategy
    if current_strategy == "RSI":
        signal, reason, rsi = strategy_rsi(closes_1h)

        # RSI Alert — แจ้งเตือนก่อนถึงจุดซื้อ
        if rsi and RSI_OVERSOLD < rsi <= RSI_ALERT and not p["entry_price"]:
            if not p["alerted"]:
                p["alerted"] = True
                send_telegram(
                    f"⚠️ <b>RSI Alert — {coin_name}</b>\n"
                    f"RSI: {rsi:.1f} ใกล้ Oversold แล้ว!\n"
                    f"Bot จะซื้ออัตโนมัติเมื่อ RSI &lt; {RSI_OVERSOLD}"
                )
        elif rsi and rsi > RSI_ALERT:
            p["alerted"] = False

    elif current_strategy == "MA":
        result = strategy_ma(closes_4h)
        signal, reason = result if result else ("hold", "—")
        rsi = None
    else:
        return

    if signal is None: return

    pct_text = f" | {(price-p['entry_price'])/p['entry_price']*100:+.2f}%" if p["entry_price"] else ""
    log(f"  {coin_name}: {price:,.0f} | {reason}{pct_text}")

    if signal == "buy" and p["prev_signal"] != "buy":
        thb    = get_thb_balance()
        amount = round(thb * POSITION_PCT, 2)
        if amount < c["min_order"]:
            send_telegram(f"⚠️ <b>{coin_name}</b> ยอดเงินไม่พอ ({thb:,.0f} บาท)")
        else:
            r = place_order("buy", symbol, amount)
            if r.get("error") == 0:
                p["entry_price"] = price
                p["peak_price"]  = price
                p["last_trade"]  = time.time()
                trade_count     += 1
                log(f"✅ BUY {coin_name} {amount:,.2f} บาท")
                send_telegram(
                    f"🟢 <b>BUY {coin_name}</b>\n"
                    f"กลยุทธ์: {current_strategy} ({reason})\n"
                    f"ตลาด: {current_regime}\n"
                    f"ราคา: {price:,.0f} | ใช้: {amount:,.2f} บาท\n"
                    f"🛑 Hard Stop: {price*(1-STOP_LOSS_PCT):,.0f}\n"
                    f"📉 Trailing: {TRAILING_STOP_PCT*100:.0f}% จาก peak"
                )
            else:
                send_telegram(f"❌ <b>ซื้อ {coin_name} ไม่สำเร็จ</b>\n{r}")
        p["prev_signal"] = "buy"

    elif signal == "sell" and p["prev_signal"] != "sell":
        cb = get_coin_balance(coin_name, reserve)
        if cb > 0: do_sell(symbol, coin_name, cb, price, f"{current_strategy} Sell")
        else: log(f"  {coin_name}: ไม่มีเหรียญในกระเป๋า")
        p["prev_signal"] = "sell"


# ==================== Main Loop ====================

def run_bot():
    global is_paused, pause_until

    log("🤖 Bitkub Adaptive Bot V2 เริ่มทำงาน!")
    log(f"🪙 เหรียญ: {', '.join(c['name'] for c in COINS)}")
    log(f"⏱️  Scan: {SCAN_INTERVAL//60}m | Regime: {REGIME_INTERVAL//3600}h | Cooldown: {TRADE_COOLDOWN//3600}h")
    log("=" * 60)

    send_telegram(
        "🤖 <b>Bitkub Adaptive Bot V2 เริ่มทำงาน!</b>\n"
        f"{'─'*25}\n"
        f"🪙 เหรียญ: {', '.join(c['name'] for c in COINS)}\n"
        f"🧠 Adaptive Strategy (RSI / MA)\n"
        f"⚠️ RSI Alert ที่ &lt; {RSI_ALERT}\n"
        f"📊 Weekly Backtest ทุกวันจันทร์\n"
        f"⏱️ Scan ทุก {SCAN_INTERVAL//60} นาที\n"
        f"🛑 Hard Stop: {STOP_LOSS_PCT*100:.0f}% | Trailing: {TRAILING_STOP_PCT*100:.0f}%\n\n"
        f"พิมพ์ /help เพื่อดูคำสั่งทั้งหมด"
    )

    threading.Thread(target=process_commands, daemon=True).start()
    detect_regime()

    while True:
        try:
            if is_paused:
                if pause_until and time.time() >= pause_until:
                    is_paused = False; pause_until = None
                    send_telegram("▶️ <b>Bot เริ่มเทรดใหม่อัตโนมัติ</b>")
                else:
                    time.sleep(SCAN_INTERVAL); continue

            if time.time() - last_regime_check > REGIME_INTERVAL:
                detect_regime()

            check_weekly_backtest()

            log(f"── Scan | Regime: {current_regime} | Strategy: {current_strategy} ──")
            for c in COINS:
                try:
                    process_coin(c)
                except Exception as e:
                    log(f"❌ {c['name']} error: {e}")
                time.sleep(1)

        except KeyboardInterrupt:
            log("🛑 หยุด bot")
            send_telegram(f"🛑 <b>Bot หยุดทำงาน</b>\nTrade: {trade_count} | PnL: {total_pnl:+.2f} บาท")
            break
        except Exception as e:
            log(f"❌ Error: {e}")
            send_telegram(f"❌ <b>Bot Error!</b>\n{e}")

        time.sleep(SCAN_INTERVAL)


# ==================== Web Dashboard Server ====================

DASHBOARD_HTML = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read() if os.path.exists("dashboard.html") else "<h1>Dashboard not found</h1>"

def get_bot_state():
    """คืนค่า state ปัจจุบันของ Bot เป็น JSON"""
    pos = {}
    for c in COINS:
        p = positions[c["name"]]
        pos[c["name"]] = {
            "entry_price": p["entry_price"],
            "peak_price":  p["peak_price"],
        }
    return json.dumps({
        "regime":    current_regime,
        "strategy":  current_strategy,
        "is_paused": is_paused,
        "trade_count": trade_count,
        "win_count":   win_count,
        "total_pnl":   round(total_pnl, 2),
        "uptime":      str(datetime.now() - start_time).split(".")[0],
        "positions":   pos,
        "regime_details": regime_details,
    })


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass  # ปิด access log

    def do_GET(self):
        if self.path == "/api/state":
            data = get_bot_state().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())


def start_dashboard():
    port = int(os.getenv("PORT", 8080))
    with socketserver.TCPServer(("", port), DashboardHandler) as httpd:
        log(f"🌐 Dashboard: http://localhost:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("❌ ไม่พบ API Key!"); exit(1)
    threading.Thread(target=start_dashboard, daemon=True).start()
    run_bot()
