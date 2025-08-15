import os, time, json, hmac, hashlib, base64, requests, math
from urllib.parse import urlencode

LIVE = os.getenv("LIVE", "false").lower() == "true"
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "BTCUSD,ETHUSD,ADAUSD,XRPUSD").split(",")]
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "10"))

API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

BASE = "https://api.kraken.com"
TIMEOUT = 20

INTERVAL_MIN = 1
EMA_FAST = 9
EMA_SLOW = 21
TAKE_PROFIT = 0.008
STOP_LOSS   = 0.004
COOLDOWN_MIN = 2

def http_get(path, params=None):
   r = requests.get(BASE + path, params=params or {}, timeout=TIMEOUT)
   r.raise_for_status()
   out = r.json()
   if out.get("error"):
       raise RuntimeError(out["error"])
   return out["result"]

def http_post_private(path, data):
   nonce = str(int(time.time() * 1000))
   data["nonce"] = nonce
   postdata = urlencode(data)
   message = (nonce + postdata).encode()
   sha256 = hashlib.sha256(message).digest()
   mac = hmac.new(base64.b64decode(API_SECRET), (path.encode() + sha256), hashlib.sha512)
   sig = base64.b64encode(mac.digest())
   headers = {"API-Key": API_KEY, "API-Sign": sig.decode()}
   r = requests.post(BASE + path, headers=headers, data=data, timeout=TIMEOUT)
   r.raise_for_status()
   out = r.json()
   if out.get("error"):
       raise RuntimeError(out["error"])
   return out["result"]

def resolve_pair(altname):
   res = http_get("/0/public/AssetPairs", {"pair": altname})
   key = list(res.keys())[0]
   d = res[key]
   base = d["base"]
   lot_decimals = d.get("lot_decimals", 6)
   ordermin = float(d.get("ordermin", "0.0001"))
   price_decimals = d.get("pair_decimals", 2)
   return {"kpair": key, "asset": base, "lot_decimals": lot_decimals, "ordermin": ordermin, "price_decimals": price_decimals}

def get_ticker(kpair):
   res = http_get("/0/public/Ticker", {"pair": kpair})
   k = list(res.keys())[0]
   last = float(res[k]["c"][0])
   bid = float(res[k]["b"][0])
   ask = float(res[k]["a"][0])
   return last, bid, ask

def get_ohlc_closes(kpair, interval=1, count=200):
   res = http_get("/0/public/OHLC", {"pair": kpair, "interval": interval})
   for k, v in res.items():
       if k != "last":
           return [float(c[4]) for c in v[-count:]]
   raise RuntimeError("No OHLC data")

def balances():
   try:
       return http_post_private("/0/private/Balance", {})
   except:
       return {}

def latest_trade(kpair, side=None):
   try:
       res = http_post_private("/0/private/TradesHistory", {"type": "all"})
       trades = res.get("trades", {})
       lst = [t for t in trades.values() if t.get("pair") == kpair]
       if side:
           lst = [t for t in lst if t.get("type") == side]
       if not lst:
           return (None, None, None, None)
       lst.sort(key=lambda t: t.get("time", 0), reverse=True)
       t = lst[0]
       return float(t["price"]), float(t["vol"]), float(t["time"]), t["type"]
   except:
       return (None, None, None, None)

def ema(series, period):
   k = 2 / (period + 1)
   vals, prev = [], None
   for price in series:
       prev = price if prev is None else price * k + prev * (1 - k)
       vals.append(prev)
   return vals

def bullish_cross(f_prev, s_prev, f_now, s_now):
   return f_prev <= s_prev and f_now > s_now

def bearish_cross(f_prev, s_prev, f_now, s_now):
   return f_prev >= s_prev and f_now < s_now

def round_qty(qty, lot_decimals):
   q = 10 ** lot_decimals
   return math.floor(qty * q) / q

def round_price(px, price_decimals):
   q = 10 ** price_decimals
   return math.floor(px * q) / q

def place_limit_post_only(kpair, side, volume, price):
   payload = {
       "ordertype": "limit",
       "type": side,
       "volume": str(volume),
       "pair": kpair,
       "price": str(price),
       "oflags": "post"
   }
   if LIVE:
       return http_post_private("/0/private/AddOrder", payload)
   return {"simulated": True, "side": side, "pair": kpair, "volume": volume, "price": price}

def trade_pair(alt_pair):
   try:
       meta = resolve_pair(alt_pair)
   except Exception as e:
       print(f"[{alt_pair}] Unable to resolve pair: {e}; skipping.")
       return

   kpair = meta["kpair"]
   asset = meta["asset"]
   lot_dec = meta["lot_decimals"]
   ordermin = meta["ordermin"]
   price_dec = meta["price_decimals"]

   last, bid, ask = get_ticker(kpair)
   closes = get_ohlc_closes(kpair, interval=INTERVAL_MIN, count=max(EMA_SLOW + 30, 120))
   ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
   f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]

   bals = balances()
   hold = float(bals.get(asset, "0") or 0.0)

   last_any = latest_trade(kpair)
   last_buy_price, _, last_buy_time, _ = latest_trade(kpair, side="buy")

   print(json.dumps({
       "pair": alt_pair, "price": last,
       "ema_fast": round(f_now,6), "ema_slow": round(s_now,6),
       "hold": hold, "last_buy": last_buy_price
   }))

   if last_any[2] and (time.time() - last_any[2]) < COOLDOWN_MIN * 60:
       print(f"[{alt_pair}] Cooldown; skipping.")
       return

   if hold < ordermin * 0.999 and bullish_cross(f_prev, s_prev, f_now, s_now):
       usd = max(USD_PER_TRADE, 5)
       vol = max(usd / last, ordermin)
       vol = round_qty(vol, lot_dec)
       if vol >= ordermin:
           buy_px = round_price(bid * (1 - 0.0002), price_dec)
           res = place_limit_post_only(kpair, "buy", vol, buy_px)
           print(f"[{alt_pair}] BUY {vol} @ {buy_px} (maker) -> {res}")
       return

   sell_reason = None
   if last_buy_price:
       if last >= last_buy_price * (1 + TAKE_PROFIT):
           sell_reason = "Take Profit"
       elif last <= last_buy_price * (1 - STOP_LOSS):
           sell_reason = "Stop Loss"
   if not sell_reason and bearish_cross(f_prev, s_prev, f_now, s_now):
       sell_reason = "Bearish cross"

   if sell_reason and hold >= ordermin:
       vol = round_qty(hold, lot_dec)
       sell_px = round_price(ask * (1 + 0.0002), price_dec)
       res = place_limit_post_only(kpair, "sell", vol, sell_px)
       print(f"[{alt_pair}] SELL ({sell_reason}) {vol} @ {sell_px} (maker) -> {res}")
   else:
       print(f"[{alt_pair}] No action.")

def main():
   if LIVE and (not API_KEY or not API_SECRET):
       raise RuntimeError("LIVE=True but API keys missing.")
   for p in PAIRS:
       try:
           trade_pair(p)
           time.sleep(1)
       except Exception as e:
           print(f"[{p}] ERROR: {e}")

if __name__ == "__main__":
   main()
