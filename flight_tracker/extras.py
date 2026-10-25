"""Extra context added to alerts: cheaper nearby departure airports, visa hint,
public-holiday "vacation days needed" and typical weather.

Every part is optional and fails quietly: if data is missing or a request fails,
the alert is still sent, just without that line.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import yaml

from .locations import Locations
from .travelpayouts import ApiError, Offer, TravelpayoutsClient

log = logging.getLogger(__name__)

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["", "January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]


# ---------------------------------------------------------------- visa
class Visa:
    ICONS = {
        "free": "🟢 Visa-free for {p} passport: book and go",
        "evisa": "🟡 Easy visa for {p} passport (eVisa / on arrival)",
        "hard": "🔴 Visa needed in advance for {p} passport: don't book without one",
    }

    def __init__(self, path: Path | None, passport: str | None):
        self.passport = (passport or "").upper()
        self.levels: dict[str, str] = {}
        self.name = self.passport
        if path and path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self.name = raw.get("passport_name") or self.passport
            for level in ("free", "evisa", "hard"):
                for cc in raw.get(level) or []:
                    self.levels[str(cc).upper()] = level

    @property
    def enabled(self) -> bool:
        return bool(self.levels)

    def line(self, country_code: str | None) -> str | None:
        if not self.enabled or not country_code or country_code.upper() == self.passport:
            return None
        level = self.levels.get(country_code.upper())
        if not level:
            return "❔ Visa: check the requirements for your passport"
        return self.ICONS[level].format(p=self.name)


# ---------------------------------------------------------------- holidays
class Holidays:
    def __init__(self, path: Path | None):
        self.days: dict[date, str] = {}
        if not path or not path.exists():
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for k, name in (raw.get("holidays") or {}).items():
            d = k if isinstance(k, date) else date.fromisoformat(str(k))
            self.days[d] = str(name)
        if raw.get("weekend_carry_over", True):
            self._carry_over()

    def _carry_over(self) -> None:
        """A holiday on Sat/Sun gives the next working day off."""
        extra: dict[date, str] = {}
        for d, name in sorted(self.days.items()):
            if d.weekday() >= 5:
                nxt = d + timedelta(days=1)
                while nxt.weekday() >= 5 or nxt in self.days or nxt in extra:
                    nxt += timedelta(days=1)
                extra[nxt] = f"{name} (moved)"
        self.days.update(extra)

    @property
    def enabled(self) -> bool:
        return bool(self.days)

    def is_day_off(self, d: date) -> bool:
        return d.weekday() >= 5 or d in self.days

    def leave_needed(self, departure_at: str, return_at: str) -> tuple[int, list[str]]:
        """Working days you must take off, and the holidays the trip covers.
        The departure day counts as a working day if you leave before 15:00;
        the return day counts if the flight back leaves before 18:00."""
        dep, ret = datetime.fromisoformat(departure_at), datetime.fromisoformat(return_at)
        needed, names = 0, []
        d = dep.date()
        while d <= ret.date():
            if d in self.days:
                base = self.days[d].replace(" (moved)", "")
                if base not in names:
                    names.append(base)
            elif d.weekday() < 5:
                if d == dep.date():
                    needed += dep.hour < 15
                elif d == ret.date():
                    needed += ret.hour < 18
                else:
                    needed += 1
            d += timedelta(days=1)
        return needed, names

    def line(self, offer: Offer) -> str | None:
        if not self.enabled or not offer.return_at:
            return None
        try:
            needed, names = self.leave_needed(offer.departure_at, offer.return_at)
        except ValueError:
            return None
        hol = " / ".join(names[:2])
        if names and needed == 0:
            return f"🌟 No vacation days needed: covers {hol}"
        if names and needed <= 2:
            return f"🌟 Only {needed} vacation day{'s' if needed > 1 else ''} needed thanks to {hol}"
        if not names and needed == 0:
            return "🌟 No vacation days needed (weekend trip)"
        return None

    def upcoming(self, today: date, limit: int = 12) -> list[tuple[date, str]]:
        return [(d, n) for d, n in sorted(self.days.items()) if d >= today][:limit]


# ---------------------------------------------------------------- climate
CLIMATE_URL = "https://archive-api.open-meteo.com/v1/archive"


def temp_icon(day_max: float) -> str:
    if day_max <= 0:
        return "🥶"
    if day_max <= 12:
        return "🧥"
    if day_max <= 25:
        return "🌤"
    if day_max <= 32:
        return "☀️"
    return "🔥"


class Climate:
    """Average day/night temperature of a month, from last year's real weather
    (Open-Meteo historical API, free for non-commercial use). Cached forever."""

    def __init__(self, cache_path: Path, enabled: bool = True,
                 client: httpx.AsyncClient | None = None):
        self.enabled = enabled
        self.cache_path = cache_path
        self._client = client
        try:
            self.cache: dict[str, list] = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    @staticmethod
    def reference_year(month: int, today: date) -> int:
        """Most recent year in which `month` is fully in the past."""
        return today.year if month < today.month else today.year - 1

    async def month_avg(self, lat: float, lon: float, month: int,
                        today: date | None = None) -> tuple[float, float] | None:
        today = today or date.today()
        key = f"{lat:.1f},{lon:.1f},{month}"
        if key in self.cache:
            v = self.cache[key]
            return (v[0], v[1]) if v else None
        year = self.reference_year(month, today)
        start = date(year, month, 1)
        end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        try:
            r = await client.get(CLIMATE_URL, params={
                "latitude": round(lat, 2), "longitude": round(lon, 2),
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min", "timezone": "auto"})
            r.raise_for_status()
            daily = r.json().get("daily") or {}
            highs = [t for t in daily.get("temperature_2m_max") or [] if t is not None]
            lows = [t for t in daily.get("temperature_2m_min") or [] if t is not None]
            result = (round(sum(highs) / len(highs)), round(sum(lows) / len(lows))) if highs and lows else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Climate lookup failed (%s)", e)
            return None                    # not cached: try again next time
        finally:
            if self._client is None:
                await client.aclose()
        self.cache[key] = list(result) if result else []
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        except OSError:
            pass
        return result

    async def line(self, offer: Offer, locations: Locations | None) -> str | None:
        if not self.enabled or not locations:
            return None
        coords = locations.coords(offer.destination_airport or offer.destination)
        if not coords:
            return None
        try:
            month = datetime.fromisoformat(offer.departure_at).month
        except ValueError:
            return None
        avg = await self.month_avg(*coords, month)
        if not avg:
            return None
        hi, lo = avg
        city = locations.city(offer.destination)[0]
        return f"{temp_icon(hi)} {MONTHS_LONG[month]} in {city}: about {hi:+d}°C day / {lo:+d}°C night"


# ---------------------------------------------------------------- nearby airports
@dataclass
class AltOrigin:
    code: str
    note: str = ""


class AltOrigins:
    """Check the same destination and dates from nearby airports."""

    def __init__(self, api: TravelpayoutsClient, flex_days: int = 2, min_saving_percent: float = 25):
        self.api = api
        self.flex_days = flex_days
        self.min_saving_percent = min_saving_percent

    async def best(self, offer: Offer, alt: AltOrigin) -> Offer | None:
        if alt.code in (offer.origin, offer.destination):
            return None
        dep = datetime.fromisoformat(offer.departure_at)
        ret = datetime.fromisoformat(offer.return_at) if offer.return_at else None
        try:
            offers = await self.api.cheapest_for_month(
                alt.code, offer.destination, date(dep.year, dep.month, 1),
                round_trip=ret is not None,
                return_month=date(ret.year, ret.month, 1) if ret else None)
        except ApiError as e:
            log.warning("Alt origin %s: %s", alt.code, e)
            return None
        best = None
        for o in offers:
            try:
                odep = datetime.fromisoformat(o.departure_at)
                if abs((odep.date() - dep.date()).days) > self.flex_days:
                    continue
                if ret:
                    if not o.return_at:
                        continue
                    oret = datetime.fromisoformat(o.return_at)
                    if abs((oret.date() - ret.date()).days) > self.flex_days:
                        continue
            except ValueError:
                continue
            if best is None or o.price < best.price:
                best = o
        if best and best.price <= offer.price * (1 - self.min_saving_percent / 100):
            return best
        return None

    async def lines(self, offer: Offer, alts: list[AltOrigin], locations: Locations | None) -> list[str]:
        out = []
        for alt in alts:
            o = await self.best(offer, alt)
            if not o:
                continue
            name = locations.short(alt.code) if locations else alt.code
            d = datetime.fromisoformat(o.departure_at)
            saving = offer.price - o.price
            note = f" ({html.escape(alt.note)})" if alt.note else ""
            link = f' <a href="{html.escape(o.link, quote=True)}">↗</a>' if o.link else ""
            out.append(f"💡 From {html.escape(name)}: <b>{o.price:g} {html.escape(o.currency)}</b> "
                       f"on {d.day} {MONTHS[d.month]} (save {saving:g}){note}. Worth the trip?{link}")
        return out


# ---------------------------------------------------------------- all together
EXTRAS_DIR = Path(__file__).resolve().parent.parent / "extras"


class Enricher:
    """Builds the extra alert lines. Visa and holiday files are picked by country:
    'auto' means the country of the origin city (e.g. BAK -> AZ -> visa_AZ.yaml)."""

    def __init__(self, *, locations: Locations | None, origin: str = "",
                 passport: str = "auto", holidays_country: str = "auto",
                 climate: Climate | None = None, alt: AltOrigins | None = None,
                 alt_origins=lambda: [], extras_dir: Path = EXTRAS_DIR):
        self.locations = locations
        self.origin = origin
        self.passport_setting = (passport or "auto").strip()
        self.holidays_setting = (holidays_country or "auto").strip()
        self.climate = climate
        self.alt = alt
        self.alt_origins = alt_origins          # callable -> list[AltOrigin] (live setting)
        self.extras_dir = Path(extras_dir)
        self.visa: Visa | None = None
        self.holidays: Holidays | None = None

    def _country(self, setting: str) -> str | None:
        if setting.lower() in ("off", "none", "false", ""):
            return None
        if setting.lower() != "auto":
            return setting.upper()
        return self.locations.country_code(self.origin) if self.locations else None

    def resolve(self) -> None:
        """Load visa/holiday files once the origin country is known."""
        if self.visa is None:
            cc = self._country(self.passport_setting)
            if cc:
                self.visa = Visa(self.extras_dir / f"visa_{cc}.yaml", cc)
                log.info("Visa hints: %s", "visa_%s.yaml" % cc if self.visa.enabled else "no file for " + cc)
        if self.holidays is None:
            cc = self._country(self.holidays_setting)
            if cc:
                self.holidays = Holidays(self.extras_dir / f"holidays_{cc}.yaml")
                log.info("Holidays: %s", "holidays_%s.yaml" % cc if self.holidays.enabled else "no file for " + cc)

    async def lines(self, offer: Offer) -> list[str]:
        out: list[str] = []
        try:
            self.resolve()
            if self.alt:
                alts = self.alt_origins()
                if alts:
                    out += await self.alt.lines(offer, alts, self.locations)
            if self.visa and self.locations:
                v = self.visa.line(self.locations.country_code(offer.destination))
                if v:
                    out.append(html.escape(v, quote=False))
            if self.holidays:
                h = self.holidays.line(offer)
                if h:
                    out.append(html.escape(h, quote=False))
            if self.climate:
                c = await asyncio.wait_for(self.climate.line(offer, self.locations), timeout=25)
                if c:
                    out.append(html.escape(c, quote=False))
        except Exception as e:  # noqa: BLE001 - extras must never block an alert
            log.warning("Extra info failed: %s", e)
        return out
