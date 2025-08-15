_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ===== Kraken HTTP =====
BASE = "https://urldefense.com/v3/__https://api.kraken.com__;!!P7nkOOY!qQroiLdqVAEWmjl01juxLp9VH5wt6yEWGuazpLvFNXN7JnzZ0tsWj1l9S-NaG_P2GhOr8qlNCQAx6Rv5dus7w1TWvFmbd9y61Q$ "
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

# ===== Fast, aggressive scalping =====
INTERVAL_MIN = 1
EMA_FAST = 9
EMA_SLOW = 21
TAKE_PROFIT = 0.015   # +1.5% target
STOP_LOSS   = 0.005   # -0.5% cut
TRAIL_ACTIVATE = 0.008  # start trailing after +0.8%
TRAIL_PCT      = 0.004  # 0.4% trail once activated
COOLDOWN_MIN = 0        # allow action every minute

# ---------- TA helpers ----------
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

# ---------- Data fetch ----------
def resolve_pair(altname):
   res = http_get("/0/public/AssetPairs", {"pair": altname})
   key = list(res.keys())[0]
   d = res[key]
   return {
       "kpair": key,
       "asset": d["base"],
       "lot_decimals": d.get("lot_decimals", 6),
       "ordermin": float(d.get("ordermin", "0.0001")),
   }

def get_ticker_last(kpair):
   res = http_get("/0/public/Ticker", {"pair": kpair})
   k = list(res.keys())[0]
   return float(res[k]["c"][0])

def get_ohlc(kpair, interval=1, count=300):
   res = http_get("/0/public/OHLC", {"pair": kpair, "interval": interval})
   for k,v in res.items():
       if k != "last": return v[-count:]
   raise RuntimeError("No OHLC data")

def balances():
   try: return http_post_private("/0/private/Balance", {})
   except: return {}

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

# ---------- Instant-fill market orders ----------
def place_market(kpair, side, volume):
   payload = {"ordertype":"market","type":side,"pair":kpair,"volume":str(volume)}
   if LIVE: return http_post_private("/0/private/AddOrder", payload)
   return {"simulated":True,"side":side,"pair":kpair,"volume":volume}

# ---------- Per-pair trading ----------
def trade_pair(alt_pair):
   try: meta = resolve_pair(alt_pair)
   except Exception as e:
       print(f"[{alt_pair}] resolve error: {e}"); return

   kpair, asset = meta["kpair"], meta["asset"]
   lot_dec, ordermin = meta["lot_decimals"], meta["ordermin"]

   price = get_ticker_last(kpair)
   ohlc = get_ohlc(kpair, interval=INTERVAL_MIN, count= max(EMA_SLOW+60, 240))
   closes = [float(x[4]) for x in ohlc]
   highs  = [float(x[2]) for x in ohlc]
   ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
   f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]

   bals = balances(); hold = float(bals.get(asset,"0") or 0.0)
   last_any = latest_trade(kpair)
   last_buy, _, t_buy, _ = latest_trade(kpair, side="buy")

   print(json.dumps({"pair":alt_pair,"price":price,"fast":round(f_now,6),
                     "slow":round(s_now,6),"hold":hold,"last_buy":last_buy}))

   # Cooldown guard (disabled unless >0)
   if COOLDOWN_MIN and last_any[2] and (time.time()-last_any[2]) < COOLDOWN_MIN*60:
       print(f"[{alt_pair}] cooldown"); return

   # ENTRY — no position & bullish cross
   if hold < ordermin*0.999 and bullish_cross(f_prev,s_prev,f_now,s_now):
       usd = max(USD_PER_TRADE, 5)
       vol = round_qty(max(usd/price, ordermin), lot_dec)
       if vol >= ordermin:
           res = place_market(kpair,"buy",vol)
           print(f"[{alt_pair}] BUY {vol} @ market -> {res}")
       return

   # EXIT — TP/SL or trailing/bearish
   sell_reason = None
   # trailing stop based on max high since last buy
   if last_buy and t_buy:
       # filter highs since buy time
       highs_since_buy = [h for (h,t) in zip(highs,[float(x[0]) for x in ohlc]) if t >= t_buy]
       if highs_since_buy:
           peak = max(highs_since_buy)
           if peak >= last_buy*(1+TRAIL_ACTIVATE):
               trail_stop = peak*(1-TRAIL_PCT)
               if price <= trail_stop:
                   sell_reason = f"Trailing stop {TRAIL_PCT*100:.2f}%"

   if not sell_reason and last_buy:
       if price >= last_buy*(1+TAKE_PROFIT): sell_reason = "TP"
       elif price <= last_buy*(1-STOP_LOSS): sell_reason = "SL"
   if not sell_reason and bearish_cross(f_prev,s_prev,f_now,s_now):
       sell_reason = "Bearish cross"

   if sell_reason and hold >= ordermin:
       vol = round_qty(hold, lot_dec)
       res = place_market(kpair,"sell",vol)
       print(f"[{alt_pair}] SELL ({sell_reason}) {vol} @ market -> {res}")
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
