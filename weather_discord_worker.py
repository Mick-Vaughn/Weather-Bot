import os
import re
import time
import math
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")
logger = logging.getLogger("weather_worker")
logging.getLogger("httpx").setLevel(logging.WARNING)


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
WEATHER_CITIES = os.getenv("WEATHER_CITIES", "nyc")
DISCORD_TOP_N = int(os.getenv("DISCORD_TOP_N", "5"))
DISCORD_MIN_EDGE = float(os.getenv("DISCORD_MIN_EDGE", "0.00"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"


CITY_SERIES = {
    "nyc": "KXHIGHNY",
    "chicago": "KXHIGHCHI",
    "miami": "KXHIGHMIA",
    "austin": "KXHIGHAUS",
    "phoenix": "KXHIGHPHX",
    "los_angeles": "KXHIGHLAX",
    "san_francisco": "KXHIGHSFO",
    "atlanta": "KXHIGHATL",
    "denver": "KXHIGHDEN",
    "philadelphia": "KXHIGHPHIL",
    "boston": "KXHIGHBOS",
    "seattle": "KXHIGHSEA",
    "houston": "KXHIGHHOU",
    "washington_dc": "KXHIGHDC",
    "oklahoma_city": "KXHIGHOKC",
    "las_vegas": "KXHIGHLV",
    "dallas": "KXHIGHDAL",
    "san_antonio": "KXHIGHSAT",
    "new_orleans": "KXHIGHMSY",
    "minneapolis": "KXHIGHMSP",
}

CITY_NAMES = {
    "nyc": "New York City",
    "chicago": "Chicago",
    "miami": "Miami",
    "austin": "Austin",
    "phoenix": "Phoenix",
    "los_angeles": "Los Angeles",
    "san_francisco": "San Francisco",
    "atlanta": "Atlanta",
    "denver": "Denver",
    "philadelphia": "Philadelphia",
    "boston": "Boston",
    "seattle": "Seattle",
    "houston": "Houston",
    "washington_dc": "Washington DC",
    "oklahoma_city": "Oklahoma City",
    "las_vegas": "Las Vegas",
    "dallas": "Dallas",
    "san_antonio": "San Antonio",
    "new_orleans": "New Orleans",
    "minneapolis": "Minneapolis",
}

CITY_COORDS = {
    "nyc": (40.7128, -74.0060),
    "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918),
    "austin": (30.2672, -97.7431),
    "phoenix": (33.4484, -112.0740),
    "los_angeles": (34.0522, -118.2437),
    "san_francisco": (37.7749, -122.4194),
    "atlanta": (33.7490, -84.3880),
    "denver": (39.7392, -104.9903),
    "philadelphia": (39.9526, -75.1652),
    "boston": (42.3601, -71.0589),
    "seattle": (47.6062, -122.3321),
    "houston": (29.7604, -95.3698),
    "washington_dc": (38.9072, -77.0369),
    "oklahoma_city": (35.4676, -97.5164),
    "las_vegas": (36.1699, -115.1398),
    "dallas": (32.7767, -96.7970),
    "san_antonio": (29.4241, -98.4936),
    "new_orleans": (29.9511, -90.0715),
    "minneapolis": (44.9778, -93.2650),
}


def selected_cities() -> List[str]:
    return [c.strip() for c in WEATHER_CITIES.split(",") if c.strip()]


def extract_threshold(title: str) -> Optional[int]:
    match = re.search(r"(\d{2,3})\s?(?:°|F|degrees)?", title, re.I)
    return int(match.group(1)) if match else None


def is_high_market(title: str) -> bool:
    text = title.lower()
    return "high" in text or "maximum" in text or "highest" in text


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def parse_temp_bucket(title: str):
    text = title.lower()

    # Only parse the part before "on Jun..." so dates don't get picked up
    text = text.split(" on ")[0]

    nums = [float(x) for x in re.findall(r"\d{2,3}(?:\.\d+)?", text)]

    if not nums:
        return None

    if (
        "below" in text
        or "or less" in text
        or "under" in text
        or "<" in text
    ):
        return {"type": "below", "low": None, "high": nums[0]}

    if (
        "above" in text
        or "or higher" in text
        or "over" in text
        or ">" in text
    ):
        return {"type": "above", "low": nums[0], "high": None}

    if len(nums) >= 2:
        return {"type": "range", "low": min(nums[0], nums[1]), "high": max(nums[0], nums[1])}

    return {"type": "above", "low": nums[0], "high": None}

async def fetch_kalshi_city_markets(client: httpx.AsyncClient, city_key: str) -> List[Dict]:
    series = CITY_SERIES.get(city_key)
    if not series:
        logger.warning("No Kalshi series configured for %s", city_key)
        return []

    params = {"series_ticker": series, "status": "open", "limit": 200}

    try:
        r = await client.get(KALSHI_URL, params=params)
        r.raise_for_status()
        data = r.json()
        markets = data.get("markets", [])
        logger.info("Kalshi %s: %s raw markets", city_key, len(markets))
        return markets
    except Exception as e:
        logger.warning("Kalshi request failed for %s (%s): %s", city_key, series, e)
        return []


async def fetch_forecast_high(client: httpx.AsyncClient, city_key: str) -> Optional[float]:
    coords = CITY_COORDS.get(city_key)
    if not coords:
        return None

    lat, lon = coords

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "forecast_days": 1,
    }

    try:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        temps = data.get("hourly", {}).get("temperature_2m", [])
        if not temps:
            return None
        return max(float(t) for t in temps)
    except Exception as e:
        logger.warning("Forecast request failed for %s: %s", city_key, e)
        return None


def evaluate_market(city_key: str, market: Dict, forecast_high: float) -> Optional[Dict]:
    title = market.get("title") or market.get("subtitle") or market.get("ticker", "")
    ticker = market.get("ticker", "")

    if not is_high_market(title):
        return None

    bucket = parse_temp_bucket(title)
    if bucket is None:
        return None
        
    if bucket["type"] == "below":
        distance = abs(bucket["high"] - forecast_high)
    elif bucket["type"] == "above":
        distance = abs(bucket["low"] - forecast_high)
    else:
        distance = min(abs(bucket["low"] - forecast_high), abs(bucket["high"] - forecast_high))

    if distance > 8:
        return None
    
    logger.info(f"DEBUG BUCKET: {title} -> {bucket}")
# Skip markets where the threshold is too far from the forecast
#    if abs(threshold - forecast_high) > 8:
#        return None

# Skip low-volume / dead markets
    volume = float(market.get("volume", 0) or 0)
  #  if volume < 100:
  #      return None

    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")

    if yes_ask is None:
        yes_ask = market.get("last_price", 50)
    if no_ask is None:
        no_ask = 100 - yes_ask

    yes_price = float(yes_ask) / 100
    no_price = float(no_ask) / 100

   # if yes_price <= 0.02 or yes_price >= 0.98:
   #     return None

    sigma = 3.0

    def cdf(temp):
        return normal_cdf((temp - forecast_high) / sigma)

    if bucket["type"] == "below":
        model_yes = cdf(bucket["high"])
        threshold_display = f"{bucket['high']}°F or below"

    elif bucket["type"] == "above":
        model_yes = 1 - cdf(bucket["low"])
        threshold_display = f"{bucket['low']}°F or above"

    else:
        low = bucket["low"]
        high = bucket["high"]
        model_yes = cdf(high) - cdf(low)
        threshold_display = f"{low}–{high}°F"

    model_yes = max(0.01, min(0.99, model_yes))
    model_no = 1 - model_yes

# ignore tiny differences between model and market
    if abs(model_yes - yes_price) < DISCORD_MIN_EDGE:
        return None
    
    yes_edge = model_yes - yes_price
    no_edge = model_no - no_price

    if yes_edge >= DISCORD_MIN_EDGE:
        side = "YES"
        edge = yes_edge
        price = yes_price
    elif no_edge >= DISCORD_MIN_EDGE:
        side = "NO"
        edge = no_edge
        price = no_price
    else:
        return None

    return {
        "city": CITY_NAMES.get(city_key, city_key),
        "ticker": ticker,
        "title": title,
        "side": side,
        "edge": edge,
        "price": price,
        "model_yes": model_yes,
        "forecast_high": forecast_high,
        "threshold": threshold_display,
    }


async def send_discord_alert(opportunities: List[Dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not configured")
        return

    if not opportunities:
        logger.info("No opportunities to send")
        return

    top = sorted(opportunities, key=lambda x: x["edge"], reverse=True)[:DISCORD_TOP_N]

    fields = []
    for i, o in enumerate(top, 1):
        fields.append({
            "name": f"#{i} {o['city']} — BUY {o['side']}",
            "value": (
                f"**Edge:** {o['edge']:+.1%}\n"
                f"**Ask:** {o['price']:.1%}\n"
                f"**Model YES:** {o['model_yes']:.1%}\n"
                f"**Forecast High:** {o['forecast_high']:.1f}°F\n"
                f"**Threshold:** {o['threshold']}°F\n"
                f"**Ticker:** `{o['ticker']}`\n"
                f"{o['title']}"
            ),
            "inline": False,
        })

    payload = {
        "username": "Kalshi Weather Scanner",
        "embeds": [{
            "title": f"Top {len(top)} Kalshi Weather Edges",
            "description": "Alert-only mode. No trades placed.",
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json=payload)
        logger.info("Discord status: %s", r.status_code)


async def scan_once() -> None:
    cities = selected_cities()
    logger.info("Scanning cities: %s", ",".join(cities))

    opportunities = []

    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for city_key in cities:
            logger.info("Scanning %s", city_key)

            forecast_high = await fetch_forecast_high(client, city_key)
            if forecast_high is None:
                continue

            logger.info("%s forecast high: %.1f°F", city_key, forecast_high)

            markets = await fetch_kalshi_city_markets(client, city_key)

            for market in markets:
                opp = evaluate_market(city_key, market, forecast_high)
                if opp:
                    opportunities.append(opp)

    logger.info("Found %s opportunities", len(opportunities))
    await send_discord_alert(opportunities)


async def main() -> None:
    logger.info("Starting clean Kalshi weather Discord worker")
    logger.info("Cities: %s", WEATHER_CITIES)
    logger.info("Top N: %s | Min edge: %.1f%% | Interval: %ss", DISCORD_TOP_N, DISCORD_MIN_EDGE * 100, CHECK_INTERVAL_SECONDS)

    await send_discord_alert([{
        "city": "Test",
        "ticker": "TEST",
        "title": "Worker started successfully",
        "side": "YES",
        "edge": 0.99,
        "price": 0.01,
        "model_yes": 1.0,
        "forecast_high": 0,
        "threshold": 0,
    }])

    while True:
        try:
            await scan_once()
        except Exception as e:
            logger.exception("Scan failed: %s", e)

        logger.info("Sleeping %s seconds", CHECK_INTERVAL_SECONDS)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
