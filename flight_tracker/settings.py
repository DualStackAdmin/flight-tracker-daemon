"""Live settings changed from Telegram, stored in SQLite.

config.yaml stays the baseline. Changes made with /addroute, /removeroute,
/setlimit, /family and /mute are stored in the database and applied on top of it,
so they survive restarts and need no SSH.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict

from .config import Config, Route
from .storage import Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS dyn_routes (destination TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removed_routes (destination TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS muted (code TEXT PRIMARY KEY, muted_at REAL NOT NULL);
"""

IATA = re.compile(r"^[A-Z]{3}$")


def valid_code(code: str) -> bool:
    return bool(IATA.match(code or ""))


class Settings:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.db = storage.db
        self.storage = storage
        self.db.executescript(SCHEMA)

    # ---------- routes ----------
    def routes(self) -> list[Route]:
        """config.yaml routes, minus removed ones, plus/overridden by Telegram changes."""
        removed = {r[0] for r in self.db.execute("SELECT destination FROM removed_routes")}
        out = {r.destination: r for r in self.cfg.routes if r.destination not in removed}
        for dest, data in self.db.execute("SELECT destination, data FROM dyn_routes"):
            out[dest] = Route(**json.loads(data))
        return list(out.values())

    def get_route(self, dest: str) -> Route | None:
        return next((r for r in self.routes() if r.destination == dest), None)

    def add_route(self, route: Route) -> None:
        self.db.execute("INSERT OR REPLACE INTO dyn_routes(destination, data) VALUES (?, ?)",
                        (route.destination, json.dumps(asdict(route))))
        self.db.execute("DELETE FROM removed_routes WHERE destination=?", (route.destination,))
        self.db.commit()

    def remove_route(self, dest: str) -> bool:
        existed = self.get_route(dest) is not None
        self.db.execute("DELETE FROM dyn_routes WHERE destination=?", (dest,))
        if any(r.destination == dest for r in self.cfg.routes):
            self.db.execute("INSERT OR IGNORE INTO removed_routes(destination) VALUES (?)", (dest,))
        self.db.commit()
        return existed

    def set_route_trip(self, dest: str, mode: str, stay: tuple[int, int] | None = None,
                       rt_price: float | None = None) -> bool:
        """Switch a route between one way ('ow'), round trip ('rt') and both."""
        route = self.get_route(dest)
        if route is None:
            return False
        route.round_trip = mode == "rt"
        route.both = mode == "both"
        if stay:
            route.min_days, route.max_days = stay
        if rt_price is not None:
            route.rt_max_price = rt_price
        self.add_route(route)
        return True

    def set_route_limit(self, dest: str, price: float | None) -> bool:
        route = self.get_route(dest)
        if route is None:
            return False
        route.max_price = price
        self.add_route(route)
        return True

    # ---------- anywhere limit ----------
    @property
    def anywhere_max_price(self) -> float | None:
        v = self.storage.get_meta("anywhere_max_price")
        if v is None:
            return self.cfg.anywhere.max_price
        return float(v) if v else None

    @anywhere_max_price.setter
    def anywhere_max_price(self, value: float | None) -> None:
        self.storage.set_meta("anywhere_max_price", "" if value is None else str(value))

    # ---------- anywhere trip type ----------
    @property
    def anywhere_trip(self) -> str:
        return self.storage.get_meta("anywhere_trip") or self.cfg.anywhere.trip or "ow"

    @property
    def anywhere_stay(self) -> tuple[int, int]:
        v = self.storage.get_meta("anywhere_stay")
        if v:
            lo, hi = v.split("-")
            return int(lo), int(hi)
        return self.cfg.anywhere.min_days, self.cfg.anywhere.max_days

    @property
    def anywhere_rt_max_price(self) -> float | None:
        v = self.storage.get_meta("anywhere_rt_max_price")
        if v:
            return float(v)
        if self.cfg.anywhere.rt_max_price is not None:
            return self.cfg.anywhere.rt_max_price
        return rt_default(self.anywhere_max_price)

    def set_anywhere_trip(self, trip: str, stay: tuple[int, int] | None = None,
                          rt_price: float | None = None) -> None:
        self.storage.set_meta("anywhere_trip", trip)
        if stay:
            self.storage.set_meta("anywhere_stay", f"{stay[0]}-{stay[1]}")
        if rt_price is not None:
            self.storage.set_meta("anywhere_rt_max_price", str(rt_price))

    # ---------- passengers ----------
    @property
    def passengers(self) -> int:
        v = self.storage.get_meta("passengers")
        return int(v) if v else max(1, int(self.cfg.passengers))

    @passengers.setter
    def passengers(self, n: int) -> None:
        self.storage.set_meta("passengers", str(int(n)))

    # ---------- mute list ----------
    def mute(self, code: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO muted(code, muted_at) VALUES (?, ?)", (code, time.time()))
        self.db.commit()

    def unmute(self, code: str) -> bool:
        cur = self.db.execute("DELETE FROM muted WHERE code=?", (code,))
        self.db.commit()
        return cur.rowcount > 0

    def muted(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT code FROM muted")}


def rt_default(one_way_price: float | None) -> float | None:
    """Round-trip target when none is set: twice the one-way target."""
    return one_way_price * 2 if one_way_price else None


def route_rt_target(route: Route) -> float | None:
    if route.rt_max_price is not None:
        return route.rt_max_price
    return route.max_price if route.round_trip else rt_default(route.max_price)


def describe_trip(mode: str, lo: int, hi: int) -> str:
    return {"ow": "one way", "rt": f"round trip {lo}–{hi} days",
            "both": f"one way + round trip {lo}–{hi} days"}[mode]


ADD_USAGE = "Usage: /addroute PAR [max_price] [rt 4-10 | both 4-10] [direct]"


def parse_addroute(args: list[str]) -> Route:
    """/addroute PAR [150] [rt 4-10 | both 4-10] [direct]  ->  Route. Raises ValueError with a friendly message."""
    if not args:
        raise ValueError(ADD_USAGE)
    dest = args[0].upper()
    if not valid_code(dest):
        raise ValueError(f"'{args[0]}' is not a 3-letter IATA code (e.g. PAR, IST, DXB)")
    route = Route(destination=dest)
    rest = [a.lower() for a in args[1:]]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("rt", "return", "roundtrip", "round", "both"):
            route.round_trip = a != "both"
            route.both = a == "both"
            if i + 1 < len(rest) and re.fullmatch(r"\d+-\d+", rest[i + 1]):
                lo, hi = map(int, rest[i + 1].split("-"))
                if lo > hi or lo < 1:
                    raise ValueError("Stay must look like 4-10 (min-max days)")
                route.min_days, route.max_days = lo, hi
                i += 1
        elif a in ("direct", "nonstop"):
            route.direct_only = True
        else:
            try:
                route.max_price = float(a)
            except ValueError:
                raise ValueError(f"Unknown option '{a}'. " + ADD_USAGE)
            if route.max_price <= 0:
                raise ValueError("Price must be positive")
        i += 1
    return route


TRIP_WORDS = {
    "ow": "ow", "oneway": "ow", "one-way": "ow", "one": "ow",
    "rt": "rt", "round": "rt", "roundtrip": "rt", "return": "rt",
    "both": "both", "all": "both",
}


def parse_trip(args: list[str]) -> tuple[str, str, tuple[int, int] | None, float | None]:
    """/trip IST both 3-7 300  ->  ("IST", "both", (3, 7), 300.0)
    /trip anywhere rt 4-10      ->  ("ANYWHERE", "rt", (4, 10), None)"""
    usage = "Usage: /trip IST|anywhere oneway|round|both [min-max days] [round-trip price]"
    if len(args) < 2:
        raise ValueError(usage)
    target = args[0].upper()
    if target in ("ANY", "WORLD", "ALL"):
        target = "ANYWHERE"
    if target != "ANYWHERE" and not valid_code(target):
        raise ValueError(f"'{args[0]}' is not a 3-letter IATA code or 'anywhere'. " + usage)
    mode = TRIP_WORDS.get(args[1].lower())
    if not mode:
        raise ValueError(f"Unknown trip type '{args[1]}'. Use oneway, round or both.")
    stay, price = None, None
    for a in args[2:]:
        if re.fullmatch(r"\d+-\d+", a):
            lo, hi = map(int, a.split("-"))
            if lo < 1 or lo > hi:
                raise ValueError("Stay must look like 3-7 (min-max days)")
            stay = (lo, hi)
        else:
            try:
                price = float(a)
            except ValueError:
                raise ValueError(f"Unknown option '{a}'. " + usage)
            if price <= 0:
                raise ValueError("Price must be positive")
    return target, mode, stay, price
