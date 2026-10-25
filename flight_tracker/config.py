"""Configuration: config.yaml + environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Route:
    destination: str
    max_price: float | None = None      # alert when price is at or below this
    round_trip: bool = False
    min_days: int = 3                   # min stay for round trips
    max_days: int = 14
    direct_only: bool = False
    both: bool = False                  # search one way AND round trip
    rt_max_price: float | None = None   # target for the round-trip part when both=True

    @property
    def kind(self) -> str:
        return "rt" if self.round_trip else "ow"

    @property
    def trip(self) -> str:
        return "both" if self.both else self.kind


@dataclass
class Anywhere:
    enabled: bool = False
    max_price: float | None = None      # target for one-way fares
    limit: int = 30
    trip: str = "ow"                    # ow | rt | both
    min_days: int = 3                   # stay length for round trips
    max_days: int = 14
    rt_max_price: float | None = None   # target for round-trip fares


@dataclass
class Alerts:
    drop_percent: float = 10.0          # alert when price drops this % below the 30-day low
    cooldown_hours: float = 12.0        # min time between alerts for the same route


@dataclass
class Config:
    origin: str = "BAK"
    currency: str = "usd"
    market: str | None = None
    months_ahead: int = 3
    scan_interval_minutes: int = 60
    digest_hour: int | None = 9         # local hour for the daily digest (null = off)
    timezone: str = "Asia/Baku"
    routes: list[Route] = field(default_factory=list)
    anywhere: Anywhere = field(default_factory=Anywhere)
    alerts: Alerts = field(default_factory=Alerts)
    db_path: str = "data/prices.db"
    passengers: int = 1                 # prices are per person; totals shown for N people
    passport: str = "auto"              # visa hints: country code, "auto" (origin country) or "off"
    holidays: str = "auto"              # public holidays: country code, "auto" or "off"
    climate: bool = True                # typical weather line (Open-Meteo)
    alt_origins: list[dict] = field(default_factory=list)   # [{code: TBS, note: "..."}]
    alt_flex_days: int = 2              # +/- days when comparing nearby airports
    alt_min_saving_percent: float = 25  # only mention a nearby airport if this much cheaper

    # from .env
    travelpayouts_token: str = ""
    travelpayouts_marker: str = ""
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (no extra dependency).

    Later lines win over earlier ones. Variables already set to a non-empty
    value in the real environment are never overridden.
    """
    if not path.exists():
        return
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Config:
    config_path = Path(config_path)
    _load_dotenv(Path(env_path))

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    routes = []
    for r in raw.get("routes") or []:
        routes.append(Route(
            destination=str(r["destination"]).upper(),
            max_price=r.get("max_price"),
            round_trip=bool(r.get("round_trip", False)),
            min_days=int(r.get("min_days", 3)),
            max_days=int(r.get("max_days", 14)),
            direct_only=bool(r.get("direct_only", False)),
            both=bool(r.get("both", False)),
            rt_max_price=r.get("rt_max_price"),
        ))

    cfg = Config(
        origin=str(raw.get("origin", "BAK")).upper(),
        currency=str(raw.get("currency", "usd")).lower(),
        market=raw.get("market"),
        months_ahead=int(raw.get("months_ahead", 3)),
        scan_interval_minutes=int(raw.get("scan_interval_minutes", 60)),
        digest_hour=raw.get("digest_hour", 9),
        timezone=raw.get("timezone", "Asia/Baku"),
        routes=routes,
        anywhere=Anywhere(**(raw.get("anywhere") or {})),
        alerts=Alerts(**(raw.get("alerts") or {})),
        db_path=raw.get("db_path", "data/prices.db"),
        passengers=int(raw.get("passengers", 1) or 1),
        passport=str(raw.get("passport", "auto")),
        holidays=str(raw.get("holidays", "auto")),
        climate=bool(raw.get("climate", True)),
        alt_origins=[a if isinstance(a, dict) else {"code": str(a)} for a in raw.get("alt_origins") or []],
        alt_flex_days=int(raw.get("alt_flex_days", 2)),
        alt_min_saving_percent=float(raw.get("alt_min_saving_percent", 25)),
        travelpayouts_token=os.environ.get("TRAVELPAYOUTS_TOKEN", ""),
        travelpayouts_marker=os.environ.get("TRAVELPAYOUTS_MARKER", ""),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_ids=[
            int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x
        ],
    )
    if cfg.scan_interval_minutes < 10:
        raise ValueError("scan_interval_minutes must be at least 10 (be nice to the API)")
    return cfg
