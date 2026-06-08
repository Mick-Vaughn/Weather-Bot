import os
import re
import time
import math
import asyncio
import logging
from datetime import datetime, timezone, date
from typing import Dict, List, Optional

import httpx


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")
logger = logging.getLogger("weather_worker")
logging.getLogger("httpx").setLevel(logging.WARNING)


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
WEATHER_CITIES = os.getenv("WEATHER_CITIES", "nyc")
DISCORD_TOP_N = int(os.getenv("DISCORD_TOP_N", "5"))
DISCORD_MIN_EDGE = float(os.getenv("DISCORD_MIN_EDGE", "0.03"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "900"))

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
    "nyc": (40.7851, -73.9683),
    "chicago": (41.7868, -87.7522),
    "miami": (25.7933, -80.2906),
    "austin": (30.1900, -97.6687),
    "phoenix": (33.4278, -112.0039),
    "los_angeles": (33.9381, -118.3889),
    "san_francisco": (37.6196, -122.3656),
    "atlanta": (33.6404, -84.4199),
    "denver": (39.8493, -104.6738),
    "philadelphia": (39.8729, -75.2440),
    "boston": (42.3656, -71.0100),
    "seattle": (47.4447, -122.3136),
    "houston": (29.6456, -95.2789),
    "washington_dc": (38.8513, -77.0360),
    "oklahoma_city": (35.3892, -97.6005),
    "las_vegas": (36.2121, -115.1940),
    "dallas": (32.9187, -97.0590),
    "san_antonio": (29.5337, -98.4698),
    "new_orleans": (29.9934, -90.2647),
    "minneapolis": (44.8800, -93.2217),
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

def parse_kalshi_date_from_ticker(ticker: str) -> Optional[date]:
    """
    Example ticker:
    KXHIGHNY-26JUN07-T87
    Means 2026-JUN-07.
    """
    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker.upper())
    if not match:
        return None

    yy, mon, dd = match.groups()

    month_map = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
        "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
        "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }

    month = month_map.get(mon)
    if month is None:
        return None

    return date(2000 + int(yy), month, int(dd))

async def fetch_forecast_high(client: httpx.AsyncClient,city_key: str,target_date: date,) -> Optional[float]:
    coords = CITY_COORDS.get(city_key)
    if not coords:
        return None

    lat, lon = coords
    headers = {"User-Agent": "kalshi-weather-bot"}

    try:
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        r = await client.get(points_url, headers=headers)
        r.raise_for_status()
        point_data = r.json()

        daily_url = point_data["properties"]["forecast"]
        r = await client.get(daily_url, headers=headers)
        r.raise_for_status()
        forecast_data = r.json()

        periods = forecast_data.get("properties", {}).get("periods", [])

        target_date_str = target_date.isoformat()

        matching_periods = [
            p for p in periods
            if p.get("isDaytime") is True
            and p.get("temperature") is not None
            and p.get("startTime", "").startswith(target_date_str)
        ]

        if not matching_periods:
            logger.warning("No NWS daytime forecast for %s on %s", city_key, target_date_str)
            return None

        forecast_high = float(matching_periods[0]["temperature"])

        logger.info(
            "%s NWS daily forecast high for %s: %.1f°F",
            city_key,
            target_date_str,
            forecast_high,
        )

        return forecast_high

    except Exception as e:
        logger.warning("NWS daily forecast request failed for %s: %s", city_key, e)
        return None

async def fetch_openmeteo_forecast_high(
    client: httpx.AsyncClient,
    city_key: str,
    target_date: date,
) -> Optional[float]:

    coords = CITY_COORDS.get(city_key)
    if not coords:
        return None

    lat, lon = coords

    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
            },
        )

        r.raise_for_status()
        data = r.json()

        highs = data.get("daily", {}).get("temperature_2m_max", [])

        if not highs:
            return None

        forecast_high = float(highs[0])

        logger.info(
            "%s Open-Meteo forecast high for %s: %.1f°F",
            city_key,
            target_date.isoformat(),
            forecast_high,
        )

        return forecast_high

    except Exception as e:
        logger.warning("Open-Meteo request failed for %s on %s: %s", city_key, target_date, e)
        return None

async def fetch_kalshi_forecast(
    client: httpx.AsyncClient,
    series_ticker: str,
    event_ticker: str,
) -> Optional[float]:
    try:
        now = int(time.time())
        
        logger.info("Kalshi forecast lookup: series=%s event=%s",
            series_ticker,
            event_ticker,
        )

        r = await client.get(
            f"https://api.elections.kalshi.com/v1/series/{series_ticker}/events/{event_ticker}/forecast_history",
            params={
                "start_ts": now - 24 * 3600,
                "end_ts": now,
                "period_interval": 1,
            },
        )

        r.raise_for_status()
        data = r.json()

        points = (
            data.get("forecast_history")
            or data.get("history")
            or data.get("data")
            or []
        )

        forecasts = [
            p.get("raw_numerical_forecast")
            for p in points
            if p.get("raw_numerical_forecast") is not None
        ]

        if not forecasts:
            return None

        return float(forecasts[-1])

    except httpx.HTTPStatusError as e:
    logger.warning(
        "Kalshi forecast failed for series=%s event=%s status=%s body=%s",
        series_ticker,
        event_ticker,
        e.response.status_code,
        e.response.text[:500],
    )
        return None

    except Exception as e:
        logger.warning(
            "Kalshi forecast failed for series=%s event=%s: %s",
            series_ticker,
            event_ticker,
            e,
        )
        return None

