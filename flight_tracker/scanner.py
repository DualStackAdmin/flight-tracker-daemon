"""One scan cycle: check all routes, store history, send alerts when needed."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from .alerts import decide, format_offer, is_weekend_trip, offer_buttons
from .config import Config, Route
from .storage import Storage
from .travelpayouts import ApiError, Offer, TravelpayoutsClient

log = logging.getLogger(__name__)


def month_starts(today: date, n: int) -> list[date]:
    out, y, m = [], today.year, today.month
    for _ in range(max(1, n)):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


@dataclass
class ScanResult:
    checked: int = 0
    alerts: int = 0
    errors: list[str] = field(default_factory=list)
    best: dict[str, Offer] = field(default_factory=dict)


class Scanner:
    def __init__(self, cfg: Config, api: TravelpayoutsClient, storage: Storage,
                 notify, request_pause: float = 0.5, locations=None, settings=None):
        self.cfg = cfg
        self.api = api
        self.storage = storage
        self.notify = notify            # async def notify(text: str, buttons=None)
        self.settings = settings        # optional live Settings (Telegram-managed)
        self.locations = locations      # optional Locations for city/country names
        self.pause = request_pause
        self._lock = asyncio.Lock()
        self.last_run: float | None = None
        self.last_result: ScanResult | None = None

    # ---------- live settings (fall back to config.yaml) ----------
    def _routes(self) -> list[Route]:
        routes = self.settings.routes() if self.settings else list(self.cfg.routes)
        muted = self._muted()
        return [r for r in routes if r.destination not in muted]

    def _muted(self) -> set[str]:
        return self.settings.muted() if self.settings else set()

    def _anywhere_max(self) -> float | None:
        return self.settings.anywhere_max_price if self.settings else self.cfg.anywhere.max_price

    def _passengers(self) -> int:
        return self.settings.passengers if self.settings else max(1, self.cfg.passengers)

    async def _route_offers(self, route: Route, months: list[date], today: date) -> list[Offer]:
        offers: list[Offer] = []
        for m in months:
            return_months = [m, _next_month(m)] if route.round_trip else [None]
            for rm in return_months:
                offers += await self.api.cheapest_for_month(
                    self.cfg.origin, route.destination, m, round_trip=route.round_trip,
                    return_month=rm, direct=route.direct_only)
                await asyncio.sleep(self.pause)
        result = []
        for o in offers:
            if datetime.fromisoformat(o.departure_at).date() < today:
                continue
            if route.direct_only and o.transfers > 0:
                continue
            if route.round_trip:
                if o.stay_days is None or not (route.min_days <= o.stay_days <= route.max_days):
                    continue
            result.append(o)
        return result

    async def _handle(self, route_key: str, offer: Offer, max_price: float | None,
                      res: ScanResult, now: float) -> None:
        recent = self.storage.recent_min(route_key, days=30, now=now)
        last = self.storage.last_alert(route_key)
        d = decide(offer, max_price=max_price, recent_min=recent,
                   drop_percent=self.cfg.alerts.drop_percent,
                   last_alert=dict(last) if last else None,
                   cooldown_hours=self.cfg.alerts.cooldown_hours, now=now)
        self.storage.record(route_key, offer, now=now)
        res.best[route_key] = offer
        if d.send:
            text = format_offer(offer, d.reasons, self.locations,
                                passengers=self._passengers(), with_link=False)
            await self.notify(text, offer_buttons(offer, route_key))
            self.storage.mark_alert(route_key, offer, now=now)
            res.alerts += 1

    async def run_once(self, today: date | None = None) -> ScanResult:
        async with self._lock:
            today = today or date.today()
            if self.locations:
                await self.locations.ensure_fresh()
            now = time.time()
            months = month_starts(today, self.cfg.months_ahead)
            res = ScanResult()

            routes = self._routes()
            muted = self._muted()
            for route in routes:
                key = f"{self.cfg.origin}-{route.destination}-{route.kind}"
                try:
                    offers = await self._route_offers(route, months, today)
                except ApiError as e:
                    res.errors.append(f"{key}: {e}")
                    log.error("%s: %s", key, e)
                    continue
                res.checked += 1
                if offers:
                    cheapest = min(offers, key=lambda o: o.price)
                    await self._handle(key, cheapest, route.max_price, res, now)
                else:
                    log.info("%s: no offers found", key)

            if self.cfg.anywhere.enabled:
                explicit = {r.destination for r in routes} | muted
                by_dest: dict[str, Offer] = {}
                for m in months:
                    try:
                        offers = await self.api.cheapest_anywhere(
                            self.cfg.origin, m, limit=self.cfg.anywhere.limit)
                    except ApiError as e:
                        res.errors.append(f"anywhere {m:%Y-%m}: {e}")
                        continue
                    await asyncio.sleep(self.pause)
                    for o in offers:
                        if o.destination in explicit:
                            continue
                        if datetime.fromisoformat(o.departure_at).date() < today:
                            continue
                        if o.destination not in by_dest or o.price < by_dest[o.destination].price:
                            by_dest[o.destination] = o
                for dest, o in by_dest.items():
                    res.checked += 1
                    await self._handle(f"{self.cfg.origin}-{dest}-any", o,
                                       self._anywhere_max(), res, now)

            self.last_run = now
            self.last_result = res
            log.info("Scan finished: %d routes, %d alerts, %d errors",
                     res.checked, res.alerts, len(res.errors))
            return res

    async def weekend_deals(self, today: date | None = None, depart_after_hour: int = 15,
                            max_price: float | None = None, top: int = 10) -> list[Offer]:
        """Cheapest Friday-evening -> Sunday round trips, one per destination."""
        today = today or date.today()
        muted = self._muted()
        best: dict[str, Offer] = {}
        for m in month_starts(today, self.cfg.months_ahead):
            try:
                offers = await self.api.round_trips_anywhere(self.cfg.origin, m)
            except ApiError as e:
                log.error("weekend %s: %s", m, e)
                continue
            await asyncio.sleep(self.pause)
            for o in offers:
                if o.destination in muted or not is_weekend_trip(o, depart_after_hour):
                    continue
                if datetime.fromisoformat(o.departure_at).date() < today:
                    continue
                if max_price is not None and o.price > max_price:
                    continue
                if o.destination not in best or o.price < best[o.destination].price:
                    best[o.destination] = o
        return sorted(best.values(), key=lambda o: o.price)[:top]
