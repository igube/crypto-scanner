import websocket
import json
import requests
import time
import threading


def adjust_price(price, entry, direction):

    entry_str = f"{entry:.10f}".rstrip("0")

    if "." in entry_str:
        decimals = len(entry_str.split(".")[1])
    else:
        decimals = 0

    tick = 10 ** (-decimals)

    rounded = round(price, decimals)

    if rounded == entry:

        if direction == "LONG":
            rounded = entry + tick
        else:
            rounded = entry - tick

    return round(rounded, decimals)

def format_price(price):
    return f"{price:.10f}".rstrip("0").rstrip(".")
    

BOT_TOKEN = "8780297094:AAHcRVQBPog5l1Qn4P2_XJLLMD8u8CGaSds"
CHAT_ID = "-1003936288779"


orderbook_cache={}
ORDERBOOK_CACHE_TIME=5
CACHE_TIME = 15
ATR_CACHE = 30
SYMBOL_CACHE = 300

ALERT_COOLDOWN = 600
MIN_24H_VOLUME = 1000000

last_prices = {}
last_alert_time = {}
last_signal = {}
last_alert_global = 0
GLOBAL_ALERT_COOLDOWN = 600
last_signal_time = {}
DUPLICATE_SIGNAL_COOLDOWN = 1800 

cache_orderbook = {}
cache_volume = {}
cache_atr = {}

symbol_cache = {"time": 0, "symbols": []}

signal_candidates = []


# ---------- TELEGRAM ----------

def send_alert(msg):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)
    except Exception as e:
        print("TELEGRAM ERROR:", e, flush=True)


# ---------- SYMBOLS ----------

def get_symbols():

    now = time.time()

    if now - symbol_cache["time"] < SYMBOL_CACHE:
        return symbol_cache["symbols"]

    try:

        data = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=10
        ).json()

        info = requests.get(
            "https://api.binance.com/api/v3/exchangeInfo",
            timeout=10
        ).json()

    except Exception as e:
        print("SYMBOL ERROR:", e, flush=True)
        return symbol_cache["symbols"] # fallback

    valid = set()

    for s in info["symbols"]:
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT":
            valid.add(s["symbol"])

    symbols = []

    for s in data:

        symbol = s["symbol"]

        if symbol in valid:

            volume = float(s["quoteVolume"])

            if volume > MIN_24H_VOLUME:
                symbols.append(symbol.lower())

    symbol_cache["time"] = now
    symbol_cache["symbols"] = symbols

    return symbols


# ---------- ORDERBOOK ----------

def check_order_book(symbol):

    now = time.time()

    if symbol in cache_orderbook:
        if now - cache_orderbook[symbol]["time"] < CACHE_TIME:
            return cache_orderbook[symbol]["value"]

    try:

        data = requests.get(
            f"https://api.binance.com/api/v3/depth?symbol={symbol.upper()}&limit=50",
            timeout=10
        ).json()

    except:
        return 0,0,0,0

    bids = sum(float(b[1]) for b in data["bids"])
    asks = sum(float(a[1]) for a in data["asks"])

    support = float(data["bids"][0][0])
    resistance = float(data["asks"][0][0])

    if asks == 0:
        return 0,0,support,resistance

    value = (bids/asks, asks/bids)

    cache_orderbook[symbol] = {"value":value,"time":now}

    return value[0],value[1],support,resistance


def analyze_orderbook(symbol):

    now = time.time()

    if symbol in orderbook_cache:
        if now - orderbook_cache[symbol]["time"] < ORDERBOOK_CACHE_TIME:
            return orderbook_cache[symbol]["value"]

    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/depth?symbol={symbol.upper()}&limit=100",
            timeout=5
        ).json()

    except:
        return 1, None

    bids = [(float(p), float(q)) for p, q in data["bids"]]
    asks = [(float(p), float(q)) for p, q in data["asks"]]

    bid_volume = sum(q for _, q in bids[:20])
    ask_volume = sum(q for _, q in asks[:20])

    if ask_volume == 0:
        return 1, None

    imbalance = bid_volume / ask_volume

    largest_bid_wall = max(q for _, q in bids[:20])
    largest_ask_wall = max(q for _, q in asks[:20])

    wall_bias = None

    if largest_bid_wall > largest_ask_wall * 1.5:
        wall_bias = "LONG"

    elif largest_ask_wall > largest_bid_wall * 1.5:
        wall_bias = "SHORT"

    orderbook_cache[symbol] = {
        "value": (imbalance, wall_bias),
        "time": now
    }

    return imbalance, wall_bias


