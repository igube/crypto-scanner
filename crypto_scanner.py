import websocket
import json
import requests
import time
import threading

def format_price_by_entry(price, entry):

    entry_str = f"{entry:.10f}".rstrip("0")

    if "." in entry_str:
        decimals = len(entry_str.split(".")[1])
    else:
        decimals = 0

    return round(price, decimals)

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

    if price >= 100:
        return round(price, 2)

    elif price >= 1:
        return round(price, 4)

    elif price >= 0.1:
        return round(price, 5)

    elif price >= 0.01:
        return round(price, 6)

    elif price >= 0.001:
        return round(price, 7)

    else:
        return round(price, 8)

BOT_TOKEN = "8780297094:AAEZhRej8tpcuCEIc8pLYtno-_UzhKMTBx8"
CHAT_ID = "-1003936288779"

FREQTRADE_URL = "http://127.0.0.1:8080"
FREQTRADE_USER = "freqtrader"
FREQTRADE_PASS = "freqtrader"

MAX_FREQTRADES = 3

active_trades = {}

orderbook_cache={}
ORDERBOOK_CACHE_TIME=5
CACHE_TIME = 15
ATR_CACHE = 30
SYMBOL_CACHE = 300

ALERT_COOLDOWN = 600
MIN_24H_VOLUME = 1000000

last_prices = {}
last_alert_time = {}

last_global_alert=0
GLOBAL_ALERT_COOLDOWN=300

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
    except:
        pass


# ---------- FREQTRADE ----------

def get_open_trades():

    try:

        r = requests.get(
            f"{FREQTRADE_URL}/api/v1/trades",
            auth=(FREQTRADE_USER, FREQTRADE_PASS),
            timeout=5
        )

        data = r.json()

        trades = data.get("trades", [])

        open_trades = [t for t in trades if t.get("is_open")]

        return len(open_trades)

    except Exception as e:

        print("Trade check error:", e)
        return 0


def open_trade(symbol, tp, sl, direction, leverage, probability):

    if get_open_trades() >= MAX_FREQTRADES:
        print("Max freqtrade trades reached")
        return

    pair = symbol if ":" in symbol else symbol.replace("USDT", "/USDT:USDT")

    side = "long" if direction == "LONG" else "short"

    try:

        r = requests.post(
            f"{FREQTRADE_URL}/api/v1/forceenter",
            json={
                "pair": pair,
                "side": side,
                "ordertype": "limit",
                "price": entry,
                "stakeamount": 100,
                "leverage": leverage
            },
            auth=(FREQTRADE_USER, FREQTRADE_PASS),
            timeout=5
        )

        print("Freqtrade response:", r.text)
        print("HTTP status:", r.status_code)

        if r.status_code != 200:
            print("Trade NOT opened:", pair)
            return

        active_trades[pair] = {
            "tp": tp,
            "sl": sl,
            "direction": direction,
            "leverage": leverage,
            "time": time.time()
        }

        print("Trade opened:", pair)

    except Exception as e:

        print("FREQTRADE error:", e)


def close_trade(pair):

    try:

        requests.post(
            f"{FREQTRADE_URL}/api/v1/force_exit",
            json={"pair": pair},
            auth=(FREQTRADE_USER, FREQTRADE_PASS),
            timeout=5
        )

        print("Trade closed:", pair)

    except Exception as e:

        print("Exit error:", e)


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

    except:
        return []

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
def calculate_leverage(probability):

    if probability < 80:
        return 3

    elif probability < 83:
        return 4

    elif probability < 86:
        return 5

    elif probability < 89:
        return 6

    elif probability < 92:
        return 8

    elif probability < 95:
        return 10

    else:
        return 12

# ---------- TAKE PROFIT ----------
def calculate_tp(probability):

    if probability < 80:
        return 1.0

    elif probability < 83:
        return 1.2

    elif probability < 86:
        return 1.4

    elif probability < 89:
        return 1.6

    else:
        return 1.9

# ---------- LEVELS ----------

def calculate_levels(price, direction, probability, atr):

    tp_percent = calculate_tp(probability)
    sl_percent = tp_percent / 2

    # procentowe odległości
    tp_percent_distance = price * (tp_percent / 100)
    sl_percent_distance = price * (sl_percent / 100)

    # ATR odległości
    atr_tp_distance = atr * 2.4
    atr_sl_distance = atr * 1.2

    # wybór większego dystansu
    tp_distance = max(tp_percent_distance, atr_tp_distance)
    sl_distance = max(sl_percent_distance, atr_sl_distance)

    if direction == "LONG":

        tp = price + tp_distance
        sl = price - sl_distance

    else:

        tp = price - tp_distance
        sl = price + sl_distance

    # minimalna odległość SL (futures safety)
    if abs((price - sl) / price) < 0.004:
        return None, None, None

    return price, tp, sl


# ---------- MONITOR TRADES ----------

def monitor_trades():

    while True:

        try:

            for pair in list(active_trades.keys()):

                trade = active_trades[pair]

                if time.time() - trade["time"] < 15:
                    continue

                data = requests.get(
                    f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={pair.replace('/','')}",
                    timeout=5
                ).json()

                if "bidPrice" not in data:
                    continue

                bid = float(data["bidPrice"])
                ask = float(data["askPrice"])

                tp = trade["tp"]
                sl = trade["sl"]
                direction = trade["direction"]

                # wybór właściwej ceny wykonania
                if direction == "LONG":
                    price = bid
                else:
                    price = ask

                if direction == "LONG":

                    if price >= tp or price <= sl:
                        close_trade(pair)
                        del active_trades[pair]

                else:

                    if price <= tp or price >= sl:
                        close_trade(pair)
                        del active_trades[pair]

        except Exception as e:

            print("Monitor error:", e)

        time.sleep(3)


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
    if wall_bias and wall_bias!=direction:
        return

    leverage = calculate_leverage(probability)

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
        
    now = time.time()
    global last_global_alert
    if symbol not in last_alert_time:
        last_alert_time[symbol] = 0

    if probability >= 78 and now - last_alert_time[symbol] > ALERT_COOLDOWN and now-last_global_alert> GLOBAL_ALERT_COOLDOWN:
        last_alert_time[symbol] = now
        send_alert(f"""
Krypto: {symbol.upper()}
Kierunek: {direction}

Szanse: {probability}%
Dźwignia: {leverage}x

Entry: {format_price(entry)}
TP: {adjust_price(tp,entry,direction)}
SL: {adjust_price(sl,entry,direction)}

""")

        last_global_alert=now

        open_trade(symbol, tp, sl, direction, leverage, probability)

        signal_candidates.append({
            "symbol":symbol,
            "direction":direction,
            "probability":probability,
            "leverage":leverage
        })

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
threading.Thread(target=monitor_trades,daemon=True).start()
threading.Thread(target=heartbeat,daemon=True).start()

while True:
    time.sleep(60)
