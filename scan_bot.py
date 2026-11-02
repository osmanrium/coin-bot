"""
BingX coklu coin LONG/SHORT sinyal botu (1s mum + 4s teyit).
- Sadece PUBLIC veri okur, API anahtari yok, islem ACAMAZ.
- Hacme gore en likit coinleri tarar, RSI + hacim + alim/satim baskisina gore sinyal uretir.
- Her saatlik mum kapanisindan sonra bir kez calisacak sekilde tasarlandi (GitHub Actions).

Ortam degiskenleri: TG_TOKEN, TG_CHAT_ID  (TEST=1 verirsen test mesaji atar)
"""
import os
import time
import requests

BASE = "https://open-api.bingx.com"
TOP_N = 30                    # 24s hacme gore ilk N coin
MIN_QUOTE_VOL = 20_000_000    # 24s minimum hacim (USDT)
MAX_SIGNALS = 10

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


def api(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def klines(symbol, interval, limit=100):
    d = api(
        "/openApi/swap/v3/quote/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    d.sort(key=lambda k: int(k["time"]))
    return [
        {
            "o": float(k["open"]),
            "h": float(k["high"]),
            "l": float(k["low"]),
            "c": float(k["close"]),
            "v": float(k["volume"]),
        }
        for k in d
    ]


def sma(values, n):
    return sum(values[-n:]) / n


def rsi(closes, period=14):
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(k, period=14):
    trs = []
    for prev, cur in zip(k, k[1:]):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(trs[-period:]) / period


def buy_share(k, n=10):
    """Alim baskisi tahmini: hacmi, kapanisin mum icindeki konumuna gore boler.
    (BingX taker alim/satim hacmini vermedigi icin yaklasik bir olcudur.)"""
    buy = total = 0.0
    for x in k[-n:]:
        rng = x["h"] - x["l"]
        frac = (x["c"] - x["l"]) / rng if rng > 0 else 0.5
        buy += x["v"] * frac
        total += x["v"]
    return buy / total if total else 0.5


def fmt(p):
    return f"{p:.4f}" if p < 10 else f"{p:.2f}"


def analyze(symbol):
    k1 = klines(symbol, "1h")
    closed = k1[:-1]  # son (olusan) mumu at
    if len(closed) < 60:
        return None
    closes = [x["c"] for x in closed]
    last = closed[-1]
    price = last["c"]
    r = rsi(closes)
    r_prev = rsi(closes[:-1])
    ma30 = sma(closes, 30)
    a = atr(closed)
    vol_ratio = last["v"] / sma([x["v"] for x in closed[:-1]], 20)
    bs = buy_share(closed)

    k4 = klines(symbol, "4h")[:-1]
    r4 = rsi([x["c"] for x in k4]) if len(k4) > 30 else 50

    green = last["c"] > last["o"]
    red = last["c"] < last["o"]
    side = kind = None

    if r_prev < 32 and r > r_prev and green and vol_ratio >= 1.2 and bs >= 0.55 and r4 < 60:
        side, kind = "LONG", "asiri satim donusu"
    elif price > ma30 and 50 <= r <= 65 and r > r_prev and green and vol_ratio >= 1.3 and bs >= 0.58 and r4 < 75:
        side, kind = "LONG", "trend devami"
    elif r_prev > 68 and r < r_prev and red and vol_ratio >= 1.2 and bs <= 0.45 and r4 > 40:
        side, kind = "SHORT", "asiri alim reddi"
    elif price < ma30 and 35 <= r <= 50 and r < r_prev and red and vol_ratio >= 1.3 and bs <= 0.42 and r4 > 25:
        side, kind = "SHORT", "trend devami"

    if not side:
        return None

    risk = 1.5 * a
    if side == "LONG":
        stop, tp1, tp2 = price - risk, price + 1.5 * risk, price + 2.5 * risk
    else:
        stop, tp1, tp2 = price + risk, price - 1.5 * risk, price - 2.5 * risk

    text = (
        f"{side} | {symbol} ({kind})\n"
        f"Giris: {fmt(price)}\n"
        f"Stop: {fmt(stop)}\n"
        f"Hedef 1: {fmt(tp1)} | Hedef 2: {fmt(tp2)}\n"
        f"RSI 1s: {r:.0f} | RSI 4s: {r4:.0f}\n"
        f"Hacim: {vol_ratio:.1f}x ortalama | Alim baskisi: %{bs * 100:.0f}"
    )
    return {"score": vol_ratio, "text": text}


def send(text):
    print(text)
    if TG_TOKEN and TG_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )


def main():
    if os.environ.get("TEST") == "1":
        send("Test mesaji: bot Telegram'a baglandi.")
        return

    tickers = api("/openApi/swap/v2/quote/ticker")
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("-USDT"):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv >= MIN_QUOTE_VOL:
            rows.append((qv, sym))
    rows.sort(reverse=True)
    symbols = [s for _, s in rows[:TOP_N]]
    print(f"Taranan coin sayisi: {len(symbols)}")

    signals = []
    for s in symbols:
        try:
            res = analyze(s)
            if res:
                signals.append(res)
        except Exception as e:
            print(f"{s} hata: {e}")
        time.sleep(0.15)

    if not signals:
        print("Sinyal yok.")
        return

    signals.sort(key=lambda x: x["score"], reverse=True)
    body = "\n\n".join(x["text"] for x in signals[:MAX_SIGNALS])
    send(f"SINYALLER ({len(signals)})\n\n{body}\n\nYatirim tavsiyesi degildir. Stop kullan.")


if __name__ == "__main__":
    main()
