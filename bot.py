"""
Bitkub Adaptive Bot — Multi-Strategy + Market Regime Detection
กลยุทธ์ปรับเองตามสภาพตลาดอัตโนมัติ
"""
import os, time, hmac, hashlib, requests, json, threading, math
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

API_KEY          = os.getenv("BITKUB_API_KEY")
API_SECRET       = os.getenv("BITKUB_API_SECRET")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL         = "https://api.bitkub.com"

# ========== ตั้งค่าหลัก ==========
SYMBOL            = "THB_BTC"
POSITION_PCT      = 0.10        # 10% ของพอร์ตต่อ trade
STOP_LOSS_PCT     = 0.05        # Hard Stop Loss 5%
TRAILING_STOP_PCT = 0.03        # Trailing Stop 3%
MIN_ORDER_THB     = 50
MAX_RETRIES       = 3

# ความถี่
SCAN_INTERVAL     = 300         # ตรวจสัญญาณทุก 5 นาที
REGIME_INTERVAL   = 14400       # วิเคราะห์ตลาดทุก 4 ชั่วโมง
TRADE_COOLDOWN    = 14400       # รอ 4 ชั่วโมงระหว่าง trade

# RSI Strategy
RSI_PERIOD        = 14
RSI_OVERSOLD      = 30
RSI_OVERBOUGHT    = 70
RSI_RESOLUTION    = "60"        # กราฟ 1 ชั่วโมง

# MA Strategy
MA_FAST           = 12
MA_SLOW           = 26
MA_RESOLUTION     = "240"       # กราฟ 4 ชั่วโมง

# Regime Detection (กราฟรายวัน)
ADX_PERIOD        = 14
ADX_THRESHOLD     = 25
BB_PERIOD         = 20
BB_SQUEEZE_PCT    = 0.05
WEEKLY_RANGE_PCT  = 0.08
REGIME_SCORE_MIN  = 2
# =================================

# ========== State ==========
entry_price       = None
peak_price        = None
is_paused         = False
pause_until       = None
bot_running       = True
trade_count       = 0
win_count         = 0
total_pnl         = 0.0
start_time        = datetime.now()
last_update_id    = 0
current_regime    = "UNKNOWN"
current_strategy  = "NONE"
last_regime_check = 0
last_trade_time   = 0
regime_details    = {}
# ===========================


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
                    "/status":   handle_status,
                    "/balance":  handle_balance,
                    "/stop":     handle_stop,
                    "/start":    handle_start,
                    "/pnl":      handle_pnl,
                    "/regime":   handle_regime,
                    "/strategy": handle_strategy,
                    "/help":     handle_help,
                }
                if text in cmds: cmds[text]()
                elif text.startswith("/pause"): handle_pause(text)
        except Exception as e: log(f"⚠️ Cmd error: {e}")
        time.sleep(3)


# ==================== Handlers ====================

def handle_status():
    status = "⏸️ หยุดชั่วคราว" if is_paused else "🟢 กำลังทำงาน"
    pause_text = ""
    if is_paused and pause_until:
        pause_text = f"\nเริ่มใหม่ใน: {max(0,int((pause_until-time.time())/60))} นาที"

    strat_icon = {"RSI": "📊 RSI (Sideway)", "MA": "📈 MA Crossover (Trending)", "NONE": "⛔ รอสัญญาณ"}.get(current_strategy, "❓")
    regime_icon = {"SIDEWAY": "📊 Sideway", "TRENDING": "📈 Trending", "VOLATILE": "⚡ Volatile"}.get(current_regime, "❓")

    pos = "ไม่มี"
    if entry_price:
        try:
            closes = get_candles(SYMBOL, "60", 5)
            cur    = closes[-1] if closes else 0
            pct    = (cur - entry_price) / entry_price * 100
            pos    = f"{SYMBOL.split('_')[1]} ที่ {entry_price:,.0f} ({pct:+.2f}%)"
        except: pos = f"{SYMBOL.split('_')[1]} ที่ {entry_price:,.0f}"

    cooldown_text = ""
    if last_trade_time:
        remaining = max(0, TRADE_COOLDOWN - (time.time() - last_trade_time))
        if remaining > 0:
            cooldown_text = f"\nCooldown: อีก {int(remaining/60)} นาที"

    wr = win_count / trade_count * 100 if trade_count > 0 else 0
    send_telegram(
        f"📊 <b>สถานะ Bot</b>\n{'─'*25}\n"
        f"สถานะ: {status}{pause_text}\n"
        f"ตลาด: {regime_icon}\n"
        f"กลยุทธ์: {strat_icon}\n"
        f"Position: {pos}\n"
        f"Trade: {trade_count} ครั้ง | Win: {wr:.0f}%\n"
        f"กำไร/ขาดทุน: {total_pnl:+.2f} บาท{cooldown_text}\n"
        f"รันมาแล้ว: {str(datetime.now()-start_time).split('.')[0]}"
    )