# ---------- VOLUME ----------

def check_volume(symbol):

    now=time.time()

    if symbol in cache_volume:
        if now-cache_volume[symbol]["time"]<CACHE_TIME:
            return cache_volume[symbol]["value"]

    try:

        data=requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval=1m&limit=10",
            timeout=10
        ).json()

    except:
        return 1

    volumes=[float(x[5]) for x in data]

    avg=sum(volumes[:-1])/len(volumes[:-1])

    value=volumes[-1]/avg

    cache_volume[symbol]={"value":value,"time":now}

    return value


# ---------- ATR ----------

def get_atr(symbol):

    now=time.time()

    if symbol in cache_atr:
        if now-cache_atr[symbol]["time"]<ATR_CACHE:
            return cache_atr[symbol]["value"]

    try:

        data=requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval=1m&limit=50",
            timeout=10
        ).json()

    except:
        return None

    trs=[float(c[2])-float(c[3]) for c in data]

    atr=sum(trs)/len(trs)

    if atr<=0:
        return None

    cache_atr[symbol]={"value":atr,"time":now}

    return atr


# ---------- TREND ----------

def get_trend(symbol):

    try:

        data=requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval=5m&limit=3",
            timeout=10
        ).json()

    except:
        return 0

    open_price=float(data[0][1])
    close_price=float(data[-1][4])

    return ((close_price-open_price)/open_price)*100


# ---------- SIGNAL PROBABILITY ----------

def signal_probability(change, volume, buy_ratio, sell_ratio, trend, atr):

    # MOMENTUM SCORE (0-30)
    momentum = min(abs(change) / 1.2, 1) * 30

    # VOLUME SCORE (0-25)
    volume_score = min((volume - 1) / 1.5, 1) * 25

    # volatility score
    volatility=min((atr/0.01),1)*10

    # ORDERBOOK IMBALANCE (0-25)
    pressure = max(buy_ratio, sell_ratio)
    orderflow = min((pressure - 1) / 0.8, 1) * 25

    # TREND ALIGNMENT (0-20)
    trend_score = 0
    if trend > 0 and buy_ratio > sell_ratio:
        trend_score = 20
    elif trend < 0 and sell_ratio > buy_ratio:
        trend_score = 20

    probability = momentum + volume_score + orderflow + trend_score + volatility

    return round(min(probability, 95))

# ---------- LEVERAGE ----------
def calculate_leverage(probability, atr, price):
    atr_percent = (atr / price) * 100

    base_lev = 3 + ((probability - 80) / 20) * 7
    base_lev = max(3, min(base_lev, 10))

    volatility_penalty = atr_percent * 1.5

    leverage = base_lev - volatility_penalty

    return round(max(2, min(leverage, 10)))

# ---------- TAKE PROFIT / STOP LOSS ----------
def calculate_rr(probability):
    min_rr = 1.5
    max_rr = 3.0

    rr = min_rr + ((probability - 80) / 20) * (max_rr - min_rr)

    return round(max(min_rr, min(rr, max_rr)), 2)

# ---------- LEVELS ----------

def calculate_levels(price, direction, probability, atr):

    rr = calculate_rr(probability)

    sl_distance = atr * 1.5
    tp_distance = sl_distance * rr

    if direction == "LONG":
        tp = price + tp_distance
        sl = price - sl_distance
    else:
        tp = price - tp_distance
        sl = price + sl_distance

    if abs((price - sl) / price) < 0.003:
        return None, None, None

    return price, tp, sl


# ---------- WHALE STREAM ----------

def on_trade(ws,message):

    data=json.loads(message)

    symbol=data["s"]

    price=float(data["p"])
    qty=float(data["q"])

    value=price*qty

    if value>350000:

        side="SELL" if data["m"] else "BUY"

        send_alert(f"""
🐋 Whale trade

Pair: {symbol}
Side: {side}
Value: ${value:,.0f}
Price: {price}
""")


