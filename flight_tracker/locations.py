"""City, airport and country names for IATA codes.

Uses the free Travelpayouts data files (no token needed):
  https://api.travelpayouts.com/data/en/cities.json
  https://api.travelpayouts.com/data/en/airports.json
  https://api.travelpayouts.com/data/en/countries.json

They are downloaded once, compacted into data/locations.json and refreshed
weekly. If the download fails, the stale cache is used; with no cache at all,
messages simply fall back to raw IATA codes, so alerts never break.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DATA_URL = "https://api.travelpayouts.com/data/en/{}.json"
MAX_AGE = 7 * 86400
CACHE_VERSION = 2          # v2 adds city coordinates (for the climate line)


def _en(item: dict) -> str:
    return (item.get("name_translations") or {}).get("en") or item.get("name") or ""


def flag(country_code: str | None) -> str:
    """'AZ' -> 🇦🇿"""
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country_code.upper())


class Locations:
    def __init__(self, cache_path: str | Path, client: httpx.AsyncClient | None = None):
        self.cache_path = Path(cache_path)
        self._client = client
        self.cities: dict[str, list] = {}      # code -> [name, country_code, lat, lon]
        self.airports: dict[str, list] = {}    # code -> [name, city_code, country_code]
        self.countries: dict[str, str] = {}    # code -> name
        self.loaded_at: float = 0.0

    # ---------- loading ----------
    def _read_cache(self) -> bool:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if raw.get("version") != CACHE_VERSION:
                return False                       # old format: download again
            self.cities, self.airports, self.countries = raw["cities"], raw["airports"], raw["countries"]
            self.loaded_at = raw.get("saved_at", 0.0)
            return True
        except (OSError, ValueError, KeyError):
            return False

    async def _download(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        try:
            data = {}
            for name in ("cities", "airports", "countries"):
                r = await client.get(DATA_URL.format(name))
                r.raise_for_status()
                data[name] = r.json()
        finally:
            if self._client is None:
                await client.aclose()

        def coords(c: dict) -> list:
            co = c.get("coordinates") or {}
            return [co.get("lat"), co.get("lon")]

        self.cities = {c["code"]: [_en(c), c.get("country_code"), *coords(c)]
                       for c in data["cities"] if c.get("code")}
        self.airports = {a["code"]: [_en(a), a.get("city_code"), a.get("country_code")]
                         for a in data["airports"] if a.get("code")}
        self.countries = {c["code"]: _en(c) for c in data["countries"] if c.get("code")}
        self.loaded_at = time.time()

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "saved_at": self.loaded_at, "cities": self.cities,
                                   "airports": self.airports, "countries": self.countries},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_path)
        log.info("Location names updated: %d cities, %d airports, %d countries",
                 len(self.cities), len(self.airports), len(self.countries))

    async def ensure_fresh(self) -> None:
        """Load from cache; download if missing or older than a week. Never raises."""
        if not self.cities:
            self._read_cache()
        if self.cities and time.time() - self.loaded_at < MAX_AGE:
            return
        try:
            await self._download()
        except Exception as e:  # noqa: BLE001 - names are nice-to-have, never fatal
            log.warning("Could not download location names (%s); %s", e,
                        "using cached copy" if self.cities else "showing IATA codes")

    # ---------- lookups ----------
    def city(self, code: str | None) -> tuple[str, str | None]:
        """IATA city or airport code -> (city name, country code)."""
        if not code:
            return "", None
        if code in self.cities:
            name, cc = self.cities[code][:2]
            return name or code, cc
        if code in self.airports:
            _, city_code, cc = self.airports[code]
            if city_code in self.cities:
                return self.cities[city_code][0] or code, cc
        return code, None

    def coords(self, code: str | None) -> tuple[float, float] | None:
        """City (or airport's city) coordinates, if known."""
        if code in self.airports:
            code = self.airports[code][1] or code
        c = self.cities.get(code or "")
        if c and len(c) >= 4 and c[2] is not None and c[3] is not None:
            return float(c[2]), float(c[3])
        return None

    def country_code(self, code: str | None) -> str | None:
        return self.city(code)[1]

    def country(self, cc: str | None) -> str:
        return self.countries.get(cc or "", cc or "")

    def place(self, code: str | None) -> str:
        """'IST' -> '🇹🇷 Istanbul, Turkiye'  (falls back to the code)."""
        name, cc = self.city(code)
        if not cc:
            return name
        return f"{flag(cc)} {name}, {self.country(cc)}".strip()

    def short(self, code: str | None) -> str:
        """'IST' -> 'Istanbul 🇹🇷'  (compact form for lists)."""
        name, cc = self.city(code)
        return f"{name} {flag(cc)}".strip() if cc else name

    def airport(self, code: str | None) -> str:
        """'SAW' -> 'Sabiha Gokcen International Airport (SAW)'."""
        if not code:
            return ""
        if code in self.airports and self.airports[code][0]:
            return f"{self.airports[code][0]} ({code})"
        return code
