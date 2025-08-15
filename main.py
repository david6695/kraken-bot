import os, time, json, hmac, hashlib, base64, requests
from urllib.parse import urlencode

# -------- CONFIG FROM SECRETS (set these in GitHub later) --------
LIVE = os.getenv("LIVE", "false").lower() == "true"
PAIRS = os.getenv("PAIRS", "BTCUSD,ETHUSD,ADAUSD").split(",")
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "10"))
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")  # base64 string from Kraken

# Correct Kraken API base URL
BASE = "https://api.kraken.com"

def kraken_request(path, data=None, private=False):
   data = data or {}
   if not private:
       r = requests.get(BASE + path, params=data, timeout=15)
       r.raise_for_status()
       return r.json()

   # Private endpoint signing
   nonce = str(int(time.time() * 1000))
   data["nonce"] = nonce
   postdata = urlencode(data)
   message = (nonce + postdata).encode()
   sha256 = hashlib.sha256(message).digest()
   mac = hmac.new(base64.b64decode(API_SECRET), (path.encode() + sha256), hashlib.sha512)
   sig = base64.b64encode(mac.digest())

   headers = {
       "API-Key": API_KEY,
       "API-Sign": sig.decode()
   }
   r = requests.post(BASE + path, headers=headers, data=data, timeout=20)
   r.raise_for_status()
   return r.json()

def get_last_price(pair):
   sym_map = {"BTCUSD":"XBTUSD", "ETHUSD":"ETHUSD", "ADAUSD":"ADAUSD", "XRPUSD":"XRPUSD"}
   kpair = sym_map.get(pair, pair)
   res = kraken_request("/0/public/Ticker", {"pair": kpair})
   key = list(res["result"].keys())[0]
   return float(res["result"][key]["c"][0]), kpair

def place_market_buy(kpair, usd_amount):
   price, _ = get_last_price(kpair)
   volume = round(usd_amount / price, 6)
   payload = {
       "ordertype": "market",
       "type": "buy",
       "volume": str(volume),
       "pair": kpair
   }
   if LIVE:
       out = kraken_request("/0/private/AddOrder", payload, private=True)
       return out
   else:
       return {"simulated": True, "pair": kpair, "volume": volume, "usd": usd_amount}

def simple_logic(pair):
   price, kpair = get_last_price(pair)
   print(f"[{pair}] price = {price}")
   result = place_market_buy(kpair, USD_PER_TRADE)
   print(f"[{pair}] order result: {result}")
   return result

def main():
   if LIVE and (not API_KEY or not API_SECRET):
       raise RuntimeError("LIVE=True but KRAKEN_API_KEY/SECRET missing")
   print(json.dumps({
       "live": LIVE, "pairs": PAIRS, "usd_per_trade": USD_PER_TRADE
   }))
   for p in PAIRS:
       try:
           simple_logic(p.strip())
           time.sleep(1)
       except Exception as e:
           print(f"[{p}] ERROR: {e}")

if __name__ == "__main__":
   main()