def handle_balance():
    b = get_balances()
    if not b: send_telegram("❌ ดึงยอดเงินไม่ได้"); return
    def v(k):
        x = b.get(k, {}); return float(x.get("available", 0)) if isinstance(x, dict) else float(x)
    send_telegram(
        f"💰 <b>ยอดเงินในกระเป๋า</b>\n{'─'*25}\n"
        f"💵 THB: {v('THB'):>12,.2f} บาท\n"
        f"₿  BTC: {v('BTC'):>16.8f}\n"
        f"Ξ  ETH: {v('ETH'):>16.8f}"
    )


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


def handle_regime():
    d = regime_details
    send_telegram(
        f"🔍 <b>สภาพตลาดปัจจุบัน: {current_regime}</b>\n{'─'*25}\n"
        f"ADX: {d.get('adx','—')} {'✅' if d.get('adx_ok') else '❌'}\n"
        f"BB Width: {d.get('bb','—')} {'✅' if d.get('bb_ok') else '❌'}\n"
        f"Weekly Range: {d.get('wr','—')} {'✅' if d.get('wr_ok') else '❌'}\n"
        f"Score: {d.get('score',0)}/3\n\n"
        f"กลยุทธ์ที่ใช้: <b>{current_strategy}</b>"
    )


def handle_strategy():
    strats = {
        "RSI":  "📊 RSI14 (1H) — ซื้อ Oversold, ขาย Overbought\nเหมาะกับ: ตลาด Sideway",
        "MA":   "📈 MA12/26 (4H) — ซื้อตอน MA ตัดขึ้น\nเหมาะกับ: ตลาด Trending",
        "NONE": "⛔ รอสัญญาณ — ตลาด Volatile หรือไม่ชัดเจน"
    }
    send_telegram(f"🧠 <b>กลยุทธ์ปัจจุบัน</b>\n{'─'*25}\n{strats.get(current_strategy, '—')}")


def handle_pnl():
    if trade_count == 0: send_telegram("📈 <b>ยังไม่มี Trade</b>"); return
    wr = win_count / trade_count * 100
    send_telegram(
        f"📈 <b>สรุปกำไร/ขาดทุน</b>\n{'─'*25}\n"
        f"Trade ทั้งหมด: {trade_count} ครั้ง\n"
        f"Win Rate: {wr:.1f}%\n"
        f"กำไร/ขาดทุนรวม: {total_pnl:+.2f} บาท\n"
        f"เฉลี่ยต่อ trade: {total_pnl/trade_count:+.2f} บาท"
    )


def handle_help():
    send_telegram(
        "📋 <b>คำสั่งที่ใช้ได้</b>\n"
        "─────────────────────\n"
        "/status    — สถานะ Bot\n"
        "/balance   — ยอดเงิน\n"
        "/pnl       — กำไร/ขาดทุน\n"
        "/regime    — สภาพตลาด\n"
        "/strategy  — กลยุทธ์ที่ใช้อยู่\n"
        "/stop      — หยุดเทรด\n"
        "/start     — เริ่มเทรด\n"
        "/pause 4h  — หยุดพัก 4 ชั่วโมง\n"
        "/pause 30m — หยุดพัก 30 นาที\n"
        "/help      — คำสั่งทั้งหมด"
    )


# ==================== API ====================

