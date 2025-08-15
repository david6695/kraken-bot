import os, time, json, hmac, hashlib, base64, requests, math
from urllib.parse import urlencode

# ===== Settings (from GitHub Secrets) =====
LIVE = os.getenv("LIVE", "false").lower() == "true"
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "BTCUSD,ETHUSD,ADAUSD,XRPUSD").split(",")]
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "10"))
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ===== Kraken HTTP =====
BASE = "https://urldefense.com/v3/__https://api.kraken.com__;!!P7nkOOY!sHlcJYdLwZerffKHNwO6d37dl6t2pyNt52Jfj6xKUAZcHMSTAaB7EP5xoDblGJxZxZ2ByLRU8qooZv90nOJ1XkKftOEBnnCFBw$ "
TIMEOUT = 20

def http_get(path, params=None):
   r = requests.get(BASE + path, params=params or {}, timeout=TIMEOUT)
   r.raise_for_status()
   data = r.json()
   if data.get("error"):
       raise RuntimeError(data["error"])
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
   if data.get("error"):
       raise RuntimeError(data["error"])
   return data["result"]

# ===== Fast scalping settings =====
INTERVAL_MIN = 1           # 1‑minute candles
EMA_FAST = 9
EMA_SLOW = 21
TAKE_PROFIT = 0.008        # +0.8%
STOP_LOSS   = 0.004        # −0.4%
COOLDOWN_MIN = 2           # per-pair cooldown to avoid spam

# ---- TA helpers ----
def ema(series, period):
   k = 2 / (period + 1); vals = []; prev = None
   for price in series:
       prev = price if prev is None else price * k + prev * (1 - k)
       vals.append(prev)
   return vals

def bullish_cross(fp, sp, fn, sn): return fp <= sp and fn > sn
def bearish_cross(fp, sp, fn, sn): return fp >= sp and fn < sn

def round_qty(qty, lot_decimals):
   q = 10 ** lot_decimals
   return math.floor(qty * q) / q

def round_price(px, price_decimals):
   q = 10 ** price_decimals
   return math.floor(px * q) / q

# ---- Data fetch ----
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

def get_ticker(kpair):
   res = http_get("/0/public/Ticker", {"pair": kpair})
   k = list(res.keys())[0]
   last = float(res[k]["c"][0]); bid = float(res[k]["b"][0]); ask = float(res[k]["a"][0])
   return last, bid, ask

def get_closes(kpair, interval=1, count=200):
   res = http_get("/0/public/OHLC", {"pair": kpair, "interval": interval})
   for k, v in res.items():
       if k != "last":
           return [float(c[4]) for c in v[-count:]]
   raise RuntimeError("No OHLC data")

def balances():
   try: return http_post_private("/0/private/Balance", {})
   except: return {}

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
   except: return (None, None, None, None)

# ---- Order placement (maker post‑only to cut fees) ----
def place_limit_post_only(kpair, side, volume, price):
   payload = {
       "ordertype": "limit",
       "type": side,
       "pair": kpair,
       "volume": str(volume),
       "price": str(price),
       "oflags": "post"  # maker only
   }
   if LIVE:
       return http_post_private("/0/private/AddOrder", payload)
   return {"simulated": True, "side": side, "pair": kpair, "volume": volume, "price": price}

# ---- Per‑pair trading logic ----
def trade_pair(alt_pair):
   try:
       meta = resolve_pair(alt_pair)
   except Exception as e:
       print(f"[{alt_pair}] resolve error: {e}"); return

   kpair, asset = meta["kpair"], meta["asset"]
   lot_dec, ordermin, price_dec = meta["lot_decimals"], meta["ordermin"], meta["price_decimals"]

   last, bid, ask = get_ticker(kpair)
   closes = get_closes(kpair, interval=INTERVAL_MIN, count=max(EMA_SLOW + 30, 120))
   ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
   f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]

   bals = balances(); hold = float(bals.get(asset, "0") or 0.0)
   last_any = latest_trade(kpair)
   last_buy, _, _, _ = latest_trade(kpair, side="buy")

   print(json.dumps({"pair": alt_pair, "price": last, "fast": round(f_now,6),
                     "slow": round(s_now,6), "hold": hold, "last_buy": last_buy}))

   # Cooldown (avoid spam)
   if last_any[2] and (time.time() - last_any[2]) < COOLDOWN_MIN * 60:
       print(f"[{alt_pair}] cooldown"); return

   # ENTRY — no position & bullish cross -> maker limit slightly under bid
   if hold < ordermin * 0.999 and bullish_cross(f_prev, s_prev, f_now, s_now):
       usd = max(USD_PER_TRADE, 5)
       vol = round_qty(max(usd / last, ordermin), lot_dec)
       if vol >= ordermin:
           buy_px = round_price(bid * (1 - 0.0002), price_dec)
           res = place_limit_post_only(kpair, "buy", vol, buy_px)
           print(f"[{alt_pair}] BUY {vol} @ {buy_px} -> {res}")
       return

   # EXIT — TP/SL or bearish cross -> maker limit slightly above ask
   sell_reason = None
   if last_buy:
       if last >= last_buy * (1 + TAKE_PROFIT): sell_reason = "TP"
       elif last <= last_buy * (1 - STOP_LOSS): sell_reason = "SL"
   if not sell_reason and bearish_cross(f_prev, s_prev, f_now, s_now): sell_reason = "Bearish"

   if sell_reason and hold >= ordermin:
       vol = round_qty(hold, lot_dec)
       sell_px = round_price(ask * (1 + 0.0002), price_dec)
       res = place_limit_post_only(kpair, "sell", vol, sell_px)
       print(f"[{alt_pair}] SELL ({sell_reason}) {vol} @ {sell_px} -> {res}")
   else:
       print(f"[{alt_pair}] no action")

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