def start_trade_stream():

    while True:

        try:

            symbols = get_symbols()
            print("SYMBOLS:", len(symbols), flush=True)
            if not symbols:
                print("NO SYMBOLS - retry...", flush=True)
                time.sleep(5)
                continue
            
            ws=websocket.WebSocketApp(
                "wss://stream.binance.com:9443/ws/!trade@arr",
                on_message=on_trade
            )

            ws.run_forever()

        except Exception as e:

            print("Trade stream error:",e)

        time.sleep(5)


# ---------- HEARTBEAT ----------

def heartbeat():

    while True:

        print("Bot running:",time.strftime("%H:%M:%S"))

        time.sleep(120)


# ---------- MARKET STREAM ----------

def on_message(ws,message):

    data=json.loads(message)

    symbol=data["s"].lower()
    price=float(data["c"])

    if symbol not in last_prices:

        last_prices[symbol]=price
        return

    change=((price-last_prices[symbol])/last_prices[symbol])*100
    if abs(change)<0.15:
        return

    buy_ratio,sell_ratio,support,resistance=check_order_book(symbol)

    imbalance,wall_bias=analyze_orderbook(symbol)
    if imbalance<1.1 and max(buy_ratio,sell_ratio)<1.4:
        return

    volume_ratio=check_volume(symbol)

    if volume_ratio < 1.6:
        return

    atr=get_atr(symbol)
    if atr is None:
        return
    if atr/price<0.002:
        return

    trend=get_trend(symbol)

    probability = signal_probability(
        change,
        volume_ratio,
        buy_ratio,
        sell_ratio,
        trend,
        atr
    )

    if imbalance > 1.3:
        probability+=5


    direction = "LONG" if buy_ratio > sell_ratio else "SHORT"

    now = time.time()
    
    if wall_bias and wall_bias!=direction:
        return

    if (
        symbol in last_signal
        and last_signal[symbol] == direction
        and now - last_signal_time.get(symbol, 0) < DUPLICATE_SIGNAL_COOLDOWN
    ):
        return

    leverage = calculate_leverage(probability, atr, price)

    if direction == "LONG" and trend < 0:
        return

    if direction == "SHORT" and trend > 0:
        return

    entry, tp, sl = None, None, None

    try:
        entry, tp, sl = calculate_levels(price, direction, probability, atr)
    except Exception as e:
        print("CALCULATE LEVELS ERROR:", e, flush=True)
        return
    
    if entry is None:
        return
        
    
    global last_alert_global
    if symbol not in last_alert_time:
        last_alert_time[symbol] = 0
        
    title = "🔥 TOP SIGNAL 🔥" if probability >= 90 else "🚨 SIGNAL 🚨"
    print(f"{title}: {symbol} {probability}", flush=True)
    
    if (
        probability >= 85
        and now - last_alert_time[symbol] > ALERT_COOLDOWN
        and now - last_alert_global > GLOBAL_ALERT_COOLDOWN
    ):
        
        direction_emoji = "📈" if direction == "LONG" else "📉"
        
        send_alert(f"""
{title}

🪙 Crypto: *{symbol.upper()}*
{direction_emoji} Direction: *{direction}*

🎯 Probability: {probability}%
⚡ Leverage: {leverage}x

💰 Entry: {format_price(entry)}
🎯 TP: {format_price(adjust_price(tp,entry,direction))}
🛑 SL: {format_price(adjust_price(sl,entry,direction))}

""")
        
        last_alert_time[symbol] = now
        last_alert_global = now
        last_signal[symbol] = direction
        last_signal_time[symbol] = now

# ---------- START SCANNER ----------

def start_scanner():

    while True:

        try:

            ws=websocket.WebSocketApp(
                "wss://stream.binance.com:9443/ws",
                on_message=on_message
            )

            ws.on_open=lambda ws: ws.send(json.dumps({
                "method":"SUBSCRIBE",
                "params":[f"{s}@ticker" for s in get_symbols()],
                "id":1
            }))

            ws.run_forever()

        except Exception as e:

            print("Websocket error:",e)

        time.sleep(5)


# ---------- THREADS ----------

threading.Thread(target=start_scanner,daemon=True).start()
threading.Thread(target=start_trade_stream,daemon=True).start()
threading.Thread(target=heartbeat,daemon=True).start()

while True:
    time.sleep(60)