def api_request(method, path, body=None):
    body_str  = json.dumps(body, separators=(',', ':')) if body else ""
    ts        = str(int(time.time() * 1000))
    sig       = hmac.new(API_SECRET.encode(), (ts+method.upper()+path+body_str).encode(), hashlib.sha256).hexdigest()
    headers   = {"Content-Type": "application/json", "X-BTK-APIKEY": API_KEY, "X-BTK-TIMESTAMP": ts, "X-BTK-SIGN": sig}
    for i in range(MAX_RETRIES):
        try:
            fn  = requests.post if method == "POST" else requests.get
            res = fn(f"{BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
            return res.json()
        except requests.exceptions.ConnectionError:
            log(f"⚠️ Connection error ({i+1}/{MAX_RETRIES})")
            if i < MAX_RETRIES - 1: time.sleep(5*(i+1))
        except Exception as e:
            if i < MAX_RETRIES - 1: time.sleep(3)
    send_telegram("🔴 <b>API ไม่ตอบสนอง!</b> ตรวจสอบ internet")
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
        return (r.get("h",[]), r.get("l",[]), r.get("c",[])) if r.get("s")=="ok" else ([],[],[])
    except: return [], [], []


def get_balances():
    d = api_request("POST", "/api/v3/market/balances")
    return d.get("result", {}) if d.get("error") == 0 else {}


def get_thb():
    v = get_balances().get("THB", {}); return float(v.get("available",0)) if isinstance(v,dict) else float(v)


def get_coin():
    coin = SYMBOL.split("_")[1]; v = get_balances().get(coin, {})
    return float(v.get("available",0)) if isinstance(v,dict) else float(v)


def place_order(side, amount):
    coin = SYMBOL.split("_")[1].lower()
    path = "/api/v3/market/place-bid" if side=="buy" else "/api/v3/market/place-ask"
    return api_request("POST", path, {"sym":f"{coin}_thb","amt":amount,"rat":0,"typ":"market"})


def do_sell(coin_bal, price, reason):
    global entry_price, peak_price, trade_count, win_count, total_pnl, last_trade_time
    r = place_order("sell", coin_bal)
    if r.get("error") == 0:
        pnl        = (price - entry_price) / entry_price * (coin_bal * entry_price) if entry_price else 0
        total_pnl += pnl
        trade_count += 1
        if pnl > 0: win_count += 1
        last_trade_time = time.time()
        log(f"✅ {reason} | {pnl:+.2f} บาท")
        send_telegram(
            f"{'🚨' if 'Stop' in reason else '🔴'} <b>{reason}</b>\n"
            f"กลยุทธ์: {current_strategy}\n"
            f"ราคาซื้อ: {entry_price:,.0f} | ราคาขาย: {price:,.0f}\n"
            f"{'📈' if pnl>=0 else '📉'} กำไร/ขาดทุน: {pnl:+.2f} บาท\n"
            f"รวม: {total_pnl:+.2f} บาท"
        )
        entry_price = peak_price = None
        return True
    send_telegram(f"❌ <b>ขายไม่สำเร็จ</b>\n{r}")
    return False


# ==================== Indicators ====================

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains  = [max(closes[i]-closes[i-1],0) for i in range(-period,0)]
    losses = [max(closes[i-1]-closes[i],0) for i in range(-period,0)]
    ag,al  = sum(gains)/period, sum(losses)/period
    return round(100-(100/(1+ag/al)),2) if al!=0 else 100


def calc_ma(closes, period):
    return sum(closes[-period:])/period if len(closes)>=period else None


def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period*2: return None
    tr_l,pdm_l,ndm_l = [],[],[]
    for i in range(1, len(closes)):
        h,l,pc = highs[i],lows[i],closes[i-1]
        tr_l.append(max(h-l, abs(h-pc), abs(l-pc)))
        pdm_l.append(max(h-highs[i-1],0) if (h-highs[i-1])>(lows[i-1]-l) else 0)
        ndm_l.append(max(lows[i-1]-l,0) if (lows[i-1]-l)>(h-highs[i-1]) else 0)

    def smooth(data, p):
        s = sum(data[:p]); res = [s]
        for v in data[p:]: s=s-s/p+v; res.append(s)
        return res

    atr,pdm_s,ndm_s = smooth(tr_l,period), smooth(pdm_l,period), smooth(ndm_l,period)
    dx_l = []
    for i in range(len(atr)):
        pdi = 100*pdm_s[i]/atr[i] if atr[i] else 0
        ndi = 100*ndm_s[i]/atr[i] if atr[i] else 0
        dx_l.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi) else 0)
    return round(sum(dx_l[-period:])/period, 2) if len(dx_l)>=period else None


