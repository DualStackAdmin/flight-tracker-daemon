"""Aviasales / Travelpayouts Data API client.

Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
Prices are cached from user searches made in the last 48 hours, not live,
so always confirm the price via the link before buying.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.travelpayouts.com"
SITE_URL = "https://www.aviasales.com"


@dataclass(frozen=True)
class Offer:
    origin: str
    destination: str
    departure_at: str           # ISO 8601
    return_at: str | None
    price: float
    currency: str
    airline: str
    flight_number: str
    transfers: int
    return_transfers: int | None
    duration_min: int | None
    link: str                   # absolute URL
    origin_airport: str | None = None
    destination_airport: str | None = None

    @property
    def key(self) -> str:
        """Stable key used to avoid re-sending the same offer."""
        return f"{self.origin}-{self.destination}-{self.departure_at[:10]}-{(self.return_at or '')[:10]}-{self.airline}{self.flight_number}"

    @property
    def stay_days(self) -> int | None:
        if not self.return_at:
            return None
        d1 = datetime.fromisoformat(self.departure_at).date()
        d2 = datetime.fromisoformat(self.return_at).date()
        return (d2 - d1).days


class ApiError(RuntimeError):
    pass


class TravelpayoutsClient:
    def __init__(self, token: str, currency: str = "usd", market: str | None = None,
                 marker: str = "", client: httpx.AsyncClient | None = None,
                 max_retries: int = 3):
        if not token:
            raise ValueError("TRAVELPAYOUTS_TOKEN is empty, check your .env file")
        self.token = token
        self.currency = currency
        self.market = market
        self.marker = marker
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0),
            headers={"X-Access-Token": token, "Accept-Encoding": "gzip, deflate",
                     "User-Agent": "flight-tracker-daemon/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> list[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("currency", self.currency)
        if self.market:
            params.setdefault("market", self.market)
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise ApiError(f"Network error: {e}") from e
                log.warning("Network error (%s), retrying in %ss", e, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                log.warning("HTTP %s, retrying in %ss", resp.status_code, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 401:
                raise ApiError("401: invalid Travelpayouts token")
            resp.raise_for_status()
            body = resp.json()
            if body.get("success") is False:
                raise ApiError(f"API error: {body.get('error')}")
            data = body.get("data") or []
            return data if isinstance(data, list) else list(data.values())
        return []

    def _to_offer(self, d: dict) -> Offer:
        link = d.get("link") or ""
        if link and not link.startswith("http"):
            link = SITE_URL + link
            if self.marker:
                link += ("&" if "?" in link else "?") + f"marker={self.marker}"
        return Offer(
            origin=d.get("origin", ""),
            destination=d.get("destination", ""),
            departure_at=d.get("departure_at", ""),
            return_at=d.get("return_at") or None,
            price=float(d.get("price") or d.get("value") or 0),
            currency=self.currency.upper(),
            airline=d.get("airline", ""),
            flight_number=str(d.get("flight_number", "")),
            transfers=int(d.get("transfers") or 0),
            return_transfers=d.get("return_transfers"),
            duration_min=d.get("duration"),
            link=link,
            origin_airport=d.get("origin_airport") or None,
            destination_airport=d.get("destination_airport") or None,
        )

    async def cheapest_for_month(self, origin: str, destination: str, month: date, *,
                                 round_trip: bool = False, return_month: date | None = None,
                                 direct: bool = False, limit: int = 30) -> list[Offer]:
        """Cheapest tickets for one route in a given month (sorted by price)."""
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": month.strftime("%Y-%m"),
            "return_at": (return_month or month).strftime("%Y-%m") if round_trip else None,
            "one_way": "false" if round_trip else "true",
            "direct": "true" if direct else "false",
            "sorting": "price",
            "limit": limit,
        }
        rows = await self._get("/aviasales/v3/prices_for_dates", params)
        return [self._to_offer(r) for r in rows if r.get("price")]

    async def cheapest_anywhere(self, origin: str, month: date, limit: int = 30) -> list[Offer]:
        """Cheapest tickets from origin to anywhere (one per destination)."""
        params = {
            "origin": origin,
            "departure_at": month.strftime("%Y-%m"),
            "one_way": "true",
            "unique": "true",
            "sorting": "price",
            "limit": limit,
        }
        rows = await self._get("/aviasales/v3/prices_for_dates", params)
        return [self._to_offer(r) for r in rows if r.get("price")]

    async def round_trips_anywhere(self, origin: str, month: date, limit: int = 1000) -> list[Offer]:
        """Round-trip fares from origin to any destination departing in `month` (for /weekend)."""
        params = {
            "origin": origin,
            "departure_at": month.strftime("%Y-%m"),
            "one_way": "false",
            "sorting": "price",
            "limit": limit,
        }
        rows = await self._get("/aviasales/v3/prices_for_dates", params)
        return [self._to_offer(r) for r in rows if r.get("price") and r.get("return_at")]
