"""Discord webhook alerts for top Kalshi weather edges."""

import httpx
from datetime import datetime, timezone
from backend.config import settings

async def send_test_alert(message: str):
    if not settings.discord_webhook_url:
        print("No Discord webhook configured")
        return

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            settings.discord_webhook_url,
            json={"content": message},
        )

        print(f"Discord test alert status: {response.status_code}")

def _pct(x: float) -> str:
    return f"{x:.1%}"


async def send_weather_edge_alerts(signals):
    if not settings.discord_webhook_url:
        return

    actionable = [
        s for s in signals
        if s.market.platform == "kalshi"
        and abs(s.edge) >= settings.discord_min_edge
    ]

    actionable.sort(key=lambda s: abs(s.edge), reverse=True)
    top = actionable[: settings.discord_top_n]

    if not top:
        return

    fields = []

    for i, s in enumerate(top, 1):
        entry_price = s.market.yes_price if s.direction == "yes" else s.market.no_price

        fields.append({
            "name": f"#{i} {s.market.city_name} — BUY {s.direction.upper()}",
            "value": (
                f"**Edge:** {_pct(abs(s.edge))}\n"
                f"**Entry:** {_pct(entry_price)}\n"
                f"**Model YES:** {_pct(s.model_probability)}\n"
                f"**Market YES:** {_pct(s.market_probability)}\n"
                f"**Forecast:** {s.ensemble_mean:.1f}°F ± {s.ensemble_std:.1f}°F\n"
                f"**Market:** `{s.market.market_id}`\n"
                f"{s.market.title}"
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

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(settings.discord_webhook_url, json=payload)
        response.raise_for_status()