def calc_bb_width(closes, period=20):
    if len(closes)<period: return None
    sl   = closes[-period:]; mean = sum(sl)/period
    std  = math.sqrt(sum((x-mean)**2 for x in sl)/period)
    return round((mean+2*std-(mean-2*std))/mean, 4)


def calc_volatility(closes, period=24):
    """วัด volatility — ถ้าสูงเกินไป = ตลาด volatile"""
    if len(closes)<period: return None
    sl   = closes[-period:]
    mean = sum(sl)/period
    return math.sqrt(sum((x-mean)**2 for x in sl)/period) / mean


# ==================== Market Regime ====================

def detect_regime():
    global current_regime, current_strategy, last_regime_check, regime_details

    log("🔍 กำลังวิเคราะห์สภาพตลาด (Daily)...")

    # ดึงกราฟรายวัน
    highs_1d, lows_1d, closes_1d = get_candles(SYMBOL, "1D", ADX_PERIOD*3+10)
    _, _, closes_1h               = get_candles(SYMBOL, "60", 200)

    if len(closes_1d) < ADX_PERIOD*2 or len(closes_1h) < 20:
        log("⚠️ ข้อมูลไม่พอสำหรับ regime detection")
        return current_regime

    score    = 0
    details  = {}

    # 1. ADX รายวัน — วัด trend strength
    adx = calc_adx(highs_1d, lows_1d, closes_1d, ADX_PERIOD)
    adx_ok = adx is not None and adx < ADX_THRESHOLD
    if adx_ok: score += 1
    details["adx"]    = f"{adx:.1f}" if adx else "—"
    details["adx_ok"] = adx_ok

    # 2. Bollinger Band Width รายชั่วโมง
    bb = calc_bb_width(closes_1h, BB_PERIOD)
    bb_ok = bb is not None and bb < BB_SQUEEZE_PCT
    if bb_ok: score += 1
    details["bb"]    = f"{bb*100:.2f}%" if bb else "—"
    details["bb_ok"] = bb_ok

    # 3. Weekly Range
    wr = (max(closes_1h[-168:]) - min(closes_1h[-168:])) / min(closes_1h[-168:]) if len(closes_1h)>=168 else None
    wr_ok = wr is not None and wr < WEEKLY_RANGE_PCT
    if wr_ok: score += 1
    details["wr"]    = f"{wr*100:.2f}%" if wr else "—"
    details["wr_ok"] = wr_ok
    details["score"] = score

    # 4. Volatility check
    vol = calc_volatility(closes_1h, 24)

    # ตัดสินใจ regime
    prev_regime    = current_regime
    prev_strategy  = current_strategy

    if vol and vol > 0.04:
        new_regime   = "VOLATILE"
        new_strategy = "NONE"
    elif score >= REGIME_SCORE_MIN:
        new_regime   = "SIDEWAY"
        new_strategy = "RSI"
    else:
        new_regime   = "TRENDING"
        new_strategy = "MA"

    current_regime   = new_regime
    current_strategy = new_strategy
    last_regime_check = time.time()
    regime_details   = details

    log(f"🔍 Regime: {current_regime} | Strategy: {current_strategy} | Score: {score}/3 | ADX: {details['adx']} | BB: {details['bb']} | WR: {details['wr']}")

    # แจ้ง Telegram ถ้าเปลี่ยน
    if new_regime != prev_regime or new_strategy != prev_strategy:
        icons = {"SIDEWAY":"📊","TRENDING":"📈","VOLATILE":"⚡"}
        strat_desc = {
            "RSI":  "RSI14 (1H) — ซื้อตอน Oversold",
            "MA":   "MA12/26 (4H) — ซื้อตาม Trend",
            "NONE": "หยุดรอ — ตลาดผันผวนมาก"
        }
        send_telegram(
            f"{icons.get(new_regime,'❓')} <b>ตลาดเปลี่ยนเป็น {new_regime}!</b>\n"
            f"{'─'*25}\n"
            f"ADX: {details['adx']} {'✅' if adx_ok else '❌'}\n"
            f"BB Width: {details['bb']} {'✅' if bb_ok else '❌'}\n"
            f"Weekly Range: {details['wr']} {'✅' if wr_ok else '❌'}\n\n"
            f"🧠 เปลี่ยนกลยุทธ์เป็น: <b>{strat_desc.get(new_strategy,'—')}</b>"
        )

    return current_regime


