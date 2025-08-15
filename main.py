import os, time, json, hmac, hashlib, base64, requests, math
from urllib.parse import urlencode

# ========= Settings from GitHub Secrets =========
LIVE = os.getenv("LIVE", "false").lower() == "true"
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "BTCUSD,ETHUSD,ADAUSD,XRPUSD,SOLUSD").split(",")]
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "20"))  # auto-capped by actual USD
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ========= Kraken HTTP =========
BASE = "https://urldefense.com/v3/__https://api.kraken.com__;!!P7nkOOY!qaUVyI9A3kGSIUC9uMD_SPHlu_ymJoKbgu0XK5mPORxADPxIL-wpIugTSEm4UDuZCmJCDbomj_MBYxSQoivlSd3hTP1e9Zk5pA$ "
TIMEOUT = 20

def http_get(path, params=None):
   r = requests.get(BASE + path, params=params or {}, timeout=TIMEOUT)
   r.raise_for_status()
   data = r.json()
   if data.get("error"): raise RuntimeError(data["error"])
   return data["result"]

def http_post_private(path, data):
   nonce = str(int(time.time() * 1000))
   data = dict(data or {}, nonce=nonce)
   postdata = urlencode(data)
   message = (nonce + postdata).encode()
   sha256 = hashlib.sha256(message).digest()
   mac = hmac.new(base64.b64decode(API_SECRET), (path.encode() + sha256), hashlib.sha512)
   sig = base64.b64encode(mac.digest()).decode()
   headers = {"API-Key": API_KEY, "API-Sign": sig}
   r = requests.post(BASE + path, headers=headers, data=data, timeout=TIMEOUT)
   r.raise_for_status()
   data = r.json()
   if data.get("error"): raise RuntimeError(data["error"])
   return data["result"]

# ========= Aggressive trading params =========
INTERVAL_MIN = 1
EMA_FAST = 9
EMA_SLOW = 21

TAKE_PROFIT = 0.015      # +1.5% TP
STOP_LOSS   = 0.005      # -0.5% SL
TRAIL_ACTIVATE = 0.008   # start trailing after +0.8%
TRAIL_PCT      = 0.004   # trail by 0.4%

MAX_HOLD_MIN        = 20   # force exit after 20 minutes if flat
SMALL_LOSS_EXIT     = 0.002
RESERVE_USD         = 1.0  # keep a little USD for fees
LOW_USD_FORCE       = 5.0  # if USD < this and bag is old -> free funds
FORCE_BUY_IF_IDLE_M = 10   # if no trade in >=10 min and USD available -> force buy

# ========= Helpers =========
def ema(series, period):
   k = 2/(period+1); vals=[]; prev=None
   for price in series:
       prev = price if prev is None else price*k + prev*(1-k)
       vals.append(prev)
   return vals

def bullish_cross(fp, sp, fn, sn): return fp <= sp and fn > sn
def bearish_cross(fp, sp, fn, sn): return fp >= sp and fn < sn

def round_qty(qty, lot_decimals):
   q = 10 ** lot_decimals
   return math.floor(qty * q) / q

def resolve_pair(altname):
   res = http_get("/0/public/AssetPairs", {"pair": altname})
   key = list(res.keys())[0]
   d = res[key]
   return {
       "kpair": key,
       "asset": d["base"],
       "lot_decimals": d.get("lot_decimals", 6),
       "ordermin": float(d.get("ordermin", "0.0001")),
       "price_decimals": d.get("pair_decimals", 2),
   }

def get_last_price(kpair):
   res = http_get("/0/public/Ticker", {"pair": kpair})
   k = list(res.keys())[0]
   return float(res[k]["c"][0])

def get_ohlc(kpair, interval=1, count=300):
   res = http_get("/0/public/OHLC", {"pair": kpair, "interval": interval})
   for kk, rows in res.items():
       if kk != "last":
           # rows: [time, open, high, low, close, vwap, volume, count]
           rows = rows[-count:]
           times  = [float(r[0]) for r in rows]
           highs  = [float(r[2]) for r in rows]
           closes = [float(r[4]) for r in rows]
           return times, highs, closes
   raise RuntimeError("No OHLC data")

def balances():
   try: return http_post_private("/0/private/Balance", {})
   except: return {}

def usd_balance_from(bals):
   try: return float(bals.get("ZUSD") or bals.get("USD") or 0.0)
   except: return 0.0

def last_trade_any():
   try:
       res = http_post_private("/0/private/TradesHistory", {"type":"all"})
       trades = list(res.get("trades", {}).values())
       if not trades: return None
       trades.sort(key=lambda t: t.get("time",0), reverse=True)
       return float(trades[0]["time"])
   except: return None

def latest_trade(kpair, side=None):
   try:
       res = http_post_private("/0/private/TradesHistory", {"type":"all"})
       trades = list(res.get("trades", {}).values())
       trades = [t for t in trades if t.get("pair")==kpair]
       if side: trades = [t for t in trades if t.get("type")==side]
       if not trades: return (None,None,None,None)
       trades.sort(key=lambda t: t.get("time",0), reverse=True)
       t = trades[0]
       return float(t["price"]), float(t["vol"]), float(t["time"]), t["type"]
   except: return (None,None,None,None)