def get_consensus_forecast(
    nws_high: float,
    openmeteo_high: float,
    kalshi_forecast: Optional[float] = None,
    max_source_spread: float = 3.0,
    max_kalshi_gap: float = 3.0,
):
    forecast_high = (nws_high + openmeteo_high) / 2
    source_spread = abs(nws_high - openmeteo_high)

    forecast_warning = source_spread > max_source_spread

    kalshi_gap = None
    kalshi_warning = False

    if kalshi_forecast is not None:
        kalshi_gap = abs(kalshi_forecast - forecast_high)
        kalshi_warning = kalshi_gap > max_kalshi_gap

    return forecast_high, forecast_warning, source_spread, kalshi_warning, kalshi_gap

async def fetch_orderbook_prices(client: httpx.AsyncClient, ticker: str):
    url = f"{KALSHI_URL}/{ticker}/orderbook"

    try:
        r = await client.get(url, params={"depth": 1})
        r.raise_for_status()
        data = r.json()

        ob = data.get("orderbook", {}) or data.get("orderbook_fp", {})

        yes_levels = ob.get("yes") or ob.get("yes_dollars") or []
        no_levels = ob.get("no") or ob.get("no_dollars") or []

        yes_bid = None
        no_bid = None

        if yes_levels:
            yes_bid = float(yes_levels[-1][0])

        if no_levels:
            no_bid = float(no_levels[-1][0])

        # Convert dollar prices like 0.83 to cents if needed
        if yes_bid is not None and yes_bid <= 1:
            yes_bid *= 100
        if no_bid is not None and no_bid <= 1:
            no_bid *= 100

        yes_ask = 100 - no_bid if no_bid is not None else None
        no_ask = 100 - yes_bid if yes_bid is not None else None

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
        }

    except Exception as e:
        logger.warning(f"ORDERBOOK FAILED {ticker}: {e}")
        return {}

def evaluate_market(
    city_key: str,
    market: Dict,
    forecast_high: float,
    prices: Dict,
    forecast_warning: bool,
    spread: float,
    nws_high: float,
    openmeteo_high: float,
) -> Optional[Dict]:
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

# Skip markets where the threshold is too far from the forecast
#    if abs(threshold - forecast_high) > 8:
#        return None