# ==================== Strategies ====================

def strategy_rsi(closes_1h):
    """RSI Strategy สำหรับตลาด Sideway"""
    rsi = calc_rsi(closes_1h, RSI_PERIOD)
    if rsi is None: return None, None
    if rsi < RSI_OVERSOLD:  return "buy",  f"RSI {rsi:.1f} (Oversold)"
    if rsi > RSI_OVERBOUGHT: return "sell", f"RSI {rsi:.1f} (Overbought)"
    return "hold", f"RSI {rsi:.1f}"


def strategy_ma(closes_4h):
    """MA Crossover สำหรับตลาด Trending"""
    ma_fast = calc_ma(closes_4h, MA_FAST)
    ma_slow = calc_ma(closes_4h, MA_SLOW)
    if not ma_fast or not ma_slow: return None, None

    # ต้องการ confirmation: MA ต้องตัดกันชัดเจน (> 0.3%)
    diff_pct = (ma_fast - ma_slow) / ma_slow * 100
    if diff_pct > 0.3:  return "buy",  f"MA{MA_FAST} > MA{MA_SLOW} (+{diff_pct:.2f}%)"
    if diff_pct < -0.3: return "sell", f"MA{MA_FAST} < MA{MA_SLOW} ({diff_pct:.2f}%)"
    return "hold", f"MA spread: {diff_pct:.2f}% (รอ confirmation)"


# ==================== Main Loop ====================

