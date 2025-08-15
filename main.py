import os, time, json, hmac, hashlib, base64, requests, math
from urllib.parse import urlencode

# ========= Settings from GitHub Secrets =========
LIVE = os.getenv("LIVE", "false").lower() == "true"
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "BTCUSD,ETHUSD,ADAUSD,XRPUSD").split(",")]
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "20"))  # target per trade
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ========= Kraken HTTP =========
BASE = "https://urldefense.com/v3/__https://api.kraken.com__;!!P7nkOOY!rWsRS--PmlCUkVju37PVbWgfRhatjvB1EKEzvOm3nAcGf_5QmCdj4LDBev2xBR6gtWQfR6V2h9sMmbQaUUUTPMRR7Wkrxp23Qg$ "
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

# ========= Strategy (aggressive, keeps capital moving) =========
INTERVAL_MIN = 1
EMA_FAST = 9
EMA_SLOW = 21

TAKE_PROFIT = 0.015      # +1.5%
STOP_LOSS   = 0.005      # -0.5%
TRAIL_ACTIVATE = 0.008   # trail starts after +0.8%
TRAIL_PCT      = 0.004   # 0.4% trail

MAX_HOLD_MIN     = 20    # force exit after 20 minutes
SMALL_LOSS_EXIT  = 0.002 # allow -0.2% on timed exit
RESERVE_USD      = 1.0   # keep a little USD for fees
LOW_USD_FORCE    = 5.0   # if USD < this, force-sell stale positions to free funds

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

def latest_trade(kpair, side=None):
   try:
       res = http_post_private("/0/private/TradesHistory", {"type": "all"})
       trades = list(res.get("trades", {}).values())
       trades = [t for t in trades if t.get("pair") == kpair]
       if side: trades = [t for t in trades if t.get("type") == side]
       if not trades: return (None, None, None, None)
       trades.sort(key=lambda t: t.get("time", 0), reverse=True)
       t = trades[0]
       return float(t["price"]), float(t["vol"]), float(t["time"]), t["type"]
   except:
       return (None, None, None, None)

# ========= Orders (instant fills) =========
def place_market(kpair, side, volume):
   payload = {"ordertype": "market", "type": side, "pair": kpair, "volume": str(volume)}
   if LIVE: return http_post_private("/0/private/AddOrder", payload)
   return {"simulated": True, "side": side, "pair": kpair, "volume": volume}

# ========= Trading per pair =========
def trade_pair(alt_pair):
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

   last_any = latest_trade(kpair)
   last_buy, _, t_buy, _ = latest_trade(kpair, side="buy")

   print(json.dumps({"pair": alt_pair, "price": price, "hold": hold, "usd": usd,
                     "ema_fast": round(f_now,6), "ema_slow": round(s_now,6),
                     "last_buy": last_buy}))

   # ----- FREE FUNDS if USD very low: liquidate stale positions (older than MAX_HOLD_MIN)
   if usd < LOW_USD_FORCE and hold >= ordermin and t_buy and (time.time() - t_buy) >= MAX_HOLD_MIN*60:
       vol = round_qty(hold, lot_dec)
       res = place_market(kpair, "sell", vol)
       print(f"[{alt_pair}] FORCE-SELL to free USD, {vol} @ market -> {res}")
       return

   # ----- ENTRY: no position + bullish cross -> BUY using available USD
   if hold < ordermin * 0.999 and bullish_cross(f_prev, s_prev, f_now, s_now):
       budget = max(0.0, min(USD_PER_TRADE, usd - RESERVE_USD))
       if budget >= price * ordermin:
           vol = round_qty(max(budget / price, ordermin), lot_dec)
           if vol >= ordermin:
               res = place_market(kpair, "buy", vol)
               print(f"[{alt_pair}] BUY {vol} @ market -> {res}")
               return
       print(f"[{alt_pair}] SKIP BUY (not enough USD) budget={budget:.4f}, need>={price*ordermin:.4f}")
       return

   # ----- EXIT: TP/SL/Trailing/Timed/Bearish
   sell_reason = None
   now = time.time()

   # Trailing stop after activation
   if last_buy and t_buy:
       highs_since_buy = [h for (h, t) in zip(highs, times) if t >= t_buy]
       if highs_since_buy:
           peak = max(highs_since_buy)
           if peak >= last_buy * (1 + TRAIL_ACTIVATE):
               trail_stop = peak * (1 - TRAIL_PCT)
               if price <= trail_stop:
                   sell_reason = f"Trailing stop {TRAIL_PCT*100:.2f}%"

   if not sell_reason and last_buy:
       if price >= last_buy * (1 + TAKE_PROFIT): sell_reason = "TP"
       elif price <= last_buy * (1 - STOP_LOSS): sell_reason = "SL"

   if not sell_reason and last_buy and t_buy and (now - t_buy) >= MAX_HOLD_MIN * 60:
       # Timed exit: get out even if flat / small loss to keep capital moving
       if price >= last_buy * (1 - SMALL_LOSS_EXIT):
           sell_reason = f"Timed exit {MAX_HOLD_MIN}m"

   if not sell_reason and bearish_cross(f_prev, s_prev, f_now, s_now):
       sell_reason = "Bearish cross"

   if sell_reason and hold >= ordermin:
       vol = round_qty(hold, lot_dec)
       res = place_market(kpair, "sell", vol)
       print(f"[{alt_pair}] SELL ({sell_reason}) {vol} @ market -> {res}")
   else:
       print(f"[{alt_pair}] no action")

# ========= Main =========
def main():
   if LIVE and (not API_KEY or not API_SECRET):
       raise RuntimeError("LIVE=True but API keys missing.")
   for p in PAIRS:
       try:
           trade_pair(p); time.sleep(1)
       except Exception as e:
           print(f"[{p}] ERROR: {e}")

if __name__ == "__main__":
   main()