# Skip low-volume / dead markets
    volume = float(market.get("volume", 0) or 0)
  #  if volume < 100:
  #      return None

    yes_bid = prices.get("yes_bid")
    yes_ask = prices.get("yes_ask")
    no_bid = prices.get("no_bid")
    no_ask = prices.get("no_ask")

    if yes_ask is None:
        yes_ask = market.get("yes_ask")

    if no_ask is None:
        no_ask = market.get("no_ask")

    if yes_bid is None:
        yes_bid = market.get("yes_bid")

    if no_bid is None:
        no_bid = market.get("no_bid")

    if yes_ask is None and no_bid is not None:
        yes_ask = 100 - float(no_bid)

    if no_ask is None and yes_bid is not None:
        no_ask = 100 - float(yes_bid)

    if yes_ask is None:
        return None

    if no_ask is None:
        no_ask = 100 - float(yes_ask)

    yes_price = float(yes_ask) / 100
    no_price = float(no_ask) / 100

    market_yes = float(yes_ask) / 100

   # if yes_price <= 0.02 or yes_price >= 0.98:
   #     return None

    sigma = 3.0

    def cdf(temp):
        return normal_cdf((temp - forecast_high) / sigma)

    if bucket["type"] == "below":
        display_high = bucket["high"] - 1
        model_yes = cdf(bucket["high"] - 0.5)
        threshold_display = f"{display_high:.0f}°F or below"

    elif bucket["type"] == "above":
        display_low = bucket["low"] + 1
        model_yes = 1 - cdf(bucket["low"] + 0.5)
        threshold_display = f"{display_low:.0f}°F or above"

    else:
        low = bucket["low"]
        high = bucket["high"]
        model_yes = cdf(high + 0.5) - cdf(low - 0.5)
        threshold_display = f"{low:.0f}–{high:.0f}°F"

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
        "market_yes": market_yes,
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

    unique = {}
    for o in opportunities:
        unique[o["ticker"]] = o

    opportunities = list(unique.values())
    
    top = sorted(opportunities, key=lambda x: x["edge"], reverse=True)[:DISCORD_TOP_N]

    fields = []
    for i, o in enumerate(top, 1):
        
        warning_text = ""

        if o.get("forecast_warning"):
            warning_text = (
                "\n⚠️ **FORECAST DISAGREEMENT**\n"
                f"NWS: {o['nws_high']:.1f}°F\n"
                f"Open-Meteo: {o['openmeteo_high']:.1f}°F\n"
                f"Spread: {o['forecast_spread']:.1f}°F\n"
            )
            
        alert_prefix = "⚠️ " if (
            o.get("forecast_warning")
            or o.get("kalshi_warning")
        ) else ""

        if o.get("kalshi_warning"):
            warning_text += (
                "\n⚠️ **KALSHI FORECAST GAP**\n"
                f"Kalshi Forecast: {o['kalshi_forecast']:.1f}°F\n"
                f"Bot Forecast: {o['forecast_high']:.1f}°F\n"
                f"Gap: {o['kalshi_gap']:.1f}°F\n"
            )
        
        title = f"{alert_prefix}{o['city']} — {o['title']}"

        kalshi_text = ""

        if o.get("kalshi_forecast") is not None:
            kalshi_text = f"**Kalshi Forecast:** {o['kalshi_forecast']:.1f}°F\n"
            
        source_text = ""

        if o.get("nws_high") is not None:
            source_text += f"**NWS:** {o['nws_high']:.1f}°F\n"

        if o.get("openmeteo_high") is not None:
            source_text += f"**Open-Meteo:** {o['openmeteo_high']:.1f}°F\n"

        
        
        fields.append({
            "name": title,
            "value": (
                f"**Side:** {o['side']}\n"
                f"**EV:** {o['edge']:+.1%}\n"
                f"**Entry:** {o['price']:.1%}\n"
                f"**Model:** {o['model_yes']:.1%}\n"
                f"**Market YES:** {o['market_yes']:.1%}\n"
                f"**Forecast:** {o['forecast_high']:.1f}°F\n"
                f"{source_text}"
                f"{kalshi_text}"
                f"{warning_text}"
                f"**Ticker:** {o['ticker']}"
            ),
            "inline": False,
        })

    payload = {
        "username": "Kalshi Weather Scanner",
        "embeds": [{
            "title": f"Top {len(top)} Kalshi Weather Edges",
            "description": "Alert-only mode. No trades placed.",
            "color": 5763719,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json=payload)
        logger.info("Discord status: %s", r.status_code)
        
        if r.status_code != 204:
            logger.error("Discord response: %s", r.text)

async def scan_once() -> None:
    cities = selected_cities()
    logger.info("Scanning cities: %s", ",".join(cities))

    opportunities = []
    forecast_cache = {}
    kalshi_forecast_cache = {}

    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for city_key in cities:
            logger.info("Scanning %s", city_key)

            markets = await fetch_kalshi_city_markets(client, city_key)

            for market in markets:
                ticker = market.get("ticker", "")

                target_date = parse_kalshi_date_from_ticker(ticker)
                if target_date is None:
                    logger.warning("Could not parse date from ticker %s", ticker)
                    continue

                cache_key = (city_key, target_date)

                if cache_key in forecast_cache:
                    nws_high, openmeteo_high = forecast_cache[cache_key]
                else:
                    nws_high = await fetch_forecast_high(client, city_key, target_date)
                    if nws_high is None:
                        continue

                    await asyncio.sleep(1)

                    openmeteo_high = await fetch_openmeteo_forecast_high(client, city_key, target_date)
                    if openmeteo_high is None:
                        continue

                    forecast_cache[cache_key] = (nws_high, openmeteo_high)

                logger.info(
                    "%s forecasts for %s -> NWS: %.1f°F Open-Meteo: %.1f°F",
                    city_key,
                    target_date.isoformat(),
                    nws_high,
                    openmeteo_high,
                )
                
                event_ticker = market.get("event_ticker") or "-".join(ticker.split("-")[:2])
                series_ticker = market.get("series_ticker") or event_ticker.split("-")[0]

                kalshi_cache_key = (series_ticker, event_ticker)

                if kalshi_cache_key in kalshi_forecast_cache:
                    kalshi_forecast = kalshi_forecast_cache[kalshi_cache_key]
                else:
                    kalshi_forecast = await fetch_kalshi_forecast(
                        client,
                        series_ticker,
                        event_ticker,
                    )
                    kalshi_forecast_cache[kalshi_cache_key] = kalshi_forecast
                    
                forecast_high, forecast_warning, spread, kalshi_warning, kalshi_gap = get_consensus_forecast(
                    nws_high,
                    openmeteo_high,
                    kalshi_forecast,
                )

                prices = await fetch_orderbook_prices(client, ticker)

                opp = evaluate_market(
                    city_key,
                    market,
                    forecast_high,
                    prices,
                    forecast_warning,
                    spread,
                    nws_high,
                    openmeteo_high,
                )

                if opp:
                    opp["forecast_warning"] = forecast_warning
                    opp["forecast_spread"] = spread
                    opp["nws_high"] = nws_high
                    opp["openmeteo_high"] = openmeteo_high
                    opp["kalshi_forecast"] = kalshi_forecast
                    opp["kalshi_warning"] = kalshi_warning
                    opp["kalshi_gap"] = kalshi_gap

                    opportunities.append(opp)
                    
                await asyncio.sleep(8)

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
        "market_yes": 1.0,
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