def run_bot():
    global entry_price, peak_price, is_paused, pause_until
    global trade_count, win_count, total_pnl, current_regime, last_trade_time

    log("🤖 Bitkub Adaptive Bot เริ่มทำงาน!")
    log(f"⏱️  Scan: ทุก {SCAN_INTERVAL//60} นาที | Regime: ทุก {REGIME_INTERVAL//3600} ชั่วโมง | Cooldown: {TRADE_COOLDOWN//3600} ชั่วโมง")
    log("=" * 60)

    send_telegram(
        "🤖 <b>Bitkub Adaptive Bot เริ่มทำงาน!</b>\n"
        f"{'─'*25}\n"
        f"🧠 ปรับกลยุทธ์อัตโนมัติตามสภาพตลาด\n"
        f"📊 Sideway → RSI14 (1H)\n"
        f"📈 Trending → MA12/26 (4H)\n"
        f"⚡ Volatile → หยุดรอ\n\n"
        f"⏱️ ตรวจสัญญาณทุก {SCAN_INTERVAL//60} นาที\n"
        f"🔄 วิเคราะห์ตลาดทุก {REGIME_INTERVAL//3600} ชั่วโมง\n"
        f"⏳ Cooldown {TRADE_COOLDOWN//3600} ชั่วโมงระหว่าง trade\n"
        f"🛑 Hard Stop: {STOP_LOSS_PCT*100:.0f}% | Trailing: {TRAILING_STOP_PCT*100:.0f}%\n\n"
        f"พิมพ์ /help เพื่อดูคำสั่งทั้งหมด"
    )

    threading.Thread(target=process_commands, daemon=True).start()

    # วิเคราะห์ตลาดครั้งแรก
    detect_regime()
    prev_signal = None

    while True:
        try:
            # เช็ค pause
            if is_paused:
                if pause_until and time.time() >= pause_until:
                    is_paused = False; pause_until = None
                    send_telegram("▶️ <b>Bot เริ่มเทรดใหม่อัตโนมัติ</b>")
                else:
                    time.sleep(SCAN_INTERVAL); continue

            # วิเคราะห์ตลาดทุก 4 ชั่วโมง
            if time.time() - last_regime_check > REGIME_INTERVAL:
                detect_regime()

            # ดึงข้อมูลตาม strategy
            _, _, closes_1h = get_candles(SYMBOL, RSI_RESOLUTION, RSI_PERIOD+15)
            _, _, closes_4h = get_candles(SYMBOL, MA_RESOLUTION,  MA_SLOW+10)

            if not closes_1h:
                log("⏳ ดึงข้อมูลไม่ได้ รอต่อไป...")
                time.sleep(SCAN_INTERVAL); continue

            current_price = closes_1h[-1]

            # อัปเดต peak
            if entry_price and current_price > (peak_price or 0):
                peak_price = current_price

            # แสดง log สรุป
            pct_text = f" | {(current_price-entry_price)/entry_price*100:+.2f}%" if entry_price else ""
            log(f"💰 {current_price:,.0f} | Regime: {current_regime} | Strategy: {current_strategy}{pct_text}")

            # ========== Hard Stop Loss ==========
            if entry_price and (current_price-entry_price)/entry_price <= -STOP_LOSS_PCT:
                cb = get_coin()
                if cb > 0: do_sell(cb, current_price, f"Hard Stop Loss {STOP_LOSS_PCT*100:.0f}%")
                prev_signal = "sell"; time.sleep(SCAN_INTERVAL); continue

            # ========== Trailing Stop ==========
            if entry_price and peak_price and (current_price-peak_price)/peak_price <= -TRAILING_STOP_PCT:
                cb  = get_coin()
                pct = (current_price-peak_price)/peak_price*100
                if cb > 0: do_sell(cb, current_price, f"Trailing Stop ลง {abs(pct):.1f}% จาก peak")
                prev_signal = "sell"; time.sleep(SCAN_INTERVAL); continue

            # ========== ไม่เทรดถ้า Volatile ==========
            if current_strategy == "NONE":
                log(f"⚡ ตลาด Volatile — รอ")
                time.sleep(SCAN_INTERVAL); continue

            # ========== เช็ค Cooldown ==========
            cooldown_remaining = TRADE_COOLDOWN - (time.time() - last_trade_time)
            if last_trade_time and cooldown_remaining > 0 and not entry_price:
                log(f"⏳ Cooldown: อีก {int(cooldown_remaining/60)} นาที")
                time.sleep(SCAN_INTERVAL); continue

            # ========== เลือก Strategy ==========
            if current_strategy == "RSI":
                signal, reason = strategy_rsi(closes_1h)
            elif current_strategy == "MA" and closes_4h:
                signal, reason = strategy_ma(closes_4h)
            else:
                signal, reason = "hold", "ไม่มีข้อมูลเพียงพอ"

            if signal is None:
                time.sleep(SCAN_INTERVAL); continue

            log(f"📡 Signal: {signal.upper()} | {reason}")

            # ========== BUY ==========
            if signal == "buy" and prev_signal != "buy":
                thb    = get_thb()
                amount = round(thb * POSITION_PCT, 2)
                if amount < MIN_ORDER_THB:
                    send_telegram(f"⚠️ <b>ยอดเงินไม่พอ</b>\nมี {thb:,.2f} บาท")
                else:
                    r = place_order("buy", amount)
                    if r.get("error") == 0:
                        entry_price = current_price
                        peak_price  = current_price
                        trade_count += 1
                        last_trade_time = time.time()
                        log(f"✅ BUY {amount:,.2f} บาท | {reason}")
                        send_telegram(
                            f"🟢 <b>BUY {SYMBOL.split('_')[1]}</b>\n"
                            f"กลยุทธ์: {current_strategy} ({reason})\n"
                            f"ตลาด: {current_regime}\n"
                            f"ราคา: {current_price:,.0f} บาท\n"
                            f"ใช้เงิน: {amount:,.2f} บาท\n"
                            f"🛑 Hard Stop: {current_price*(1-STOP_LOSS_PCT):,.0f}\n"
                            f"📉 Trailing: {TRAILING_STOP_PCT*100:.0f}% จาก peak"
                        )
                    else:
                        send_telegram(f"❌ <b>ซื้อไม่สำเร็จ</b>\n{r}")
                prev_signal = "buy"

            # ========== SELL ==========
            elif signal == "sell" and prev_signal != "sell":
                cb = get_coin()
                if cb > 0: do_sell(cb, current_price, f"{current_strategy} Sell ({reason})")
                else: log("⚠️ ไม่มีเหรียญในกระเป๋า")
                prev_signal = "sell"

            # ========== HOLD ==========
            else:
                if signal != prev_signal: prev_signal = signal

        except KeyboardInterrupt:
            log("🛑 หยุด bot")
            send_telegram(f"🛑 <b>Bot หยุดทำงาน</b>\nTrade: {trade_count} | PnL: {total_pnl:+.2f} บาท")
            break
        except Exception as e:
            log(f"❌ Error: {e}")
            send_telegram(f"❌ <b>Bot Error!</b>\n{e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("❌ ไม่พบ API Key!"); exit(1)
    run_bot()