# ========= Orders (market = instant fill) =========
def place_market(kpair, side, volume):
   payload = {"ordertype":"market","type":side,"pair":kpair,"volume":str(volume)}
   if LIVE: return http_post_private("/0/private/AddOrder", payload)
   return {"simulated":True,"side":side,"pair":kpair,"volume":volume}

# ========= Per‑pair trading =========
def trade_pair(alt_pair, idle_minutes_since_last_any):
   try:
       meta = resolve_pair(alt_pair)
   except Exception as e:
       print(f"[{alt_pair}] resolve error: {e}"); return

   kpair, asset = meta["kpair"], meta["asset"]
   lot_dec, ordermin = meta["lot_decimals"], meta["ordermin"]

   price = get_last_price(kpair)
   times, highs, closes = get_ohlc(kpair, interval=INTERVAL_MIN, count=max(EMA_SLOW+60, 240))
   ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
   f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]

   bals = balances()
   hold = float(bals.get(asset, "0") or 0.0)
   usd  = usd_balance_from(bals)

   last_buy, _, t_buy, _ = latest_trade(kpair, side="buy")

   print(json.dumps({"pair":alt_pair,"price":price,"hold":hold,"usd":usd,
                     "fast":round(f_now,6),"slow":round(s_now,6),"last_buy":last_buy}))

   # ---- FREE FUNDS if USD very low & bag is old
   if usd < LOW_USD_FORCE and hold >= ordermin and t_buy and (time.time() - t_buy) >= MAX_HOLD_MIN*60:
       vol = round_qty(hold, lot_dec)
       res = place_market(kpair, "sell", vol)
       print(f"[{alt_pair}] FORCE-SELL stale bag {vol} -> {res}")
       return

   # ---- ENTRY (aggressive):
   # Prefer bullish cross OR momentum (price above slow EMA).
   # If idle too long (>= FORCE_BUY_IF_IDLE_M), force a buy with available USD.
   want_entry = bullish_cross(f_prev, s_prev, f_now, s_now) or (closes[-1] > es[-1])

   if hold < ordermin * 0.999:
       budget = max(0.0, min(USD_PER_TRADE, usd - RESERVE_USD))
       if (idle_minutes_since_last_any is not None and idle_minutes_since_last_any >= FORCE_BUY_IF_IDLE_M):
           want_entry = True  # force activity after idle window

       if want_entry and budget >= price * ordermin:
           vol = round_qty(max(budget / price, ordermin), lot_dec)
           if vol >= ordermin:
               res = place_market(kpair, "buy", vol)
               print(f"[{alt_pair}] BUY {vol} @ market -> {res}")
               return
       elif want_entry:
           print(f"[{alt_pair}] WANT BUY but not enough USD (budget={budget:.4f}, need>={price*ordermin:.4f})")
       # if no entry conditions, fall through to exits (if any)

   # ---- EXIT (TP/SL/Trailing/Timed/Bearish)
   if hold >= ordermin and last_buy:
       sell_reason = None
       now = time.time()

       # trailing stop (after activation)
       highs_since_buy = [h for (h, t) in zip(highs, times) if t >= (t_buy or 0)]
       if highs_since_buy:
           peak = max(highs_since_buy)
           if peak >= last_buy * (1 + TRAIL_ACTIVATE):
               trail_stop = peak * (1 - TRAIL_PCT)
               if price <= trail_stop:
                   sell_reason = f"Trail {TRAIL_PCT*100:.2f}%"

       if not sell_reason:
           if price >= last_buy * (1 + TAKE_PROFIT): sell_reason = "TP"
           elif price <= last_buy * (1 - STOP_LOSS): sell_reason = "SL"

       if not sell_reason and t_buy and (now - t_buy) >= MAX_HOLD_MIN * 60:
           if price >= last_buy * (1 - SMALL_LOSS_EXIT):
               sell_reason = f"Timed {MAX_HOLD_MIN}m"

       if not sell_reason and bearish_cross(f_prev, s_prev, f_now, s_now):
           sell_reason = "Bearish"

       if sell_reason:
           vol = round_qty(hold, lot_dec)
           res = place_market(kpair, "sell", vol)
           print(f"[{alt_pair}] SELL ({sell_reason}) {vol} @ market -> {res}")
       else:
           print(f"[{alt_pair}] hold; no exit")

# ========= Main =========
def main():
   if LIVE and (not API_KEY or not API_SECRET):
       raise RuntimeError("LIVE=True but API keys missing.")
   last_any = last_trade_any()
   idle_m = None
   if last_any:
       idle_m = int((time.time() - last_any) / 60)
   for p in PAIRS:
       try:
           trade_pair(p, idle_m); time.sleep(1)
       except Exception as e:
           print(f"[{p}] ERROR: {e}")

if __name__ == "__main__":
   main()
